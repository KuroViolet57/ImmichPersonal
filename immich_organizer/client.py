"""Minimal Immich REST client built on the standard library only.

Only the endpoints this tool actually needs are wrapped. Everything is
verified against the Immich OpenAPI spec (``open-api/immich-openapi-specs.json``,
API version 3.2.0); see ``docs/API-NOTES.md`` for the mapping.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

__all__ = [
    "ImmichClient",
    "ImmichError",
    "AuthError",
    "NotFoundError",
    "normalise_base_url",
]

# Immich caps ``size`` at 1000 for every search endpoint.
MAX_PAGE_SIZE = 1000
DEFAULT_TIMEOUT = 30.0


class ImmichError(RuntimeError):
    """An Immich API call failed."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class AuthError(ImmichError):
    """The API key was rejected (401/403)."""


class NotFoundError(ImmichError):
    """The requested resource does not exist (404)."""


def normalise_base_url(url: str) -> str:
    """Return the ``/api`` root for a user-supplied server URL.

    Accepts the forms people actually paste: ``http://host:2283``,
    ``http://host:2283/``, ``http://host:2283/api`` and the full
    ``http://host:2283/api/`` spelling.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("Server URL is empty")
    if "://" not in url:
        url = "http://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"Could not parse a host out of {url!r}")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api"):
        path = path + "/api"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


class ImmichClient:
    """Thin, synchronous Immich API client.

    Authentication uses the ``x-api-key`` header, which is what an API key
    created under *Account Settings -> API Keys* expects.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        verify_tls: bool = True,
        retries: int = 3,
    ):
        self.base_url = normalise_base_url(base_url)
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.retries = max(0, retries)
        self._ssl_context: ssl.SSLContext | None = None
        if not verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx

    # ---------------------------------------------------------------- plumbing

    def _url(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = self.base_url + "/" + path.lstrip("/")
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        raw: bool = False,
    ) -> Any:
        url = self._url(path, params)
        data = None
        headers = {
            "x-api-key": self.api_key,
            "Accept": "*/*" if raw else "application/json",
            "User-Agent": "immich-organizer/1.0",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self._ssl_context
                ) as resp:
                    payload = resp.read()
                    if raw:
                        return payload, resp.headers.get("Content-Type", "application/octet-stream")
                    if not payload:
                        return None
                    return json.loads(payload.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:600]
                except Exception:  # pragma: no cover - body already consumed
                    pass
                if exc.code in (401, 403):
                    raise AuthError(
                        f"Immich rejected the API key ({exc.code}). "
                        "Check the key is valid and belongs to the right user.",
                        exc.code,
                        detail,
                    ) from exc
                if exc.code == 404:
                    raise NotFoundError(f"Not found: {method} {path}", 404, detail) from exc
                # 5xx and 429 are worth another go; other 4xx are not.
                if exc.code >= 500 or exc.code == 429:
                    last_exc = exc
                    if attempt < self.retries:
                        time.sleep(2**attempt)
                        continue
                raise ImmichError(
                    f"{method} {path} failed with HTTP {exc.code}: {detail}", exc.code, detail
                ) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(2**attempt)
                    continue
                reason = getattr(exc, "reason", exc)
                raise ImmichError(
                    f"Could not reach Immich at {self.base_url}: {reason}. "
                    "Is the server running, and is the URL reachable from this machine?"
                ) from exc
        raise ImmichError(f"{method} {path} failed: {last_exc}")

    # ------------------------------------------------------------------ server

    def ping(self) -> bool:
        """``GET /server/ping`` -- reachability check, no auth required."""
        return (self._request("GET", "/server/ping") or {}).get("res") == "pong"

    def about(self) -> dict:
        """``GET /server/about`` -- version info. Requires a valid key."""
        return self._request("GET", "/server/about") or {}

    def me(self) -> dict:
        """``GET /users/me`` -- the user the API key belongs to."""
        return self._request("GET", "/users/me") or {}

    def server_config(self) -> dict:
        return self._request("GET", "/server/config") or {}

    # ------------------------------------------------------------------ search

    def search_smart(self, payload: dict) -> dict:
        """``POST /search/smart`` -- one page of CLIP similarity search.

        ``payload`` may carry ``query`` (natural language) or ``queryAssetId``
        (find assets that look like this one), plus any of the documented
        filters. Returns the raw ``SearchResponseDto``.
        """
        return self._request("POST", "/search/smart", body=payload) or {}

    def search_metadata(self, payload: dict) -> dict:
        """``POST /search/metadata`` -- exact/metadata search, no ML involved."""
        return self._request("POST", "/search/metadata", body=payload) or {}

    def iter_smart_search(
        self, payload: dict, *, limit: int, page_size: int = 250
    ) -> Iterator[dict]:
        """Yield up to ``limit`` assets from smart search, following pagination.

        Results arrive ordered by embedding distance (closest first), so the
        caller can treat the stream as a ranking and stop early.
        """
        if limit <= 0:
            return
        page_size = max(1, min(page_size, MAX_PAGE_SIZE, limit))
        page = 1
        seen = 0
        seen_ids: set[str] = set()
        while seen < limit:
            body = dict(payload)
            body["page"] = page
            body["size"] = min(page_size, limit - seen)
            assets = (self.search_smart(body) or {}).get("assets") or {}
            items = assets.get("items") or []
            if not items:
                return
            for item in items:
                asset_id = item.get("id")
                # Defensive: a page boundary should never repeat an id, but a
                # duplicate would silently inflate the result count.
                if not asset_id or asset_id in seen_ids:
                    continue
                seen_ids.add(asset_id)
                yield item
                seen += 1
                if seen >= limit:
                    return
            if not assets.get("nextPage"):
                return
            page += 1

    # ------------------------------------------------------------------ assets

    def get_asset(self, asset_id: str) -> dict:
        return self._request("GET", f"/assets/{asset_id}") or {}

    def thumbnail(self, asset_id: str, size: str = "thumbnail") -> tuple[bytes, str]:
        """``GET /assets/{id}/thumbnail`` -- returns ``(bytes, content_type)``.

        ``size`` is one of ``thumbnail``, ``preview``, ``fullsize``.
        """
        if size not in ("thumbnail", "preview", "fullsize"):
            raise ValueError(f"Unsupported thumbnail size: {size!r}")
        return self._request(
            "GET", f"/assets/{asset_id}/thumbnail", params={"size": size}, raw=True
        )

    # ------------------------------------------------------------------ albums

    def list_albums(self) -> list[dict]:
        return self._request("GET", "/albums") or []

    def get_album(self, album_id: str) -> dict:
        return self._request("GET", f"/albums/{album_id}") or {}

    def create_album(
        self, name: str, *, description: str = "", asset_ids: list[str] | None = None
    ) -> dict:
        body: dict[str, Any] = {"albumName": name}
        if description:
            body["description"] = description
        if asset_ids:
            body["assetIds"] = asset_ids
        return self._request("POST", "/albums", body=body) or {}

    def add_assets_to_album(self, album_id: str, asset_ids: list[str]) -> list[dict]:
        """``PUT /albums/{id}/assets``.

        Immich reports per-asset outcomes rather than failing the whole call;
        assets already in the album come back as ``success: false`` with
        ``error: "duplicate"``.
        """
        if not asset_ids:
            return []
        return self._request("PUT", f"/albums/{album_id}/assets", body={"ids": asset_ids}) or []

    def remove_assets_from_album(self, album_id: str, asset_ids: list[str]) -> list[dict]:
        if not asset_ids:
            return []
        return self._request("DELETE", f"/albums/{album_id}/assets", body={"ids": asset_ids}) or []

    # ------------------------------------------------- workflows and plugins

    def list_plugins(self) -> list[dict]:
        """``GET /plugins`` -- installed workflow plugins.

        Added in the Immich release that introduced Workflows; older servers
        answer 404, which the caller should treat as "not supported".
        """
        return self._request("GET", "/plugins") or []

    def list_plugin_methods(self) -> list[dict]:
        """``GET /plugins/methods`` -- the filters and actions workflows can use."""
        return self._request("GET", "/plugins/methods") or []

    def list_workflows(self) -> list[dict]:
        return self._request("GET", "/workflows") or []

    def workflow_triggers(self) -> list:
        return self._request("GET", "/workflows/triggers") or []

    def supports_workflows(self) -> bool:
        """Whether this server exposes the Workflows/plugins API at all."""
        try:
            self.list_plugins()
            return True
        except NotFoundError:
            return False

    # ------------------------------------------------------------------- misc

    def update_assets(self, asset_ids: list[str], **changes: Any) -> None:
        """``PUT /assets`` -- bulk-update flags such as ``isFavorite``.

        ``visibility`` accepts ``timeline``/``archive``/``hidden``/``locked``;
        setting ``archive`` is how you take filed photos out of the main
        timeline without deleting them.
        """
        if not asset_ids or not changes:
            return
        self._request("PUT", "/assets", body={"ids": asset_ids, **changes})
