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
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..client import ImmichClient, ImmichError
from .. import aitagger as aitagger_mod
from .. import albums as albums_mod
from .. import searchplus as searchplus_mod
from .. import themes as themes_mod
from ..config import state_dir
from ..engine import (
    apply_plan, build_plan, evaluate_rule, excluded_asset_ids, file_assets,
    read_journal, undo_run,
)
from ..rules import MAX_LIMIT, Rule, RuleError, load_rules, parse_filters, parse_ruleset

RULES_HEADER = """\
# Immich Organizer rules -- edited from the panel's Rules tab.
# A rule is a saved search: "photos that look like X go into album Y".
# Preview shows what a rule would add; Apply adds it. Re-running a rule only
# adds photos that are not in the album yet. Reference: docs/RULES.md
"""

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 4 << 20  # room for ~10k asset ids in a skip/select list
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


def _plain(value):
    """YAML turns unquoted 2024-01-01 into a date; the browser needs text."""
    import datetime as _dt
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return value


def _log(message: str) -> None:
    """One line on stdout -- the journal under systemd (``journalctl -u immich-organizer``).

    Milliseconds are included so lines can be matched against the app's own log.
    """
    now = time.time()
    print(f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now % 1 * 1000):03d} {message}", flush=True)


def _rate(nbytes: int, seconds: float) -> str:
    return f"{nbytes * 8 / seconds / 1e6:.1f} Mbit/s" if nbytes > 0 and seconds > 0 else "-"


