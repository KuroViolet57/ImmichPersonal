"""A small local web server backing the mobile UI.

Design notes:

* It is a thin proxy in front of the Immich API. The browser never sees the
  Immich API key -- requests are signed server-side.
* Binding to ``0.0.0.0`` so a phone can reach it also exposes it to everything
  else on the network, so every data route requires a shared token. The token
  is printed with the URL at startup and can be pinned via
  ``IMMICH_ORGANIZER_TOKEN`` so a home-screen shortcut keeps working across
  restarts.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import secrets
import socket
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..client import ImmichClient, ImmichError
from ..engine import apply_plan, build_plan, evaluate_rule, find_album, Plan, RuleMatch
from ..rules import Rule, RuleError, load_rules, parse_filters

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 1 << 20  # 1 MiB is far more than any request here needs
TOKEN_HEADER = "X-Organizer-Token"


def _local_ip() -> str:
    """Best guess at the LAN address to show in the startup banner."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # no packets are sent
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class OrganizerHandler(BaseHTTPRequestHandler):
    server_version = "immich-organizer"
    protocol_version = "HTTP/1.1"

    # Injected by serve()
    client: ImmichClient
    token: str
    rules_path: Path | None
    verbose: bool = False

    # -------------------------------------------------------------- utilities

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        if self.verbose:
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: bytes, content_type: str, *, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, data: object) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("request body too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        supplied = self.headers.get(TOKEN_HEADER) or (query.get("t") or [""])[0]
        return hmac.compare_digest(supplied, self.token)

    # ---------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"

        if route in ("/", "/index.html"):
            return self._serve_static("index.html")
        if route.startswith("/static/"):
            return self._serve_static(route[len("/static/"):])
        if route in ("/manifest.webmanifest", "/sw.js", "/icon.svg"):
            return self._serve_static(route.lstrip("/"))

        if route.startswith(("/api/", "/thumb/")) and not self._authorised(query):
            return self._error(401, "Missing or invalid access token.")

        try:
            if route == "/api/status":
                return self._json(200, self._status())
            if route == "/api/albums":
                return self._json(200, [
                    {
                        "id": a.get("id"),
                        "name": a.get("albumName"),
                        "count": a.get("assetCount", 0),
                    }
                    for a in sorted(
                        self.client.list_albums(),
                        key=lambda a: (a.get("albumName") or "").casefold(),
                    )
                ])
            if route == "/api/rules":
                return self._json(200, self._rules_payload())
            if route.startswith("/thumb/"):
                return self._serve_thumb(route[len("/thumb/"):], query)
        except ImmichError as exc:
            return self._error(502, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return self._error(500, f"{type(exc).__name__}: {exc}")

        self._error(404, "Not found")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        route = parsed.path.rstrip("/") or "/"

        if not self._authorised(query):
            return self._error(401, "Missing or invalid access token.")

        try:
            body = self._read_json()
        except ValueError as exc:
            return self._error(400, str(exc))

        try:
            if route == "/api/search":
                return self._json(200, self._search(body))
            if route == "/api/file":
                return self._json(200, self._file_assets(body))
            if route == "/api/plan":
                return self._json(200, self._run_rules(body, apply_changes=False))
            if route == "/api/apply":
                return self._json(200, self._run_rules(body, apply_changes=True))
        except (ValueError, RuleError) as exc:
            return self._error(400, str(exc))
        except ImmichError as exc:
            return self._error(502, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            return self._error(500, f"{type(exc).__name__}: {exc}")

        self._error(404, "Not found")

    # --------------------------------------------------------------- handlers

    def _serve_static(self, relative: str) -> None:
        # Resolve inside STATIC_DIR so a crafted path cannot escape it.
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self._error(403, "Forbidden")
        if not target.is_file():
            return self._error(404, "Not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".webmanifest":
            content_type = "application/manifest+json"
        self._send(200, target.read_bytes(), content_type, cache="no-cache")

    def _serve_thumb(self, asset_id: str, query: dict[str, list[str]]) -> None:
        size = (query.get("size") or ["thumbnail"])[0]
        if size not in ("thumbnail", "preview"):
            size = "thumbnail"
        try:
            payload, content_type = self.client.thumbnail(asset_id, size=size)
        except ImmichError as exc:
            return self._error(502, str(exc))
        # Thumbnails are immutable for a given asset id, so let the phone cache them.
        self._send(200, payload, content_type, cache="private, max-age=3600")

    def _status(self) -> dict:
        user, about = {}, {}
        try:
            user = self.client.me()
            about = self.client.about()
        except ImmichError as exc:
            return {"connected": False, "error": str(exc), "server": self.client.base_url}
        return {
            "connected": True,
            "server": self.client.base_url,
            "user": user.get("email") or user.get("name") or "",
            "version": about.get("version", ""),
            "rulesFile": str(self.rules_path) if self.rules_path else None,
        }

    def _rules_payload(self) -> dict:
        if not self.rules_path:
            return {"loaded": False, "rules": []}
        try:
            ruleset = load_rules(self.rules_path)
        except RuleError as exc:
            return {"loaded": False, "error": str(exc), "rules": []}
        return {
            "loaded": True,
            "path": str(self.rules_path),
            "rules": [
                {
                    "name": r.name,
                    "album": r.album,
                    "match": r.describe_match(),
                    "limit": r.limit,
                    "enabled": r.enabled,
                }
                for r in ruleset.rules
            ],
        }

    def _build_ad_hoc_rule(self, body: dict) -> Rule:
        query = (body.get("query") or "").strip() or None
        like = (body.get("like") or "").strip() or None
        if bool(query) == bool(like):
            raise ValueError("Provide either a text query or a reference asset, not both.")

        limit = body.get("limit", 60)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be a whole number between 1 and 1000")

        raw_filters = body.get("filters") or {}
        if not isinstance(raw_filters, dict):
            raise ValueError("filters must be an object")

        refine = body.get("refine") or {}
        if not isinstance(refine, dict):
            raise ValueError("refine must be an object")

        rule = Rule(
            name="ad-hoc",
            album=(body.get("album") or "(preview)").strip() or "(preview)",
            query=query,
            like_asset=like,
            limit=limit,
            filters=parse_filters(raw_filters, "filters"),
        )
        all_of = [s.strip() for s in (refine.get("all_of") or []) if isinstance(s, str) and s.strip()]
        none_of = [s.strip() for s in (refine.get("none_of") or []) if isinstance(s, str) and s.strip()]
        rule.refine.all_of = all_of
        rule.refine.none_of = none_of
        return rule

    def _search(self, body: dict) -> dict:
        rule = self._build_ad_hoc_rule(body)
        matches = evaluate_rule(self.client, rule)
        return {
            "match": rule.describe_match(),
            "count": len(matches),
            "assets": [
                {
                    "id": a.get("id"),
                    "name": a.get("originalFileName"),
                    "type": a.get("type"),
                    "date": (a.get("localDateTime") or a.get("fileCreatedAt") or "")[:10],
                }
                for a in matches
            ],
        }

    def _file_assets(self, body: dict) -> dict:
        asset_ids = body.get("assetIds") or []
        album_name = (body.get("album") or "").strip()
        if not isinstance(asset_ids, list) or not all(isinstance(i, str) for i in asset_ids):
            raise ValueError("assetIds must be a list of strings")
        if not asset_ids:
            raise ValueError("Select at least one asset first.")
        if not album_name:
            raise ValueError("An album name is required.")

        albums = self.client.list_albums()
        album = find_album(albums, album_name)
        created = False
        if album is None:
            if not body.get("createAlbum", True):
                raise ValueError(f"Album {album_name!r} does not exist.")
            album = self.client.create_album(album_name, description="Created by immich-organizer")
            created = True

        responses = self.client.add_assets_to_album(album["id"], asset_ids)
        added = [r["id"] for r in responses if r.get("success")]
        duplicates = sum(1 for r in responses if r.get("error") == "duplicate")
        failures = [
            f"{r.get('id')}: {r.get('error')}"
            for r in responses
            if not r.get("success") and r.get("error") != "duplicate"
        ]

        if added and body.get("favorite"):
            self.client.update_assets(added, isFavorite=True)
        if added and body.get("archive"):
            self.client.update_assets(added, visibility="archive")

        return {
            "album": album_name,
            "albumId": album.get("id"),
            "created": created,
            "added": len(added),
            "duplicates": duplicates,
            "failures": failures,
        }

    def _run_rules(self, body: dict, *, apply_changes: bool) -> dict:
        if not self.rules_path:
            raise ValueError("No rules file was loaded. Start `serve` with --rules.")
        ruleset = load_rules(self.rules_path)
        only = body.get("only") or None
        if only is not None and not isinstance(only, list):
            raise ValueError("`only` must be a list of rule names")

        plan = build_plan(self.client, ruleset, only=only)
        payload = {
            "totalToAdd": plan.total_to_add,
            "newAlbums": plan.albums_to_create(),
            "entries": [
                {
                    "rule": e.rule.name,
                    "album": e.album_name,
                    "match": e.rule.describe_match(),
                    "albumExists": e.album_exists,
                    "toAdd": len(e.to_add),
                    "alreadyThere": len(e.already_in_album),
                    "error": e.error,
                    "assets": [
                        {
                            "id": a.get("id"),
                            "name": a.get("originalFileName"),
                            "date": (a.get("localDateTime") or a.get("fileCreatedAt") or "")[:10],
                        }
                        for a in e.to_add[:120]
                    ],
                }
                for e in plan.entries
            ],
        }
        if not apply_changes:
            payload["applied"] = False
            return payload

        result = apply_plan(self.client, plan)
        payload.update(
            applied=True,
            runId=result.run_id,
            added=result.total_added,
            createdAlbums=result.created_albums,
            failures=result.failures,
        )
        return payload


def serve(
    client: ImmichClient,
    *,
    host: str = "127.0.0.1",
    port: int = 8777,
    rules_path: Path | None = None,
    open_browser: bool = False,
    token: str | None = None,
) -> int:
    """Run the UI until interrupted. Returns a process exit code."""
    token = token or os.environ.get("IMMICH_ORGANIZER_TOKEN") or secrets.token_urlsafe(18)

    handler = type(
        "BoundOrganizerHandler",
        (OrganizerHandler,),
        {
            "client": client,
            "token": token,
            "rules_path": rules_path,
            "verbose": bool(os.environ.get("IMMICH_ORGANIZER_DEBUG")),
        },
    )

    try:
        httpd = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        print(f"Could not bind {host}:{port} -- {exc}")
        return 1
    httpd.daemon_threads = True

    shown_host = _local_ip() if host in ("0.0.0.0", "::") else host
    url = f"http://{shown_host}:{port}/?t={token}"

    print(f"Immich Organizer UI -> {url}")
    print(f"  Immich server: {client.base_url}")
    print(f"  Rules file:    {rules_path or '(none - ad-hoc searches only)'}")
    if host in ("0.0.0.0", "::"):
        print("\n  Reachable from your phone on the same network at the URL above.")
        print("  Open it in Chrome, then menu -> 'Add to Home screen' to install it.")
        print("  The token in the URL is the only thing protecting your library,")
        print("  so treat that link as a password. Pin it across restarts with")
        print("  IMMICH_ORGANIZER_TOKEN=<value>.")
    else:
        print("\n  Bound to localhost only. Use --host 0.0.0.0 to reach it from a phone.")
    print("\n  Ctrl+C to stop.")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        httpd.server_close()
    return 0
