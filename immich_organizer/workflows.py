"""Creating Immich Workflows that use the Smart Album plugin.

Immich's native Workflows fire on asset events and can file assets into albums,
but every filter its core plugin ships matches on metadata. The
``immich-smart-album`` plugin adds a filter that matches on image content; this
module wires the two into a workflow without hand-writing the JSON.

The plugin needs an Immich API key of its own, because it reaches smart search
over HTTP and the workflow host injects no credentials. That key is a secret:
it is read from the environment by preference, never written to a file here,
and always redacted before anything is printed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .client import ImmichClient, ImmichError

PLUGIN_NAME = "immich-smart-album"
FILTER_METHOD = f"{PLUGIN_NAME}#smartMatchFilter"
ALBUM_METHOD = "immich-plugin-core#assetAddToAlbums"

ENV_PLUGIN_KEY = "IMMICH_PLUGIN_API_KEY"
REDACTED = "***redacted***"
DEFAULT_SERVER_URL = "http://localhost:2283"
DEFAULT_LIMIT = 200

# Every trigger Immich exposes. AssetTagged is the default because it is the
# only one that reliably runs after Smart Search has indexed the asset --
# see docs/API-NOTES.md for the job ordering.
TRIGGERS = ("AssetTagged", "AssetCreate", "AssetMetadataExtraction")
RELIABLE_TRIGGER = "AssetTagged"


class WorkflowError(RuntimeError):
    """The workflow cannot be built as described."""


@dataclass
class SmartAlbumSpec:
    """What a "file photos that look like X into album Y" workflow needs."""

    name: str
    album: str
    query: str | None = None
    like_asset: str | None = None
    limit: int = DEFAULT_LIMIT
    trigger: str = RELIABLE_TRIGGER
    server_url: str = DEFAULT_SERVER_URL
    inverse: bool = False
    enabled: bool = True

    def validate(self) -> None:
        if not self.name.strip():
            raise WorkflowError("the workflow needs a name")
        if not self.album.strip():
            raise WorkflowError("the workflow needs a target album")
        if bool(self.query) == bool(self.like_asset):
            raise WorkflowError(
                "set exactly one of a description (--query) or a reference photo (--like)"
            )
        if self.trigger not in TRIGGERS:
            raise WorkflowError(f"trigger must be one of {', '.join(TRIGGERS)}")
        if not 1 <= self.limit <= 1000:
            raise WorkflowError("match depth must be between 1 and 1000")

    def describe_match(self) -> str:
        if self.query:
            return f'text "{self.query}"'
        return f"similar to asset {self.like_asset}"


def resolve_plugin_api_key(explicit: str | None, configured: str | None) -> tuple[str, str]:
    """Find the API key the plugin step should carry, and say where it came from.

    Preference order puts the environment ahead of a command-line flag, because
    an argument is visible in shell history and in the process list while a
    variable is not.
    """
    from_env = os.environ.get(ENV_PLUGIN_KEY, "").strip()
    if from_env:
        return from_env, f"${ENV_PLUGIN_KEY}"
    if explicit:
        return explicit.strip(), "--plugin-api-key (visible in shell history)"
    if configured:
        return configured.strip(), "this tool's own configured key (likely over-permissioned)"
    raise WorkflowError(
        "No API key available for the plugin step. Set it in the environment:\n"
        f"    export {ENV_PLUGIN_KEY}='<key with asset.read>'\n"
        "Create the key in Immich under Account Settings -> API Keys."
    )


def missing_plugin_methods(client: ImmichClient) -> list[str]:
    """Which required workflow steps this server does not offer.

    Checked before creating anything, because Immich accepts a workflow naming
    a method that does not exist and it simply never runs.
    """
    try:
        methods = client.list_plugin_methods()
    except ImmichError as exc:
        raise WorkflowError(
            f"Could not read the server's plugin methods ({exc}). "
            "An API key with 'plugin.read' is needed to check this."
        ) from exc

    available = {m.get("key") for m in methods}
    return [m for m in (FILTER_METHOD, ALBUM_METHOD) if m not in available]


def build_payload(spec: SmartAlbumSpec, api_key: str) -> dict:
    """Render the spec as the ``WorkflowCreateDto`` Immich expects."""
    spec.validate()

    filter_config: dict[str, object] = {
        "limit": spec.limit,
        "serverUrl": spec.server_url,
        "apiKey": api_key,
        "explainMisses": True,
    }
    if spec.query:
        filter_config["query"] = spec.query
    else:
        filter_config["likeAssetId"] = spec.like_asset
    if spec.inverse:
        filter_config["inverse"] = True

    return {
        "name": spec.name,
        "description": (
            f"Files assets matching {spec.describe_match()} into {spec.album!r}. "
            "Created by immich-organizer."
        ),
        "trigger": spec.trigger,
        "enabled": spec.enabled,
        "logging": True,
        "steps": [
            {"method": FILTER_METHOD, "config": filter_config, "enabled": True},
            {
                "method": ALBUM_METHOD,
                # An empty albumIds with albumName makes the core plugin reuse an
                # album of that name, or create one on first run.
                "config": {"albumIds": [], "albumName": spec.album},
                "enabled": True,
            },
        ],
    }


def redact(payload: dict) -> dict:
    """A copy of the payload safe to print, log, or paste into a report."""
    safe = {**payload, "steps": []}
    for step in payload.get("steps", []):
        config = dict(step.get("config") or {})
        if config.get("apiKey"):
            config["apiKey"] = REDACTED
        safe["steps"].append({**step, "config": config})
    return safe


def trigger_warning(trigger: str) -> str | None:
    """Warn about triggers that fire before Smart Search has indexed the asset."""
    if trigger == RELIABLE_TRIGGER:
        return None
    return (
        f"Trigger {trigger!r} fires before Immich has built the asset's smart-search\n"
        "  embedding, so the filter cannot match a freshly uploaded photo. Use\n"
        f"  {RELIABLE_TRIGGER!r}, or sweep the library with `immich-organizer apply` instead."
    )
