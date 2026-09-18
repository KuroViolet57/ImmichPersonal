"""An in-process stand-in for an Immich server.

It implements just enough of the real API -- with the same request and
response shapes the OpenAPI spec documents -- that the client, engine, and web
UI can be exercised end to end without a live server.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = "test-key"


def make_assets(count: int = 40) -> list[dict]:
    assets = []
    for i in range(count):
        assets.append(
            {
                "id": str(uuid.UUID(int=i)),
                "originalFileName": f"IMG_{i:04d}.jpg",
                "type": "VIDEO" if i % 10 == 9 else "IMAGE",
                "localDateTime": f"20{20 + i % 5}-06-{(i % 28) + 1:02d}T12:00:00.000Z",
                "fileCreatedAt": f"20{20 + i % 5}-06-{(i % 28) + 1:02d}T12:00:00.000Z",
                "checksum": f"sum{i}",
                "ownerId": "owner-1",
            }
        )
    return assets


class FakeImmich:
    """Holds the fake library state and serves it over HTTP."""

    def __init__(self, assets: list[dict] | None = None):
        self.assets = assets if assets is not None else make_assets()
        self.by_id = {a["id"]: a for a in self.assets}
        # query text -> ordered asset ids, i.e. a canned CLIP ranking
        self.smart_results: dict[str, list[str]] = {}
        # reference asset id -> ordered asset ids
        self.similar_results: dict[str, list[str]] = {}
        self.albums: dict[str, dict] = {}
        self.album_members: dict[str, list[str]] = {}
        self.updates: list[dict] = []
        # Workflows/plugins API. `supports_workflows = False` emulates a server
        # released before the feature existed, which answers 404.
        self.supports_workflows = True
        self.plugins: list[dict] = []
        self.plugin_methods: list[dict] = []
        self.workflows: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self.fail_next: dict[str, int] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- helpers

    def add_album(self, name: str, members: list[str] | None = None) -> str:
        album_id = str(uuid.uuid4())
        self.albums[album_id] = {
            "id": album_id,
            "albumName": name,
            "description": "",
            "assetCount": len(members or []),
            "albumUsers": [],
            "shared": False,
            "hasSharedLink": False,
            "isActivityEnabled": True,
            "albumThumbnailAssetId": None,
            "createdAt": "2024-01-01T00:00:00.000Z",
            "updatedAt": "2024-01-01T00:00:00.000Z",
        }
        self.album_members[album_id] = list(members or [])
        return album_id

    def install_core_plugin(self) -> None:
        """Mirror Immich's built-in plugin: metadata filters, no content filter."""
        self.plugins.append({
            "id": "core", "name": "immich-plugin-core", "version": "2.0.1",
            "title": "Immich Core Plugin", "author": "immich", "description": "",
            "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z",
            "methods": [],
        })
        self.plugin_methods.extend([
            {"key": "immich-plugin-core#assetDateFilter", "name": "assetDateFilter",
             "title": "Filter by date", "description": "", "uiHints": ["Filter"],
             "types": ["AssetV1"], "hostFunctions": False},
            {"key": "immich-plugin-core#assetAddToAlbums", "name": "assetAddToAlbums",
             "title": "Add to Album(s)", "description": "", "uiHints": [],
             "types": ["AssetV1"], "hostFunctions": True},
        ])

    def install_smart_album_plugin(self) -> None:
        self.plugins.append({
            "id": "smart", "name": "immich-smart-album", "version": "1.0.0",
            "title": "Smart Album", "author": "immich-organizer", "description": "",
            "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z",
            "methods": [],
        })
        self.plugin_methods.append({
            "key": "immich-smart-album#smartMatchFilter", "name": "smartMatchFilter",
            "title": "Filter by smart search", "description": "", "uiHints": ["Filter"],
            "types": ["AssetV1"], "hostFunctions": True,
        })

    def album_by_name(self, name: str) -> dict | None:
        for album in self.albums.values():
            if album["albumName"] == name:
                return album
        return None

    # ---------------------------------------------------------- server plumbing

    @property
    def url(self) -> str:
        assert self._server, "server not started"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeImmich":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def _send(self, status: int, payload, content_type="application/json"):
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authed(self) -> bool:
                return self.headers.get("x-api-key") == API_KEY

            def _route(self, method: str):
                path = urllib.parse.urlparse(self.path).path
                outer.requests.append((method, path))

                if path == "/api/server/ping":
                    return self._send(200, {"res": "pong"})

                if not self._authed():
                    return self._send(401, {"message": "Invalid API key"})

                # Lets a test force a transient failure on a given path.
                remaining = outer.fail_next.get(path, 0)
                if remaining:
                    outer.fail_next[path] = remaining - 1
                    return self._send(500, {"message": "boom"})

                if path == "/api/server/about":
                    return self._send(200, {"version": "v2.0.0", "versionUrl": "u", "licensed": False})
                if path == "/api/users/me":
                    return self._send(200, {"id": "owner-1", "email": "me@example.com", "name": "Me"})

                if path == "/api/search/smart" and method == "POST":
                    return self._send(200, outer._smart(self._body()))
                if path == "/api/search/metadata" and method == "POST":
                    return self._send(200, outer._metadata(self._body()))

                if path in ("/api/plugins", "/api/plugins/methods", "/api/workflows"):
                    if not outer.supports_workflows:
                        return self._send(404, {"message": "Not found"})
                    return self._send(200, {
                        "/api/plugins": outer.plugins,
                        "/api/plugins/methods": outer.plugin_methods,
                        "/api/workflows": outer.workflows,
                    }[path])

                if path == "/api/albums" and method == "GET":
                    return self._send(200, list(outer.albums.values()))
                if path == "/api/albums" and method == "POST":
                    body = self._body()
                    album_id = outer.add_album(body["albumName"], body.get("assetIds") or [])
                    return self._send(201, outer.albums[album_id])

                match = re.fullmatch(r"/api/albums/([0-9a-f-]+)/assets", path)
                if match:
                    return self._send(200, outer._album_assets(match.group(1), method, self._body()))

                match = re.fullmatch(r"/api/albums/([0-9a-f-]+)", path)
                if match and method == "GET":
                    album = outer.albums.get(match.group(1))
                    return self._send(200, album) if album else self._send(404, {"message": "no"})

                match = re.fullmatch(r"/api/assets/([0-9a-f-]+)/thumbnail", path)
                if match:
                    return self._send(200, b"\x89PNG\r\n\x1a\nFAKE", "image/png")

                match = re.fullmatch(r"/api/assets/([0-9a-f-]+)", path)
                if match and method == "GET":
                    asset = outer.by_id.get(match.group(1))
                    return self._send(200, asset) if asset else self._send(404, {"message": "no"})

                if path == "/api/assets" and method == "PUT":
                    outer.updates.append(self._body())
                    return self._send(204, b"", "text/plain")

                return self._send(404, {"message": f"unhandled {method} {path}"})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

            def do_DELETE(self):
                self._route("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------- endpoints

    def _page(self, ids: list[str], body: dict) -> dict:
        size = int(body.get("size") or 100)
        page = int(body.get("page") or 1)
        start = (page - 1) * size
        window = ids[start : start + size]
        has_more = len(ids) > start + size
        return {
            "albums": {"total": 0, "count": 0, "items": [], "facets": []},
            "assets": {
                "total": len(ids),
                "count": len(window),
                "items": [self.by_id[i] for i in window if i in self.by_id],
                "facets": [],
                "nextPage": str(page + 1) if has_more else None,
                "nextCursor": None,
            },
        }

    def _matches_filters(self, asset: dict, body: dict) -> bool:
        if body.get("type") and asset.get("type") != body["type"]:
            return False
        if body.get("takenAfter") and asset["localDateTime"] < body["takenAfter"]:
            return False
        if body.get("takenBefore") and asset["localDateTime"] > body["takenBefore"]:
            return False
        if body.get("isNotInAlbum"):
            filed = {i for ids in self.album_members.values() for i in ids}
            if asset["id"] in filed:
                return False
        return True

    def _smart(self, body: dict) -> dict:
        if body.get("queryAssetId"):
            ids = self.similar_results.get(body["queryAssetId"], [])
        else:
            ids = self.smart_results.get(body.get("query", ""), [])
        ids = [i for i in ids if i in self.by_id and self._matches_filters(self.by_id[i], body)]
        return self._page(ids, body)

    def _metadata(self, body: dict) -> dict:
        album_ids = body.get("albumIds") or []
        if album_ids:
            ids = [i for album_id in album_ids for i in self.album_members.get(album_id, [])]
        else:
            ids = [a["id"] for a in self.assets]
        ids = [i for i in ids if self._matches_filters(self.by_id[i], body)]
        return self._page(ids, body)

    def _album_assets(self, album_id: str, method: str, body: dict) -> list[dict]:
        members = self.album_members.setdefault(album_id, [])
        out = []
        for asset_id in body.get("ids", []):
            if method == "PUT":
                if asset_id in members:
                    out.append({"id": asset_id, "success": False, "error": "duplicate"})
                elif asset_id not in self.by_id:
                    out.append({"id": asset_id, "success": False, "error": "not_found"})
                else:
                    members.append(asset_id)
                    out.append({"id": asset_id, "success": True})
            else:
                if asset_id in members:
                    members.remove(asset_id)
                    out.append({"id": asset_id, "success": True})
                else:
                    out.append({"id": asset_id, "success": False, "error": "not_found"})
        if album_id in self.albums:
            self.albums[album_id]["assetCount"] = len(members)
        return out
