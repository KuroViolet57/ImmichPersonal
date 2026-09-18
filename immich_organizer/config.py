"""Where the server URL and API key live, and how they are found."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "immich-organizer"
ENV_URL = "IMMICH_URL"
ENV_KEY = "IMMICH_API_KEY"


def config_dir() -> Path:
    """Platform-appropriate config directory.

    Windows gets ``%APPDATA%\\immich-organizer``; everything else follows the
    XDG spec so Termux on Android lands somewhere sane too.
    """
    override = os.environ.get("IMMICH_ORGANIZER_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


def state_dir() -> Path:
    """Directory for the action journal and cached reports."""
    override = os.environ.get("IMMICH_ORGANIZER_HOME")
    if override:
        return Path(override).expanduser() / "state"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / APP_NAME / "state"
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / APP_NAME


@dataclass
class Settings:
    server_url: str = ""
    api_key: str = ""
    verify_tls: bool = True
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.server_url and self.api_key)

    def redacted(self) -> dict:
        key = self.api_key
        shown = f"{key[:4]}...{key[-4:]}" if len(key) > 12 else ("set" if key else "")
        return {
            "server_url": self.server_url,
            "api_key": shown,
            "verify_tls": self.verify_tls,
            "timeout": self.timeout,
        }


def load_settings() -> Settings:
    """Read settings from disk, then let environment variables win.

    Environment overrides matter for Docker/Task Scheduler runs where writing
    a config file is awkward.
    """
    settings = Settings()
    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read config at {path}: {exc}") from exc
        settings.server_url = data.get("server_url", "") or ""
        settings.api_key = data.get("api_key", "") or ""
        settings.verify_tls = bool(data.get("verify_tls", True))
        settings.timeout = float(data.get("timeout", 30.0))

    if os.environ.get(ENV_URL):
        settings.server_url = os.environ[ENV_URL]
    if os.environ.get(ENV_KEY):
        settings.api_key = os.environ[ENV_KEY]
    if os.environ.get("IMMICH_VERIFY_TLS"):
        settings.verify_tls = os.environ["IMMICH_VERIFY_TLS"].lower() not in ("0", "false", "no")
    return settings


def save_settings(settings: Settings) -> Path:
    """Persist settings, readable only by the current user where the OS allows."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "server_url": settings.server_url,
        "api_key": settings.api_key,
        "verify_tls": settings.verify_tls,
        "timeout": settings.timeout,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
    if os.name != "nt":
        # The file holds a credential; keep it out of other users' reach.
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path