class OrganizerHandler(BaseHTTPRequestHandler):
    server_version = "immich-organizer"
    protocol_version = "HTTP/1.1"

    # Injected by serve()
    client: ImmichClient
    token: str
    rules_path: Path | None
    verbose: bool = False
    theme_backend = None  # tests inject a fake; None = the real Docker/ML backend
    theme_sp_engine = None  # tests inject a fake Search+ scorer for smart albums; None = the real index
    searchplus_parts = None  # tests inject (store, service, indexer); None = the real ones
    aitagger_parts = None  # tests inject (store, services, indexer) for the AI Tagger; None = the real ones

    # -------------------------------------------------------------- utilities

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002
        if self.verbose:
            super().log_message(fmt, *args)

    def handle_one_request(self) -> None:
        # The phone often drops a kept-alive connection (Tailscale/Wi-Fi changes, the app closing a video).
        # That is not an error worth a traceback in the journal; just stop serving this connection.
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

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

        if route.startswith(("/api/", "/thumb/", "/media/")) and not self._authorised(query):
            return self._error(401, "Missing or invalid access token.")

        try:
            if route == "/api/status":
                return self._json(200, self._status())
            if route == "/api/diag/speed":
                return self._serve_speed_test(query)
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
            if route == "/api/prefs":
                return self._json(200, self._prefs())
            if route == "/api/history":
                return self._json(200, self._history())
            if route == "/api/people":
                return self._json(200, self._people())
            if route == "/api/faces":
                return self._json(200, self._faces())
            if route == "/api/themes":
                return self._json(200, self._themes_list())
            if route == "/api/searchplus":
                return self._json(200, self._searchplus_status())
            if route == "/api/aitagger":
                return self._json(200, self._aitagger_status())
            if route.startswith("/api/aitagger/"):
                return self._json(200, self._aitagger_get(route[len("/api/aitagger/"):], query))
            if route == "/api/albums/list":
                return self._json(200, {"albums": albums_mod.summarise_albums(self.client)})
            if route.startswith("/api/albums/") and route.endswith("/items"):
                album_id = route[len("/api/albums/"):-len("/items")]
                return self._json(200, self._album_items(album_id, (query.get("sort") or ["taken_desc"])[0],
                                                         (query.get("type") or [""])[0], (query.get("q") or [""])[0]))
            if route.startswith("/api/asset/"):
                return self._json(200, self._asset_info(route[len("/api/asset/"):]))
            if route.startswith("/media/"):
                return self._serve_media(route[len("/media/"):], query)
            if route.startswith("/thumb/person/"):
                return self._serve_person_thumb(route[len("/thumb/person/"):])
            if route.startswith("/thumb/"):
                return self._serve_thumb(route[len("/thumb/"):], query)
        except ValueError as exc:
            return self._error(400, str(exc))
        except ImmichError as exc:
            return self._error(502, str(exc))
        except aitagger_mod.NotFound as exc:
            return self._error(404, str(exc))
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
            if route == "/api/rules/save":
                return self._json(200, self._save_rules(body))
            if route == "/api/prefs":
                return self._json(200, self._save_prefs(body))
            if route == "/api/undo":
                return self._json(200, self._undo(body))
            if route == "/api/faces/measure":
                return self._json(200, self._faces_measure())
            if route == "/api/faces/visibility":
                return self._json(200, self._faces_visibility(body))
            if route == "/api/faces/covers":
                return self._json(200, self._faces_covers())
            if route.startswith("/api/albums/"):
                return self._json(200, self._album_action(route[len("/api/albums/"):], body))
            if route.startswith("/api/themes/"):
                return self._json(200, self._theme_action(route[len("/api/themes/"):], body))
            if route.startswith("/api/searchplus/"):
                return self._json(200, self._searchplus_action(route[len("/api/searchplus/"):], body))
            if route.startswith("/api/aitagger/"):
                return self._json(200, self._aitagger_action(route[len("/api/aitagger/"):], body))
        except (ValueError, RuleError) as exc:
            return self._error(400, str(exc))
        except searchplus_mod.GpuBusy as exc:
            return self._error(503, str(exc))
        except aitagger_mod.ImmichDown as exc:
            return self._error(502, f"Immich is not answering ({exc}).")
        except searchplus_mod.ServiceDown as exc:
            if route.startswith("/api/aitagger"):
                return self._error(503, f"The AI Tagger models are loading or not running ({exc}). "
                                        "Try again in a minute.")
            return self._error(503, f"The Search+ model is not ready yet ({exc}). Try again in a moment.")
        except ImmichError as exc:
            return self._error(502, str(exc))
        except aitagger_mod.NotFound as exc:
            return self._error(404, str(exc))
        except LookupError:
            return self._error(404, "Not found")
        except Exception as exc:  # pragma: no cover - defensive
            return self._error(500, f"{type(exc).__name__}: {exc}")

        self._error(404, "Not found")

    # ------------------------------------------------------------ Search+ (experimental)

    def _sp(self):
        return self.searchplus_parts or searchplus_mod.instance()

    def _searchplus_status(self) -> dict:
        store, service, indexer = self._sp()
        health = service.health(timeout=1.5) or {}
        return {
            "model": searchplus_mod.MODEL_LABEL, "indexModel": store.model or None,
            "settings": searchplus_mod.load_settings(), "limits": {k: list(v) for k, v in searchplus_mod.LIMITS.items()},
            "counts": store.counts(), "indexer": indexer.status(), "failures": store.failures(),
            "service": {"container": service.container_state(), "status": health.get("status"),
                        "error": health.get("error") or "", "idleExitMinutes": health.get("idleExitMinutes"),
                        "idleSeconds": health.get("idleSeconds"), "loadedIn": health.get("loadedIn")},
        }

    def _searchplus_action(self, action: str, body: dict) -> dict:
        store, service, indexer = self._sp()
        if action == "search":
            return self._searchplus_search(body)
        if action == "settings":
            changes = body.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise ValueError("Nothing to change.")
            searchplus_mod.save_settings({k: v for k, v in changes.items() if k != "indexing"})
        elif action == "index":
            what = body.get("action")
            if what == "start":
                searchplus_mod.save_settings({"indexing": True})
                indexer.start()
            elif what == "pause":
                searchplus_mod.save_settings({"indexing": False})
                indexer.stop()
            elif what == "clear":
                store.clear_failed()
            elif what == "retry":
                store.retry_failed()
                if searchplus_mod.load_settings()["indexing"]:
                    indexer.start()             # wakes it up if it is waiting for new photos
            elif what == "reset":
                if body.get("confirm") != "reset":
                    raise ValueError("Starting over deletes the Search+ index; confirm it first.")
                indexer.stop(wait_s=30)
                store.reset()
            else:
                raise ValueError("action must be start, pause, retry, clear or reset")
        elif action == "unload":
            searchplus_mod.save_settings({"indexing": False})
            indexer.stop()
            stopped = service.stop()
            return {"stopped": stopped, **self._searchplus_status()}
        else:
            raise LookupError(action)
        return self._searchplus_status()

    def _searchplus_search(self, body: dict) -> dict:
        import time as _time

        store, service, _ = self._sp()
        text = str(body.get("text") or "").strip()[:500]
        like = str(body.get("like") or "").strip()
        media = body.get("media") if body.get("media") in ("IMAGE", "VIDEO") else None
        after = str(body.get("after") or "")[:10] or None
        before = str(body.get("before") or "")[:10] or None
        try:
            limit = max(1, min(int(body.get("limit") or 200), 1000))
        except (TypeError, ValueError):
            raise ValueError("limit must be a number") from None
        started = _time.monotonic()
        if like:
            like = self._uuid(like)
            vector = store.view().asset_vector(like)
        elif text:
            health = service.ready(wait=150)
            if store.model and health.get("model") != store.model:
                raise ValueError(f"The index was built with {store.model}, but the model server runs {health.get('model')}.")
            vector = service.embed_text([text])[0]
        else:
            raise ValueError("Type what you are looking for, or pick a photo to find more like it.")
        found = searchplus_mod.search(store, vector, media=media, after=after, before=before, limit=limit,
                                      skip=like or None)
        out = {"assets": found, "count": len(found), "tookMs": round((_time.monotonic() - started) * 1000),
               "counts": store.counts(), "query": {"text": text, "like": like or None}}
        if body.get("compare"):
            out["immich"] = self._searchplus_immich(text, like, media, after, before, limit)
            mine = {a["id"] for a in found}
            out["immich"]["overlap"] = sum(1 for a in out["immich"]["assets"] if a["id"] in mine)
        return out

    def _searchplus_immich(self, text, like, media, after, before, limit) -> dict:
        """The same question asked to Immich's own smart search, for comparison."""
        store = self._sp()[0]
        backend = self._backend()
        model = backend.model_name(self.client)
        kw = {"media": media, "taken_after": after, "taken_before": before, "limit": limit + (1 if like else 0)}
        rows = backend.select(like=like, **kw) if like else backend.select(vec=backend.text_vector(text, model), **kw)
        view = store.view()
        assets = []
        for aid, score in rows:
            if aid == like:
                continue
            p = view.pos.get(aid)
            assets.append({"id": aid, "score": round(score, 4) if score is not None else None,
                           "type": str(view.types[p]) if p is not None else "",
                           "date": str(view.taken[p]) if p is not None else "",
                           "name": view.names[p] if p is not None else ""})
        return {"model": model, "assets": assets[:limit]}

    # ------------------------------------------------------------ AI Tagger

    def _at(self):
        return self.aitagger_parts or aitagger_mod.instance(self.client)

    def _aitagger_status(self) -> dict:
        store, service, indexer = self._at()
        counts = store.counts()
        return {
            "settings": aitagger_mod.load_settings(),
            "limits": {k: list(v) for k, v in aitagger_mod.LIMITS.items()},
            "settingsVersion": store.settings_version, "counts": counts, "indexer": indexer.status(counts),
            "service": {**service.status(), "exclusive": searchplus_mod.AITAGGER_EXCLUSIVE},
            "models": aitagger_mod.model_labels(), "failures": store.failures(),
            "reprocessKeys": {mode: list(keys) for mode, keys in aitagger_mod.REPROCESS.items()},
        }

    def _asset_ids(self, value, name: str = "ids", limit: int = 5000) -> list[str]:
        if not isinstance(value, list) or not value or not all(isinstance(i, str) for i in value):
            raise ValueError(f"{name} must be a non-empty list of asset ids")
        if len(value) > limit:
            raise ValueError(f"at most {limit} assets at a time")
        return [self._uuid(i) for i in value]

    def _aitagger_get(self, what: str, query: dict[str, list[str]]) -> dict:
        store, _service, indexer = self._at()
        arg = lambda name, default="": (query.get(name) or [default])[0]  # noqa: E731
        if what == "assets":
            try:
                page, size = max(int(arg("page", "1")), 1), max(1, min(int(arg("size", "60")), 200))
            except ValueError:
                raise ValueError("page and size must be numbers") from None
            return store.list_assets(tag=arg("tag").strip(), q=arg("q").strip(),
                                     outdated=arg("outdated") in ("1", "true"), page=page, size=size)
        if what == "sample":
            kind = arg("type")
            if kind not in ("", "IMAGE", "VIDEO"):
                raise ValueError("type must be IMAGE or VIDEO")
            item = store.random_asset(kind)
            if item is None and not store.meta("catalog_at"):
                indexer.refresh_catalog()           # nothing has been read from Immich yet
                item = store.random_asset(kind)
            if item is None:
                raise aitagger_mod.NotFound(f"There is no {kind.lower() or 'photo'} in the library list.")
            return {"id": item["id"], "name": item["name"], "type": item["type"]}
        raise aitagger_mod.NotFound("Not found")

    def _aitagger_action(self, action: str, body: dict) -> dict:
        store, service, indexer = self._at()
        if action == "settings":
            changes = body.get("changes")
            if not isinstance(changes, dict) or not changes:
                raise ValueError("Nothing to change.")
            mode, scope = body.get("reprocess", "none"), body.get("scope", "outdated")
            if mode != "none":
                try:
                    mode = aitagger_mod.normalize_mode(mode)         # the v2 "describe" counts as "retag"
                except ValueError:
                    raise ValueError("reprocess must be none, retag or full") from None
            if scope not in ("outdated", "all"):
                raise ValueError("scope must be outdated or all")
            settings, changed = aitagger_mod.apply_settings({k: v for k, v in changes.items() if k != "indexing"}, store)
            queued = aitagger_mod.reprocess(store, scope, mode) if mode != "none" else 0
            if queued:
                indexer.start() if settings["indexing"] else indexer.poke()
            return {**self._aitagger_status(), "queued": queued, "changed": changed,
                    "suggest": aitagger_mod.suggest_mode(changed)}
        if action == "index":
            what = body.get("action")
            if what == "start":
                aitagger_mod.apply_settings({"indexing": True}, store)
                indexer.start()
            elif what == "pause":
                aitagger_mod.apply_settings({"indexing": False}, store)
                indexer.stop()
            elif what == "retry":
                store.retry_failed()
                indexer.start() if aitagger_mod.load_settings()["indexing"] else indexer.poke()
            elif what == "clear":
                store.clear_failed()
            else:
                raise ValueError("action must be start, pause, retry or clear")
            return self._aitagger_status()
        if action == "load":
            service.load()                      # starts the containers; they keep loading in the background
            return self._aitagger_status()
        if action == "unload":
            aitagger_mod.apply_settings({"indexing": False}, store)
            indexer.stop(wait_s=5)
            service.unload()
            return self._aitagger_status()
        if action in ("preview", "apply"):
            return indexer.test(self._uuid(str(body.get("id") or "")), write=action == "apply")
        if action == "reprocess":
            scope, ids = body.get("scope"), body.get("ids")
            if scope == "ids":
                ids = self._asset_ids(ids)
            queued = aitagger_mod.reprocess(store, str(scope or ""), str(body.get("mode") or ""), ids,
                                            body.get("tag") or "")
            if queued:
                indexer.start() if aitagger_mod.load_settings()["indexing"] else indexer.poke()
            return {**self._aitagger_status(), "queued": queued}
        if action == "remove":
            exclude = body.get("exclude", False)
            if not isinstance(exclude, bool):
                raise ValueError("exclude must be true or false")
            return indexer.remove(self._asset_ids(body.get("ids")), exclude)
        raise LookupError(action)

    # --------------------------------------------------------------- themes

    def _backend(self):
        return self.theme_backend or themes_mod.Backend()

    def _sp_engine(self):
        if self.theme_sp_engine is not None:
            return self.theme_sp_engine
        return themes_mod.SearchPlusEngine(parts=self._sp)

    @staticmethod
    def _public_theme(t: dict) -> dict:
        return {k: v for k, v in t.items() if k != "vectors"}

    def _schedule(self) -> dict:
        try:
            active = subprocess.run(["systemctl", "is-active", "immich-themes.timer"],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            nxt = subprocess.run(["systemctl", "show", "immich-themes.timer", "-p", "NextElapseUSecRealtime", "--value"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return {"active": False, "next": ""}
        return {"active": active == "active", "next": nxt}

    def _themes_list(self) -> dict:
        self._import_rules_once()
        return {"themes": [self._public_theme(t) for t in themes_mod.load_themes()], "schedule": self._schedule()}

    def _import_rules_once(self) -> None:
        """Rules became smart albums: bring the saved rules over the first time (the rules file is kept)."""
        store = themes_mod.load_store()
        if store.get("rulesImported"):
            return
        try:
            raw = self._read_rules_raw()
        except (OSError, ValueError, RuleError, ImportError):
            raw = {"rules": []}
        themes = themes_mod.load_themes()
        have = {(t["album"].casefold(), t["description"].casefold()) for t in themes}
        new = [t for t in themes_mod.import_rules(raw) if (t["album"].casefold(), t["description"].casefold()) not in have]
        themes_mod.save_themes(themes + new, rulesImported=True)

    def _theme_action(self, action: str, body: dict) -> dict:
        if action == "preview":
            limit = body.get("limit", 300)
            if not isinstance(limit, int) or not 1 <= limit <= 2000:
                raise ValueError("limit must be between 1 and 2000")
            spec = body.get("theme") or {"description": body.get("description"), "media": body.get("media")}
            if not isinstance(spec, dict):
                raise ValueError("theme must be an object")
            return themes_mod.preview(self.client, limit=limit, backend=self._backend(), spec=spec,
                                      engine=self._sp_engine())
        items = themes_mod.load_themes()
        by_id = {t["id"]: t for t in items}
        if action == "save":
            data = body.get("theme") or {}
            if not isinstance(data, dict):
                raise ValueError("theme must be an object")
            if data.get("id") in by_id:
                old = by_id[data["id"]]
                t = themes_mod.build_theme(data, old)
                if t["album"] != old["album"]:
                    t["albumId"] = None
                items = [t if x["id"] == old["id"] else x for x in items]
            else:
                t = themes_mod.build_theme(data)
                if any(x["album"].casefold() == t["album"].casefold() for x in items):
                    raise ValueError(f"Another smart album already fills the album {t['album']!r}.")
                items.append(t)
            themes_mod.save_themes(items)
            return {"theme": self._public_theme(t)}
        if action == "run":
            only = body.get("ids")
            if only is not None and (not isinstance(only, list) or not all(isinstance(i, str) for i in only)):
                raise ValueError("ids must be a list")
            return {"results": themes_mod.run_all(self.client, only=only, backend=self._backend(),
                                                  engine=self._sp_engine())}
        theme_id = str(body.get("id") or "")
        if theme_id not in by_id:
            raise ValueError("Unknown theme.")
        if action == "forget":
            themes_mod.forget_removals(theme_id)
            return {"ok": True}
        if action == "delete":
            t = by_id[theme_id]
            deleted = None
            if body.get("deleteAlbum") and t.get("albumId"):
                try:
                    deleted = albums_mod.delete_albums(self.client, [t["albumId"]])
                except ImmichError:
                    deleted = None
            themes_mod.save_themes([x for x in items if x["id"] != theme_id])
            themes_mod.forget_removals(theme_id)
            return {"deleted": t["name"], "albumDeleted": bool(deleted)}
        raise LookupError(action)

    # --------------------------------------------------------------- albums

    def _album_items(self, album_id: str, sort: str, media: str = "", q: str = "") -> dict:
        items = albums_mod.album_items(self.client, album_id)
        if media in ("IMAGE", "VIDEO"):
            items = [i for i in items if i.get("type") == media]
        if sort == "relevance":
            return self._album_by_relevance(album_id, items, q)
        added = None
        if sort.startswith("added"):
            added = albums_mod.added_to_album_times(album_id)
            if added is None:
                sort = "taken_desc"
            else:
                for item in items:
                    item["added"] = added.get(item["id"], "")
        key, reverse = {
            "taken_desc": ("taken", True), "taken_asc": ("taken", False),
            "added_desc": ("added", True), "added_asc": ("added", False),
            "uploaded_desc": ("uploaded", True), "uploaded_asc": ("uploaded", False),
            "name_asc": ("name", False),
        }.get(sort, ("taken", True))
        # Within one "added" batch everything shares a timestamp: fall back to date taken.
        items.sort(key=lambda i: ((i.get(key) or ""), i.get("taken") or ""), reverse=reverse)
        return {"items": items, "sort": sort, "addedAvailable": added is not None if sort.startswith("added") else None}

    def _album_by_relevance(self, album_id: str, items: list[dict], q: str) -> dict:
        """Most to least relevant to a description: the same similarity score smart search ranks by."""
        q = (q or "").strip()
        if not q:
            album = next((a for a in self.client.list_albums() if a.get("id") == album_id), {})
            theme = next((t for t in themes_mod.load_themes() if t.get("albumId") == album_id), None)
            q = (theme or {}).get("description") or album.get("albumName") or ""
        if not q:
            raise ValueError("Type what the photos should look like to sort by relevance.")
        backend = self._backend()
        scores = backend.album_scores(backend.text_vector(q, backend.model_name(self.client)), album_id)
        for item in items:
            item["score"] = round(scores[item["id"]], 4) if item["id"] in scores else None
        items.sort(key=lambda i: (i["score"] is not None, i["score"] or 0), reverse=True)
        return {"items": items, "sort": "relevance", "query": q,
                "unscored": sum(1 for i in items if i["score"] is None)}

    def _album_action(self, action: str, body: dict) -> dict:
        def ids(name: str) -> list[str]:
            value = body.get(name) or []
            if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
                raise ValueError(f"{name} must be a list of ids")
            return value

        if action == "transfer":
            return albums_mod.transfer(self.client, str(body.get("sourceId") or ""), ids("assetIds"),
                                       str(body.get("target") or "").strip(), move=bool(body.get("move")))
        if action == "remove":
            return albums_mod.remove_photos(self.client, str(body.get("albumId") or ""), ids("assetIds"))
        if action == "merge":
            return albums_mod.merge(self.client, ids("sourceIds"), str(body.get("target") or "").strip(),
                                    delete_sources=bool(body.get("deleteSources", True)),
                                    replace_target=bool(body.get("replaceTarget")))
        if action == "delete":
            if not ids("albumIds"):
                raise ValueError("Pick at least one album.")
            return albums_mod.delete_albums(self.client, ids("albumIds"))
        if action == "rename":
            return albums_mod.rename(self.client, str(body.get("albumId") or ""), str(body.get("name") or ""))
        if action == "create":
            return albums_mod.create(self.client, str(body.get("name") or ""))
        raise LookupError(action)

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
        if size not in ("thumbnail", "preview", "fullsize"):
            size = "thumbnail"
        try:
            payload, content_type = self.client.thumbnail(asset_id, size=size)
        except ImmichError as exc:
            return self._error(502, str(exc))
        # Thumbnails are immutable for a given asset id, so let the phone cache them.
        self._send(200, payload, content_type, cache="private, max-age=3600")

    @staticmethod
    def _uuid(value: str) -> str:
        import uuid as _uuid
        try:
            return str(_uuid.UUID(value))
        except ValueError:
            raise ValueError("Not an asset id.") from None

    @staticmethod
    def _seconds(duration) -> float | None:
        """A video's length in seconds: Immich v3 sends milliseconds, older versions "H:MM:SS.fffff"."""
        if duration in (None, "") or isinstance(duration, bool):
            return None
        if isinstance(duration, (int, float)):
            return round(duration / 1000.0, 2) if duration > 0 else None
        try:
            h, m, s = str(duration).split(":")
            secs = int(h) * 3600 + int(m) * 60 + float(s)
        except ValueError:
            return None
        return round(secs, 2) if secs > 0 else None

    def _asset_info(self, asset_id: str) -> dict:
        a = self.client.get_asset(self._uuid(asset_id))
        exif = a.get("exifInfo") or {}
        return {
            "id": a.get("id"), "name": a.get("originalFileName"), "type": a.get("type"),
            "taken": a.get("localDateTime") or a.get("fileCreatedAt"), "duration": self._seconds(a.get("duration")),
            "width": exif.get("exifImageWidth") or a.get("width"), "height": exif.get("exifImageHeight") or a.get("height"),
            "size": exif.get("fileSizeInByte"), "description": exif.get("description") or "",
            "mime": a.get("originalMimeType"), "favorite": a.get("isFavorite"),
        }

    def _serve_media(self, asset_id: str, query: dict[str, list[str]]) -> None:
        """Stream a video (playable version) or an original file, passing Range through so seeking works.

        Every request leaves one line in the log: the range asked for, how much was sent and how fast, and
        where the time went -- waiting for the phone to take the data (the network) or waiting for Immich.
        The app sends X-Playback-Id so a line can be matched to the player's own log on the phone.
        """
        asset_id = self._uuid(asset_id)
        kind = (query.get("kind") or ["original"])[0]
        path = f"/assets/{asset_id}/video/playback" if kind == "video" else f"/assets/{asset_id}/original"
        headers = {"x-api-key": self.client.api_key, "Accept": "*/*", "User-Agent": "immich-organizer/1.0"}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        tag = f"media {kind} {asset_id[:8]}"
        if self.headers.get("X-Playback-Id"):
            tag += f" [{self.headers['X-Playback-Id'][:24]}]"
        asked = self.headers.get("Range") or "whole file"
        started = time.monotonic()
        req = urllib.request.Request(self.client._url(path), headers=headers)  # noqa: SLF001
        try:
            resp = urllib.request.urlopen(req, timeout=60, context=self.client._ssl_context)  # noqa: SLF001
        except urllib.error.HTTPError as exc:
            _log(f"{tag} {asked} -> Immich answered {exc.code}")
            return self._error(exc.code if exc.code in (404, 416) else 502, f"Immich answered {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            _log(f"{tag} {asked} -> could not reach Immich: {exc}")
            return self._error(502, f"Could not reach Immich: {exc}")
        first_byte_ms = (time.monotonic() - started) * 1000
        with resp:
            self.send_response(resp.status)
            for name in ("Content-Type", "Content-Length", "Content-Range", "Last-Modified", "ETag"):
                if resp.headers.get(name):
                    self.send_header(name, resp.headers[name])
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=3600")
            if (query.get("download") or [""])[0]:
                name = (query.get("name") or ["download"])[0].replace('"', "")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            if self.command == "HEAD":
                return
            sent, reading, writing, ended = 0, 0.0, 0.0, "complete"
            try:
                while True:
                    t0 = time.monotonic()
                    chunk = resp.read(256 * 1024)
                    t1 = time.monotonic()
                    reading += t1 - t0
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    writing += time.monotonic() - t1
                    sent += len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                ended = "phone closed it"   # the player seeked or left the video; normal
                self.close_connection = True
            except OSError as exc:          # e.g. Immich stopped sending (timeout)
                ended = f"failed: {type(exc).__name__}: {exc}"
                self.close_connection = True  # the body is cut short, so this connection can't be reused
            finally:
                total = time.monotonic() - started
                length = resp.headers.get("Content-Length") or ""
                of = f" of {int(length) / 1e6:.1f}" if length.isdigit() else ""
                _log(f"{tag} {asked} -> {resp.status}, sent {sent / 1e6:.1f}{of} MB in {total:.1f} s "
                     f"({_rate(sent, total)}); waited on phone {writing:.1f} s, on Immich {reading:.1f} s "
                     f"(first byte {first_byte_ms:.0f} ms); {ended}")

    def _serve_speed_test(self, query: dict[str, list[str]]) -> None:
        """Send ``mb`` MB of random bytes, so the app can time the phone <-> panel link without Immich involved."""
        try:
            mb = max(1, min(int((query.get("mb") or ["8"])[0]), 64))
        except ValueError:
            raise ValueError("mb must be a whole number.") from None
        size = mb * 1_000_000
        block = memoryview(os.urandom(250_000))   # random, so nothing on the way can compress it
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        started, sent, ended = time.monotonic(), 0, "complete"
        try:
            while sent < size:
                n = min(len(block), size - sent)
                self.wfile.write(block[:n])
                sent += n
        except (BrokenPipeError, ConnectionResetError):
            ended = "phone closed it"
            self.close_connection = True
        total = time.monotonic() - started
        _log(f"speed test {mb} MB: sent {sent / 1e6:.1f} MB in {total:.1f} s ({_rate(sent, total)}); {ended}")

    def _serve_person_thumb(self, person_id: str) -> None:
        try:
            payload, content_type = self.client.person_thumbnail(person_id)
        except ImmichError as exc:
            return self._error(502, str(exc))
        self._send(200, payload, content_type, cache="private, max-age=3600")

    def _people(self) -> dict:
        """Named, visible people for the picker (unnamed ones have nothing to type)."""
        everyone = self.client.list_people()
        named = sorted(
            ({"id": p["id"], "name": p["name"]} for p in everyone if p.get("name") and not p.get("isHidden")),
            key=lambda p: p["name"].casefold(),
        )
        return {"people": named, "unnamed": sum(1 for p in everyone if not p.get("name"))}

    # ----------------------------------------------------------- faces review

    def _quality_path(self) -> Path:
        return state_dir() / "face-quality.json"

    def _read_quality(self) -> dict | None:
        try:
            return json.loads(self._quality_path().read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def _faces(self) -> dict:
        """Unnamed people with their sharpness, blurriest first."""
        quality = self._read_quality()
        people = self.client.list_people(with_hidden=True)
        if quality is None:
            return {"measured": False, "people": [],
                    "unnamed": sum(1 for p in people if not p.get("name"))}
        scores = quality.get("people") or {}
        out = []
        for person in people:
            if person.get("name"):
                continue
            q = scores.get(person["id"])
            out.append({
                "id": person["id"],
                "hidden": bool(person.get("isHidden")),
                "faces": q["faces"] if q else 0,
                "best": q["best"] if q else None,
                "median": q["median"] if q else None,
            })
        out.sort(key=lambda p: (p["best"] is None, p["best"] if p["best"] is not None else 0))
        return {"measured": True, "generated": quality.get("generated"),
                "method": quality.get("method"), "people": out}

    def _faces_measure(self) -> dict:
        try:
            from .. import face_quality
        except ImportError as exc:  # pragma: no cover - import is stdlib-only
            raise ValueError(str(exc)) from exc
        try:
            result = face_quality.run(self._quality_path(), progress=lambda _m: None)
        except ImportError:
            raise ValueError("Measuring faces needs numpy and Pillow on the server "
                             "(apt install python3-numpy python3-pil).")
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            raise ValueError(f"Could not measure faces: {exc}")
        return {"faces": result["faces"], "people": len(result["people"])}

    def _faces_visibility(self, body: dict) -> dict:
        ids = body.get("ids") or []
        hidden = body.get("hidden")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or not ids:
            raise ValueError("ids must be a non-empty list of person ids")
        if not isinstance(hidden, bool):
            raise ValueError("hidden must be true or false")
        ok = failed = 0
        for i in range(0, len(ids), 200):
            res = self.client.update_people([{"id": p, "isHidden": hidden} for p in ids[i:i + 200]])
            ok += sum(1 for r in res if r.get("success"))
            failed += sum(1 for r in res if not r.get("success"))
        return {"changed": ok, "failed": failed, "hidden": hidden}

    def _faces_covers(self) -> dict:
        """Point every *unnamed* person's cover at their sharpest face.

        Named people are left alone -- their cover may have been chosen by hand.
        """
        quality = self._read_quality()
        if quality is None:
            raise ValueError("Measure the faces first.")
        scores = quality.get("people") or {}
        unnamed = [p["id"] for p in self.client.list_people(with_hidden=True) if not p.get("name")]
        items = [{"id": pid, "featureFaceAssetId": scores[pid]["bestAssetId"]}
                 for pid in unnamed if pid in scores and scores[pid].get("bestAssetId")]
        ok = failed = 0
        for i in range(0, len(items), 200):
            res = self.client.update_people(items[i:i + 200])
            ok += sum(1 for r in res if r.get("success"))
            failed += sum(1 for r in res if not r.get("success"))
        return {"changed": ok, "failed": failed}

    # ------------------------------------------------- remembered view choices
    # Shared by the web panel and the app, so a sort picked in one is there in the other.
    PREF_CHOICES = {
        "albumsSort": ("name", "small", "big", "updated"),
        "albumSort": ("taken_desc", "taken_asc", "added_desc", "added_asc", "uploaded_desc", "uploaded_asc", "name_asc"),
        "searchEngine": ("immich", "searchplus"),
    }
    PREF_DEFAULTS = {"albumsSort": "name", "albumSort": "taken_desc", "searchEngine": "immich"}

    def _prefs_path(self) -> Path:
        return state_dir() / "ui-prefs.json"

    def _prefs(self) -> dict:
        try:
            saved = json.loads(self._prefs_path().read_text("utf-8"))
        except (OSError, ValueError):
            saved = {}
        return {k: saved.get(k) if saved.get(k) in self.PREF_CHOICES[k] else v for k, v in self.PREF_DEFAULTS.items()}

    def _save_prefs(self, body: dict) -> dict:
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("Nothing to change.")
        prefs = self._prefs()
        for key, value in changes.items():
            if key not in self.PREF_CHOICES or value not in self.PREF_CHOICES[key]:
                raise ValueError(f"Unknown choice for {key}: {value!r}")
            prefs[key] = value
        path = self._prefs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs), "utf-8")
        tmp.replace(path)
        return prefs

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

    # ------------------------------------------------------------ rules file

    def _read_rules_raw(self) -> dict:
        """The rules file as plain data, so the panel can edit it field by field."""
        path = self.rules_path
        if not path or not path.exists():
            return {"version": 1, "rules": []}
        text = path.read_text("utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text or "{}")
        if not isinstance(data, dict):
            raise RuleError("the rules file must be a mapping")
        return data

    def _write_rules_raw(self, data: dict) -> None:
        path = self.rules_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.with_name(path.name + ".bak").write_text(path.read_text("utf-8"), "utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml
            text = RULES_HEADER + "\n" + yaml.safe_dump(
                data, sort_keys=False, allow_unicode=True, default_flow_style=False
            )
        else:
            text = json.dumps(data, indent=2)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(path)

    def _rules_payload(self) -> dict:
        if not self.rules_path:
            return {"loaded": False, "rules": []}
        try:
            raw = self._read_rules_raw()
            ruleset = parse_ruleset(raw, source=self.rules_path)
        except Exception as exc:  # RuleError, YAML errors
            return {"loaded": False, "error": str(exc), "rules": [], "path": str(self.rules_path)}
        raw_rules = _plain(raw.get("rules") or [])
        names: dict[str, str] = {}
        if any(r.filters.get("personIds") for r in ruleset.rules):
            try:
                names = {p["id"]: p.get("name") or "(unnamed)" for p in self.client.list_people(with_hidden=True)}
            except ImmichError:
                names = {}
        return {
            "loaded": True,
            "path": str(self.rules_path),
            "defaults": _plain(raw.get("defaults") or {}),
            "rules": [
                {
                    "name": r.name,
                    "album": r.album,
                    "match": r.describe_match(),
                    "limit": r.limit,
                    "enabled": r.enabled,
                    "excludeAlbums": r.exclude_albums,
                    "people": [names.get(i, i) for i in (r.filters.get("personIds") or [])],
                    "raw": raw_rules[i] if i < len(raw_rules) else {},
                }
                for i, r in enumerate(ruleset.rules)
            ],
        }

    def _save_rules(self, body: dict) -> dict:
        if not self.rules_path:
            raise ValueError("No rules file is configured. Start `serve` with --rules.")
        rules = body.get("rules")
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")
        data = self._read_rules_raw() if self.rules_path.exists() else {"version": 1}
        data = {"version": data.get("version", 1), **({"defaults": data["defaults"]} if data.get("defaults") else {}),
                "rules": rules}
        parse_ruleset(data, source=self.rules_path)  # validate before touching the file
        self._write_rules_raw(data)
        return self._rules_payload()

    # --------------------------------------------------------------- history

    def _undone_path(self) -> Path:
        return state_dir() / "undone.json"

    def _undone(self) -> set[str]:
        try:
            return set(json.loads(self._undone_path().read_text("utf-8")))
        except (OSError, ValueError):
            return set()

    def _history(self) -> dict:
        undone = self._undone()
        runs = []
        for record in reversed(read_journal(limit=30)):
            added = {k: len(v) for k, v in (record.get("added") or {}).items()}
            removed = {
                (info.get("name") or k): len(info.get("ids") or [])
                for k, info in (record.get("removed") or {}).items()
            }
            runs.append({
                "runId": record.get("run_id"),
                "timestamp": record.get("timestamp"),
                "note": record.get("note") or ("rules" if record.get("source") != "panel" else ""),
                "added": added,
                "removed": removed,
                "deleted": [d.get("name") for d in record.get("deleted_albums") or []],
                "renamed": list((record.get("renamed") or {}).values()),
                "undone": record.get("run_id") in undone,
            })
        return {"runs": runs}

    def _undo(self, body: dict) -> dict:
        run_id = body.get("runId")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("runId is required")
        matches = [r for r in read_journal(limit=500) if r.get("run_id") == run_id]
        if not matches:
            raise ValueError(f"No change with id {run_id!r} in the history.")
        if run_id in self._undone():
            raise ValueError("That change was already undone.")
        removed, failures = undo_run(self.client, matches[-1])
        restored = sum(len(i.get("ids") or []) for i in (matches[-1].get("removed") or {}).values())
        undone = self._undone() | {run_id}
        path = self._undone_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(undone)), "utf-8")
        return {"runId": run_id, "removed": removed, "restored": restored, "failures": failures[:20]}

    def _build_ad_hoc_rule(self, body: dict) -> Rule:
        query = (body.get("query") or "").strip() or None
        like = (body.get("like") or "").strip() or None
        people = body.get("people") or []
        if not isinstance(people, list) or not all(isinstance(i, str) for i in people):
            raise ValueError("people must be a list of person ids")
        if query and like:
            raise ValueError("Provide either a text query or a reference asset, not both.")
        if not query and not like and not people:
            raise ValueError("Describe what you want, paste a reference photo, or pick people.")

        limit = body.get("limit", 60)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be a whole number between 1 and {MAX_LIMIT}")

        raw_filters = body.get("filters") or {}
        if not isinstance(raw_filters, dict):
            raise ValueError("filters must be an object")
        if people:
            raw_filters = dict(raw_filters, person_ids=people)

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
        match = body.get("peopleMatch") or "all"
        if match not in ("all", "any"):
            raise ValueError("peopleMatch must be 'all' or 'any'")
        rule.people_match = match
        return rule

    def _search(self, body: dict) -> dict:
        if body.get("mode") == "tags":
            raise ValueError("Search by tags has been removed.")
        engine = body.get("engine") or "immich"
        if engine not in themes_mod.ENGINES:
            raise ValueError("engine must be immich or searchplus")
        rule = self._build_ad_hoc_rule(body)
        if engine == "searchplus":
            return self._search_searchplus(body, rule)

        exclude_albums = body.get("excludeAlbums") or []
        skip_ids = body.get("skipIds") or []
        if not isinstance(exclude_albums, list) or not all(isinstance(a, str) for a in exclude_albums):
            raise ValueError("excludeAlbums must be a list of album names or ids")
        if not isinstance(skip_ids, list) or not all(isinstance(a, str) for a in skip_ids):
            raise ValueError("skipIds must be a list of asset ids")

        exclude, unknown = excluded_asset_ids(self.client, exclude_albums)
        exclude |= set(skip_ids)
        matches = evaluate_rule(self.client, rule, exclude_ids=exclude)
        return {
            "match": rule.describe_match(),
            "count": len(matches),
            "excluded": len(exclude),
            "unknownAlbums": unknown,
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

    SP_SEARCH_FILTERS = ("type", "taken_after", "taken_before", "only_unfiled")

    def _search_searchplus(self, body: dict, rule) -> dict:
        """The Search tab with the Search+ model: same filters, scores from the Search+ index."""
        filters = body.get("filters") or {}
        extra = sorted(k for k, v in filters.items() if v not in (None, "", False) and k not in self.SP_SEARCH_FILTERS)
        if extra:
            raise ValueError(f"{', '.join(extra)} can't be used with the Search+ model.")
        exclude_albums = body.get("excludeAlbums") or []
        skip_ids = body.get("skipIds") or []
        if not isinstance(exclude_albums, list) or not all(isinstance(a, str) for a in exclude_albums):
            raise ValueError("excludeAlbums must be a list of album names or ids")
        if not isinstance(skip_ids, list) or not all(isinstance(a, str) for a in skip_ids):
            raise ValueError("skipIds must be a list of asset ids")
        exclude, unknown = excluded_asset_ids(self.client, exclude_albums)
        exclude |= set(skip_ids)
        if rule.like_asset:
            exclude.add(rule.like_asset)               # the reference photo itself isn't a result
        theme = themes_mod.build_theme({
            "source": "text" if rule.query else "like" if rule.like_asset else "none",
            "description": rule.query or "", "like": rule.like_asset, "engine": "searchplus",
            "mode": "top", "limit": min(rule.limit, 10000), "cutoff": themes_mod.SP_TERM_CUTOFF,
            "media": filters.get("type") or None, "people": body.get("people") or [],
            "people_match": rule.people_match, "taken_after": filters.get("taken_after"),
            "taken_before": filters.get("taken_before"), "only_unfiled": bool(filters.get("only_unfiled")),
            "all_of": rule.refine.all_of, "none_of": rule.refine.none_of,
        })
        engine = self._sp_engine()
        rows, _ = themes_mod.searchplus_select(self.client, theme, self._backend(), engine)
        kept = [(i, sc) for i, sc in rows if i not in exclude]
        view = engine.view()

        def info(aid, score):
            p = view.pos.get(aid)
            return {"id": aid, "name": view.names[p] if p is not None else "",
                    "type": str(view.types[p]) if p is not None else "", "score": round(score, 4) if score is not None else None,
                    "date": str(view.taken[p]) if p is not None else ""}

        what = f'"{rule.query}"' if rule.query else f"like {rule.like_asset}" if rule.like_asset else "people"
        return {
            # no "total": every indexed photo gets a score, so "of 72,000" would only confuse
            "match": f"Search+ model · {what}", "count": min(len(kept), rule.limit),
            "excluded": len(rows) - len(kept), "unknownAlbums": unknown, "engine": "searchplus",
            "assets": [info(i, sc) for i, sc in kept[:rule.limit]],
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
        return file_assets(
            self.client,
            asset_ids,
            album_name,
            move=bool(body.get("move")),
            create_album=bool(body.get("createAlbum", True)),
            archive=bool(body.get("archive")),
            favorite=bool(body.get("favorite")),
        )

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
    searchplus_mod.autostart()            # resume building the Search+ index if it was on
    aitagger_mod.autostart(client)        # ... and tagging with the AI Tagger

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
