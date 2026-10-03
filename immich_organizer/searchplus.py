"""Search+ (experimental): a second, stronger "search by description" index.

Immich's smart search compares your words with one vector per photo made by
its CLIP model. Search+ keeps its own vectors, made by a bigger model (Meta's
Perception Encoder, PE-Core G/14 at 448 px), and for videos and GIFs it
stores several frames instead of one thumbnail, so a scene in the middle of a
clip can be found. Nothing in Immich is changed.

Pieces:
* the model server (``embed_service.py``) runs in its own GPU container, started on demand and
  stopping by itself after a quiet spell;
* ``Store``: the vectors (one float16 row per photo / frame) in ``vectors.f16`` plus a small
  SQLite catalogue saying which rows belong to which asset;
* ``Indexer``: a background thread that reads previews / video frames and asks the model server
  for their vectors; it resumes where it stopped;
* ``search``: scores every row against the query (numpy), keeps each asset's best frame.

Keeping the library up to date, and the graphics card free (the AI Tagger imports these helpers from here):
* the indexer asks Immich's database a cheap question (``fetch_probe``: how many live assets, the newest upload, the
  newest preview file) every ``check_every`` minutes and reads the whole library list only when the answer changed,
  plus once an hour as a safety net (``CatalogWatch``). A new upload is not touched until Immich has made its preview
  (it is held back for up to ``PREVIEW_GRACE`` seconds; after that it is processed without one, as before);
* a model is never kept loaded "just in case": when the indexer has nothing left to do and the model server has been
  idle for ``unload_after`` minutes, the panel stops the container (``IdleLoop``, ``unload_status``). Interactive
  use (a search, the AI Tagger's Test card) first earns the model the server's own idle time (20 minutes), which stays
  the backstop everywhere else (``IDLE_EXIT_MINUTES`` in the model server).
"""

from __future__ import annotations

import base64
import collections
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from pathlib import Path

from .config import state_dir

MODEL_LABEL = "PE-Core G/14 · 448 px (Meta Perception Encoder)"
SERVICE_URL = os.environ.get("SEARCHPLUS_URL", "http://127.0.0.1:11439")
CONTAINER = os.environ.get("SEARCHPLUS_CONTAINER", "immich_searchplus")
COMPOSE = Path(__file__).resolve().parent.parent / "deploy" / "searchplus" / "docker-compose.yml"
AITAGGER_CONTAINER = os.environ.get("AITAGGER_CONTAINER", "immich_aitagger")   # the AI Tagger's container (its only GPU user)
# Whether Search+ and the AI Tagger take turns on the graphics card. True: Search+ will not start while the tagger
# container runs (GpuBusy), and the AI Tagger stops a running Search+ before it starts its container.
# False: they may run at the same time (set it when the card has room for both); nothing is stopped or refused.
AITAGGER_EXCLUSIVE = False
# unload_after: minutes the model server must be idle (and nothing left to index) before the panel stops it;
# check_every: minutes between the cheap "did anything change in Immich?" looks. Whole minutes, 1-60 each.
DEFAULTS = {"indexing": False, "keep_updated": True, "video_frames": 4, "unload_after": 2, "check_every": 1}
LIMITS = {"video_frames": (1, 8), "unload_after": (1, 60), "check_every": (1, 60)}
FRAME_SIDE = 640              # frames are sent at most this big; the model looks at 448 x 448
MAX_ATTEMPTS = 3
CLEARED = 99                  # attempts value for failures the user cleared from the list
RETRY_AFTER = 900             # seconds before a photo that failed is tried again (e.g. its preview was not made yet)
STATE_TTL = 5.0               # seconds a container's state is remembered (the status route is polled)
SAFETY_SYNC = 3600            # the library list is read in full at least this often, whatever the probe says
PREVIEW_GRACE = 1800          # seconds Immich gets to make a new asset's preview; an asset without one is held back till then
IDLE_POLL = 60                # an indexer with nothing to do looks again at least this often (the unload check runs then)
IDLE_EXIT_DEFAULT_MIN = 20    # the model servers' own idle exit (IDLE_EXIT_MINUTES), when /health does not say


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat(timespec="seconds")


def _wall() -> float:
    return time.time()


def preview_cutoff() -> int:
    """Epoch seconds: an asset Immich added after this that has no preview file yet is not worked on (see ``READY``)."""
    return int(_wall() - PREVIEW_GRACE)


# What the work list leaves out: an asset Immich has not finished, i.e. no preview file yet and added less than
# ``PREVIEW_GRACE`` ago (``added`` 0 = unknown, never held back). One ``?`` in it, ``preview_cutoff()``. Both stores use it.
READY = "(coalesce(a.preview,'') <> '' or coalesce(a.added,0) = 0 or a.added < ?)"


def home() -> Path:
    path = state_dir() / "searchplus"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- settings

def settings_path() -> Path:
    return home() / "settings.json"


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    out = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    for key, (lo, hi) in LIMITS.items():            # a hand-edited value out of range falls back to the default
        try:
            out[key] = int(out[key])
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
        if not lo <= out[key] <= hi:
            out[key] = DEFAULTS[key]
    return out


def save_settings(changes: dict) -> dict:
    settings = load_settings()
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError(f"Unknown Search+ setting: {key}")
        if key in LIMITS:
            lo, hi = LIMITS[key]
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number") from None
            if not lo <= value <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
        else:
            value = bool(value)
        settings[key] = value
    tmp = settings_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=1), "utf-8")
    tmp.replace(settings_path())
    return settings


# ---------------------------------------------------------------- the library list

def _psql(sql: str, timeout: float = 600) -> str:
    """Run one query in Immich's database (``docker exec immich_postgres psql``): rows end in \\x1e, fields in \\x1f."""
    return subprocess.run(
        ["docker", "exec", "-i", "immich_postgres", "psql", "-U", "postgres", "-d", "immich", "-At", "-F", "\x1f",
         "-R", "\x1e"], input=sql, capture_output=True, text=True, timeout=timeout, check=True).stdout


# What the library list shows: live photos and videos of the timeline and the archive.
_LIVE = """a."deletedAt" is null and a.visibility in ('timeline','archive') and a.type in ('IMAGE','VIDEO')"""

# The cheap change probe: five numbers that move when a photo is uploaded, deleted, trashed / restored, archived or
# hidden, or when Immich makes (or drops) a preview. Columns checked against the live schema (Immich 2.x):
# ``asset."createdAt"`` is set once when the row is made and ``asset."deletedAt"`` when it is trashed, while
# ``asset."updatedAt"`` / ``updateId`` also move whenever anything is written to the asset (the AI Tagger writes every
# description), so they would make the probe say "changed" all the time. ``asset_file."createdAt"`` is the time the
# preview file's row was made (its ``updatedAt`` moves when a preview is regenerated: not a change for us).
PROBE_SQL = f"""select
  (select count(*) from asset a where {_LIVE}),
  (select coalesce(max(a."createdAt")::text, '') from asset a where {_LIVE}),
  (select coalesce(max(a."deletedAt")::text, '') from asset a),
  (select count(*) from asset_file f where f.type = 'preview'),
  (select coalesce(max(f."createdAt")::text, '') from asset_file f where f.type = 'preview')"""


def fetch_probe() -> tuple:
    """Immich's change signature: one small query. Two equal signatures mean the library list did not change."""
    out = _psql(PROBE_SQL, timeout=60)
    parts = tuple(out.split("\x1e")[0].strip("\n").split("\x1f"))
    if len(parts) != 5:
        raise RuntimeError(f"unexpected answer to the change probe: {out[:100]!r}")
    return parts


def fetch_catalog() -> list[dict]:
    """Every photo and video Immich shows (timeline + archive), with host paths to its files. ``added`` is when
    Immich made the asset (epoch seconds): a new asset without a preview yet is held back for a while."""
    sql = f"""select a.id, a.type, coalesce(p.path,''), a."originalPath", coalesce(a.duration,0),
                    coalesce(a."localDateTime"::text, a."fileCreatedAt"::text, ''), a."originalFileName",
                    coalesce(extract(epoch from a."createdAt")::bigint, 0)
             from asset a
             left join asset_file p on p."assetId" = a.id and p.type = 'preview'
             where {_LIVE}"""
    out = _psql(sql)
    mounts = json.loads(subprocess.run(["docker", "inspect", "immich_server", "--format", "{{json .Mounts}}"],
                                       capture_output=True, text=True, timeout=30, check=True).stdout)
    outside = next(m["Source"] for m in mounts if m.get("Destination") == "/data")

    def host(path: str) -> str:
        return outside + path[len("/data"):] if path.startswith("/data") else path

    rows = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) != 8:
            continue
        aid, typ, preview, original, duration, taken, name, added = parts
        rows.append({"id": aid, "type": typ, "preview": host(preview), "original": host(original),
                     "duration_ms": int(duration or 0), "taken": taken, "name": name, "added": int(added or 0)})
    return rows


class CatalogWatch:
    """Decides when the library list has to be read in full. Every ``check_every`` minutes it runs ``probe`` (cheap);
    the list is read when the answer differs from the one at the last full read, when there has been no full read
    yet, and at least every ``SAFETY_SYNC`` seconds whatever the probe says (a changed date, an asset moved between
    timeline and archive by a path the probe does not see...). A probe that fails changes nothing: the safety read
    covers it. Without a probe (``None``: an injected catalogue in a test) every look counts as a change, which makes
    ``check_every`` the old fixed re-read period."""

    def __init__(self, probe, clock=time.monotonic):
        self.probe, self.clock = probe, clock
        self.signature = None           # the probe's answer as of the last full read
        self.seen = None                # the probe's answer at the look that asked for the current read
        self.probed_at: float | None = None
        self.force = False              # look again at the next chance (Start / Try again on a waiting indexer)

    def observe(self):
        """The probe's answer now, or None (no probe, or it failed)."""
        if self.probe is None:
            return None
        try:
            return self.probe()
        except Exception:  # noqa: BLE001 - docker hiccup: the safety read is the net
            return None

    def due(self, last_sync, check_every: int) -> str:
        """Why the library list should be read in full now ("first", "safety", "changed"), or "" (not now)."""
        now = self.clock()
        every = max(int(check_every), 1) * 60
        if last_sync is None:
            self.seen = self.observe()
            self.probed_at = now
            return "first"
        if now - last_sync >= SAFETY_SYNC:
            self.seen = self.observe()
            self.probed_at = now
            return "safety"
        if not (self.force or self.probed_at is None or now - self.probed_at >= every):
            return ""
        self.force, self.probed_at = False, now
        if self.probe is None:
            return "changed"                    # nothing to compare with: every look is a full read
        self.seen = self.observe()
        return "changed" if self.seen is not None and self.seen != self.signature else ""

    def synced(self, seen) -> None:
        """A full read was done; ``seen`` is the probe's answer taken just before it (None: unknown)."""
        self.signature = seen

    def next_in(self, check_every: int) -> float:
        """Seconds until the next look."""
        if self.probed_at is None:
            return 0.0
        return max(max(int(check_every), 1) * 60 - (self.clock() - self.probed_at), 0.0)


# ---------------------------------------------------------------- frames

def video_frames(path: str, duration_ms: int, n: int, side: int = FRAME_SIDE,
                 positions: list[float] | None = None) -> list[bytes]:
    """``n`` frames spread over the video, or the frames at ``positions`` (fractions 0-1 of its length)."""
    secs = max(duration_ms / 1000.0, 0.1)
    frames = []
    for p in positions or [(i + 0.5) / n for i in range(n)]:
        t = secs * p
        try:
            data = subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
                 "-vf", f"scale='min({side},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-"],
                capture_output=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            data = b""
        if data:
            frames.append(data)
    return frames


def prepare_frames(item: dict, video_n: int) -> tuple[list[bytes], str]:
    """The pictures that stand for one asset: a photo's preview, or frames spread over a video / GIF."""
    from .media import ANIMATED_EXT, animation_frames, shrink_image

    original = item.get("original") or ""
    if item["type"] == "VIDEO" and original and os.path.exists(original):
        frames = video_frames(original, item.get("duration_ms") or 0, video_n)
        if frames:
            return frames, "video"
    if item["type"] == "IMAGE" and original.lower().endswith(ANIMATED_EXT) and os.path.exists(original):
        frames = animation_frames(original, video_n)
        if len(frames) > 1:
            return [shrink_image(f, FRAME_SIDE) for f in frames], "animation"
    preview = item.get("preview")
    if preview and os.path.exists(preview):
        with open(preview, "rb") as fh:
            return [shrink_image(fh.read(), FRAME_SIDE)], "video" if item["type"] == "VIDEO" else "image"
    # Immich has no preview for it: use the file itself when it is a picture we can read
    if not original or not os.path.exists(original):
        raise FileNotFoundError("the file is gone and Immich has no preview of it")
    if item["type"] == "IMAGE":
        data, problem = _picture(original)
        if data:
            return [data], "image"
        if problem == "truncated":
            raise ValueError("damaged picture: the file is cut short (an incomplete copy), and Immich has no preview")
        raise ValueError("not a picture: Immich has no preview and the file can't be read as an image "
                         f"({_what_is(original)})")
    raise ValueError(f"not a playable video: ffmpeg can't open it and Immich has no preview ({_what_is(original)})")


def _picture(path: str, side: int = FRAME_SIDE) -> tuple[bytes | None, str]:
    """(the file as a small JPEG, "") or (None, why): "truncated" or "unreadable"."""
    import io

    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.format == "JPEG":
                im.draft("RGB", (side, side))
            im = im.convert("RGB")
            im.thumbnail((side, side))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=90)
            return out.getvalue(), ""
    except Exception as exc:  # noqa: BLE001
        return None, "truncated" if "truncated" in str(exc).lower() else "unreadable"


def _what_is(path: str) -> str:
    """A short guess at what a file that isn't media really is (from its first bytes)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return "unreadable file"
    name = path.lower()
    if name.endswith(".ts") and head[:1] != b"\x47":
        return "a TypeScript code file named .ts, not an MPEG-TS video"
    text = head.lstrip()[:200].lower()
    if text.startswith((b"<svg", b"<?xml")) and b"<svg" in head.lower():
        return "an SVG drawing"
    if text.startswith((b"<!doctype html", b"<html")):
        return "a web page (HTML)"
    if head and all(32 <= b < 127 or b in (9, 10, 13) for b in head[:256]):
        return "a text file"
    return "damaged or unsupported file"


# ---------------------------------------------------------------- the model server

class ServiceDown(Exception):
    """The model server is not answering (stopped, loading, restarting). Not the photo's fault."""


class GpuBusy(ServiceDown):
    """The graphics card is taken by the AI Tagger's container: Search+ must wait for it to be freed."""


def container_running(name: str) -> bool:
    """Whether a container is up (False when docker or the container is not there)."""
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


class Service:
    """Talks to the model server container.

    ``/health`` of the model server says ``idleSeconds`` (since its last request) and ``idleExitMinutes`` but, unlike
    the AI Tagger's, no ``busy``: so the requests this panel has in flight are counted here (``inflight``), and the
    panel's own "last interactive use" is kept (``embed_text`` is only ever called for a search, a smart album or a
    theme: the indexer sends pictures, never words)."""

    def __init__(self, url: str = SERVICE_URL, container: str = CONTAINER, compose: Path = COMPOSE, *,
                 clock=time.monotonic):
        self.url, self.container, self.compose, self.clock = url.rstrip("/"), container, compose, clock
        self._state: str | None = None          # the container's state, remembered for STATE_TTL seconds
        self._state_at = 0.0
        self._count_lock = threading.Lock()
        self._inflight = 0
        self._interactive: float | None = None

    def health(self, timeout: float = 3) -> dict | None:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=timeout) as resp:
                return json.loads(resp.read())
        except (OSError, ValueError):
            return None

    def container_state(self, fresh: bool = False) -> str:
        """running / stopped / missing / unknown; remembered for a few seconds unless ``fresh`` (the status is polled)."""
        if not fresh and self._state is not None and self.clock() - self._state_at < STATE_TTL:
            return self._state
        try:
            out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self.container],
                                 capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            state = "unknown"
        else:
            state = "missing" if out.returncode != 0 else "running" if out.stdout.strip() == "true" else "stopped"
        self._state, self._state_at = state, self.clock()
        return state

    def invalidate(self) -> None:
        self._state = None

    # ---- what the unload rules need (see IdleLoop)
    def inflight(self) -> int:
        """Requests this panel has sent and not had answered."""
        return self._inflight

    def mark_interactive(self) -> None:
        self._interactive = self.clock()

    def interactive_age(self) -> float | None:
        """Seconds since the panel last used the model for a person (None: not since it started)."""
        return None if self._interactive is None else self.clock() - self._interactive

    def unload_inputs(self, fresh: bool = False) -> tuple[bool, dict | None]:
        """(whether the container runs, its /health). The state is remembered for a few seconds unless ``fresh``."""
        running = self.container_state(fresh=fresh) == "running"
        return running, (self.health(timeout=3 if fresh else 1.5) if running else None)

    def start(self) -> None:
        state = self.container_state(fresh=True)
        if state == "running":
            return
        if AITAGGER_EXCLUSIVE and container_running(AITAGGER_CONTAINER):       # take turns on the card
            raise GpuBusy("The GPU is in use by the AI Tagger — pause it to use Search+")
        if state == "missing":
            cmd = ["docker", "compose", "-f", str(self.compose), "up", "-d"]
        else:
            cmd = ["docker", "start", self.container]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        self.invalidate()
        if out.returncode != 0:
            raise RuntimeError(f"could not start the Search+ model server: {(out.stderr or out.stdout).strip()[-300:]}")

    def stop(self) -> bool:
        if self.container_state(fresh=True) != "running":
            return False
        subprocess.run(["docker", "stop", "-t", "10", self.container], capture_output=True, timeout=60)
        self.invalidate()
        return True

    def ready(self, wait: float = 120, progress=None, stop: threading.Event | None = None) -> dict:
        """Start the server if needed and wait until the model is loaded."""
        h = self.health()
        if h and h.get("status") == "ok":
            return h
        self.start()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise ServiceDown("stopped")
            h = self.health()
            if h and h.get("status") == "ok":
                return h
            if h and h.get("status") == "error":
                raise RuntimeError(f"the Search+ model failed to load: {h.get('error')}")
            if progress:
                progress("loading the Search+ model (the very first time it is downloaded, ~10 GB)"
                         if not h else "loading the Search+ model into the graphics card")
            if self.container_state() == "stopped" and time.monotonic() > deadline - wait + 20:
                raise RuntimeError("the Search+ model server stopped while loading; see `docker logs " + self.container + "`")
            time.sleep(2)
        raise ServiceDown("the Search+ model is still loading - try again in a moment")

    def _post(self, path: str, body: dict, timeout: float = 600) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with self._count_lock:
            self._inflight += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 503:
                raise ServiceDown(detail) from exc
            raise RuntimeError(f"Search+ model server error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise ServiceDown(str(exc)) from exc
        finally:
            with self._count_lock:
                self._inflight -= 1

    @staticmethod
    def _decode(b64: str):
        import numpy as np
        return np.frombuffer(base64.b64decode(b64), dtype="<f2").astype(np.float32)

    def embed_text(self, texts: list[str]):
        """Words to vectors: always for a person (a search, a smart album, a theme), so it counts as interactive use:
        the model then keeps its place for the server's own idle time instead of the short ``unload_after``."""
        import numpy as np
        self.mark_interactive()
        try:
            data = self._post("/embed/text", {"texts": texts}, timeout=120)
        finally:
            self.mark_interactive()
        return np.stack([self._decode(v) for v in data["vectors"]])

    def embed_images(self, images: list[bytes]) -> tuple[list, list]:
        data = self._post("/embed/images", {"images": [base64.b64encode(b).decode() for b in images]})
        vecs = [None if v is None else self._decode(v) for v in data["vectors"]]
        return vecs, data.get("errors") or [None] * len(vecs)


# ---------------------------------------------------------------- freeing the graphics card
#
# The rules (the same for Search+ and the AI Tagger; ``unload_after`` is a setting of each, 1-60 minutes, default 2):
#
# * "after-work": the indexer has nothing left to do (it is waiting for new photos) and the model server has been idle
#   for ``unload_after`` minutes: the panel stops the container. Never while the indexer works, never while a request
#   is in flight (the tagger says ``busy`` in /health; the Search+ server doesn't, so the panel counts its own).
# * "interactive": after a person used the model (a Search+ search, the AI Tagger's Test card) the short rule waits
#   until the server's own idle time (20 minutes) has passed since that use: more searches usually follow.
# * "server": nothing the panel does applies (the indexer is paused or stopped, so there is no waiting loop to run
#   the check): the model server exits by itself after its ``IDLE_EXIT_MINUTES``. That is also the backstop for all the
#   rest, and the only rule that never needs the panel.
# The check runs from the indexer's waiting loop, which wakes at least every ``IDLE_POLL`` seconds.

def unload_status(*, running: bool, health: dict | None, unload_after: int, indexer: str,
                  interactive_age: float | None = None, busy: bool = False) -> tuple[dict, bool]:
    """(the status ``unload`` object, whether the panel should stop the container now).

    ``indexer`` is "working", "waiting" (nothing left to do: the unload check runs) or "off" (paused / stopped);
    ``interactive_age`` the seconds since a person last used the model; ``busy`` a request the panel knows is in flight
    (on top of the server's own ``busy``). The object: ``loaded`` (the container runs), ``idleSeconds`` (the server's,
    None while it loads), ``unloadInSeconds`` (None: not counting down, e.g. while busy), ``rule`` (which rule will do
    it: after-work, interactive or server), ``busy`` (in use right now: a request in flight or the indexer working)."""
    if not running:
        return {"loaded": False, "idleSeconds": None, "unloadInSeconds": None, "rule": None, "busy": False}, False
    health = health if isinstance(health, dict) else {}
    number = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None  # noqa: E731
    ok = health.get("status") == "ok"
    idle = number(health.get("idleSeconds")) if ok else None             # meaningless while it is loading
    exit_min = number(health.get("idleExitMinutes"))
    server_exit = exit_min * 60 if exit_min and exit_min > 0 else None   # None: it never exits (or does not say)
    working = bool(busy or health.get("busy") or indexer == "working")
    out = {"loaded": True, "idleSeconds": None if idle is None else round(idle), "unloadInSeconds": None,
           "rule": "server" if indexer == "off" else "after-work", "busy": working}
    if idle is None or working:
        return out, False
    server_in = None if server_exit is None else max(server_exit - idle, 0.0)
    if indexer != "waiting":
        out["unloadInSeconds"] = None if server_in is None else round(server_in)
        return out, False
    short_in = max(max(int(unload_after), 1) * 60 - idle, 0.0)
    grace_len = server_exit if server_exit is not None else IDLE_EXIT_DEFAULT_MIN * 60.0
    grace_in = grace_len - interactive_age if interactive_age is not None and interactive_age < grace_len else 0.0
    panel_in = max(short_in, grace_in)
    if server_in is not None and server_in < panel_in:             # the server lets go first (e.g. unload_after 30)
        out.update(unloadInSeconds=round(server_in), rule="server")
        return out, False
    out.update(unloadInSeconds=round(panel_in), rule="interactive" if grace_in > short_in else "after-work")
    return out, panel_in <= 0


_NOW = object()         # "the probe's answer of the look that asked for this read" (see ``IdleLoop._read_catalog``)


class IdleLoop:
    """What Search+'s indexer and the AI Tagger's do while they have nothing to do, and how they keep the library list
    current: the unload rules (``unload_status`` / ``unload_check``), the sleep between looks, and the probe-driven
    re-read of the library list (``CatalogWatch``). The subclass says which model server it drives (``_model_server``),
    where its settings are (``_unload_settings``) and how to stop it (``_stop_model``), has ``store``, ``catalog``,
    ``clock``, ``last_sync``, ``_watch``, ``_wake``, ``_sync_lock``, ``IDLE_POLL``, ``running()``, and keeps ``waiting``
    true while it has nothing to do. The model server object offers ``unload_inputs(fresh)`` -> (running, health),
    ``inflight()`` and ``interactive_age()``; one that does not (a test's stand-in) is simply never unloaded."""

    waiting = False

    def _model_server(self):
        raise NotImplementedError

    def _unload_settings(self) -> dict:
        raise NotImplementedError

    def _stop_model(self) -> bool:
        raise NotImplementedError

    def _indexer_mode(self) -> str:
        if not self.running():
            return "off"
        return "waiting" if self.waiting else "working"

    def _judge(self, running: bool, health: dict | None) -> tuple[dict, bool, bool]:
        """(the ``unload`` object, whether the panel should stop the server now, whether a request is in flight)."""
        svc = self._model_server()
        age, inflight = getattr(svc, "interactive_age", None), getattr(svc, "inflight", None)
        busy = bool(inflight and inflight())
        out, due = unload_status(running=bool(running), health=health, unload_after=self._unload_settings()["unload_after"],
                                 indexer=self._indexer_mode(), interactive_age=age() if age else None, busy=busy)
        return out, due, busy

    def unload_status(self, *, running: bool | None = None, health: dict | None = None) -> dict:
        """The status ``unload`` object. Pass ``running`` and ``health`` when the caller has them already (the status
        route does): nothing is asked of docker or the model server then; otherwise the remembered state is used."""
        if running is None:
            inputs = getattr(self._model_server(), "unload_inputs", None)
            running, health = inputs(fresh=False) if inputs else (False, None)
        return self._judge(running, health)[0]

    def unload_check(self) -> dict | None:
        """Look at the model server with fresh eyes and stop it when the rules say so. Returns the ``unload`` object as
        it is afterwards (None when the server can't be looked at). Meant for the waiting loop: the caller has set
        ``waiting``."""
        inputs = getattr(self._model_server(), "unload_inputs", None)
        if inputs is None:
            return None
        try:
            out, due, busy = self._judge(*inputs(fresh=True))
            if due and not busy and self._stop_model():
                return {"loaded": False, "idleSeconds": None, "unloadInSeconds": None, "rule": None, "busy": False}
            return out
        except Exception:  # noqa: BLE001 - never let a docker hiccup end the indexer; the server's own exit is the net
            return None

    MIN_LOOK = 5.0              # seconds: never look at the model server more often than this (a stop that fails, a busy server)

    @staticmethod
    def _seconds_to_unload(status: dict | None, rules=("after-work", "interactive")) -> float | None:
        """When the panel itself will want to look again (None: nothing for it to wait for)."""
        if not status or not status.get("loaded") or status.get("rule") not in rules:
            return None
        if status.get("busy"):
            return 0.0                      # a request is in flight: look again soon, it will be idle after it
        return status.get("unloadInSeconds")

    # ---- the waiting loop (what an indexer does while it has nothing to do)
    def _idle_wait(self, settings: dict, unload: dict | None) -> float:
        """How long to sleep with nothing to do: until the next probe, until the unload rule may fire, at most
        ``IDLE_POLL`` seconds (a wake-up is cheap, and it is what runs the unload check)."""
        wait = min(self.IDLE_POLL, max(self._watch.next_in(settings["check_every"]), 1))
        left = self._seconds_to_unload(unload)
        return wait if left is None else min(wait, max(left + 1, self.MIN_LOOK))

    def _wind_down(self, stop: threading.Event) -> bool:
        """``keep_updated`` is off and everything is done: the thread is about to end, but first the card is freed once
        the short rule allows (when an interactive grace is on, the model server's own exit does it). True: woken for
        new work (Try again, Start) rather than ended."""
        self.waiting = True
        try:
            while not stop.is_set():
                left = self._seconds_to_unload(self.unload_check(), rules=("after-work",))
                if left is None:
                    return False
                if self._wake.wait(min(self.IDLE_POLL, max(left + 1, self.MIN_LOOK))):
                    self._wake.clear()
                    return not stop.is_set()
            return False
        finally:
            self.waiting = False

    def _idle_text(self, settings: dict, done: str) -> str:
        every = settings["check_every"]
        text = f"everything is {done}; looks for new photos every {'minute' if every == 1 else f'{every} minutes'}"
        held = self.store.held()
        if held:
            text += f"; {held} new {'item is' if held == 1 else 'items are'} waiting for Immich to finish them"
        return text

    # ---- the library list
    def _read_catalog(self, seen=_NOW) -> None:
        """Read the library list in full. ``seen`` is the probe's answer from just before the read (the next probe is
        compared to it): the look that asked for this read by default."""
        with self._sync_lock:
            if seen is _NOW:
                seen = self._watch.seen
            self.store.sync_catalog(self.catalog())
            self.last_sync = self.clock()
            self._watch.synced(seen)

    def _sync_if_due(self, settings: dict) -> None:
        if self._watch.due(self.last_sync, settings["check_every"]):
            self.state, self.detail = "running", "reading the library list"
            self._read_catalog()


# ---------------------------------------------------------------- the index

SCHEMA = """
create table if not exists assets (
  id text primary key, type text, taken text, name text, preview text, original text,
  duration_ms integer default 0, gone integer default 0, added integer default 0
);
create table if not exists indexed (
  id text primary key, first_row integer, n_rows integer, kind text, indexed_at text
);
create table if not exists failed (id text primary key, error text, attempts integer, at text);
create table if not exists meta (key text primary key, value text);
"""


class View:
    """A snapshot for searching: the vector matrix and, per row, which asset it belongs to."""

    def __init__(self, store: "Store"):
        import numpy as np

        conn = store.conn
        dim = store.dim
        n_rows = store.row_count() if dim else 0
        assets = conn.execute("select id, type, substr(taken,1,10), name, gone from assets").fetchall()
        self.ids = [a[0] for a in assets]
        self.pos = {aid: i for i, aid in enumerate(self.ids)}
        self.types = np.array([a[1] or "" for a in assets], dtype="U5")
        self.taken = np.array([a[2] or "" for a in assets], dtype="U10")
        self.names = [a[3] or "" for a in assets]
        self.live = np.array([not a[4] for a in assets], dtype=bool)
        self.owner = np.full(n_rows, -1, dtype=np.int32)
        self.first = np.zeros(len(self.ids), dtype=np.int64)       # per asset: its first matrix row ...
        self.n_rows = np.zeros(len(self.ids), dtype=np.int64)      # ... and how many rows it has (0 = not indexed)
        self.rows: dict[str, tuple[int, int]] = {}
        for aid, first, n in conn.execute("select id, first_row, n_rows from indexed"):
            p = self.pos.get(aid)
            if p is None or first + n > n_rows:
                continue
            self.owner[first:first + n] = p
            self.first[p], self.n_rows[p] = first, n
            self.rows[aid] = (first, n)
        self.indexed = np.zeros(len(self.ids), dtype=bool)
        for aid in self.rows:
            self.indexed[self.pos[aid]] = True
        self.matrix = (np.memmap(store.vec_path, dtype="<f2", mode="r", shape=(n_rows, dim))
                       if n_rows else np.zeros((0, dim or 1), dtype="<f2"))

    def row_scores(self, q):
        import numpy as np

        q = np.asarray(q, dtype=np.float32)
        out = np.empty(len(self.matrix), dtype=np.float32)
        step = 32768
        for s in range(0, len(self.matrix), step):
            out[s:s + step] = np.asarray(self.matrix[s:s + step], dtype=np.float32) @ q
        return out

    def rows_of(self, positions):
        """The matrix rows that belong to these asset positions (an asset's rows together, assets in position order; an
        asset that is not indexed has none)."""
        import numpy as np

        pos = np.unique(np.asarray(positions, dtype=np.int64))
        counts = self.n_rows[pos]
        pos, counts = pos[counts > 0], counts[counts > 0]
        if not len(pos):
            return np.zeros(0, dtype=np.int64)
        starts = np.cumsum(counts) - counts                        # where each asset's rows begin in the answer
        return np.repeat(self.first[pos] - starts, counts) + np.arange(int(counts.sum()))

    def row_scores_of(self, q, rows):
        """Like ``row_scores`` for just these matrix rows (in the order given): the other rows are never read."""
        import numpy as np

        q = np.asarray(q, dtype=np.float32)
        out = np.empty(len(rows), dtype=np.float32)
        step = 32768
        for s in range(0, len(rows), step):
            out[s:s + step] = np.asarray(self.matrix[rows[s:s + step]], dtype=np.float32) @ q
        return out

    def asset_vector(self, asset_id: str):
        import numpy as np

        if asset_id not in self.rows:
            raise ValueError("That photo is not in the Search+ index yet.")
        first, n = self.rows[asset_id]
        v = np.asarray(self.matrix[first:first + n], dtype=np.float32).mean(axis=0)
        return v / max(float(np.linalg.norm(v)), 1e-6)


class Store:
    def __init__(self, folder: Path | None = None):
        self.folder = folder or home()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.vec_path = self.folder / "vectors.f16"
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.folder / "index.sqlite"), timeout=30, check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.executescript(SCHEMA)
        if "added" not in {r[1] for r in self.conn.execute("pragma table_info(assets)")}:      # made before the preview hold-back
            with self.conn:
                self.conn.execute("alter table assets add column added integer default 0")
        self._meta = dict(self.conn.execute("select key, value from meta").fetchall())
        self._view: View | None = None
        self._view_key = None
        self._tidy()

    # ---- meta (kept in memory too: read from many threads, written rarely)
    def meta(self, key: str, default: str = "") -> str:
        return self._meta.get(key, default)

    def set_meta(self, key: str, value: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("insert or replace into meta values (?,?)", (key, str(value)))
            self._meta[key] = str(value)

    @property
    def dim(self) -> int:
        return int(self.meta("dim", "0") or 0)

    @property
    def model(self) -> str:
        return self.meta("model")

    def row_count(self) -> int:
        dim = self.dim
        if not dim or not self.vec_path.exists():
            return 0
        return self.vec_path.stat().st_size // (dim * 2)

    def _tidy(self) -> None:
        """Drop a half-written last row (e.g. after a power cut)."""
        dim = self.dim
        if dim and self.vec_path.exists():
            size = self.vec_path.stat().st_size
            if size % (dim * 2):
                with open(self.vec_path, "r+b") as fh:
                    fh.truncate(size - size % (dim * 2))

    def adopt_model(self, model: str, dim: int) -> None:
        """Remember which model made the vectors; refuse to mix models in one index."""
        if not self.model:
            self.set_meta("model", model)
            self.set_meta("dim", dim)
        elif self.model != model or self.dim != dim:
            raise RuntimeError(f"this index was built with {self.model} ({self.dim}-d) but the model server runs "
                               f"{model} ({dim}-d); use 'Start over' to rebuild it")

    def reset(self) -> None:
        with self.lock, self.conn:
            self.conn.execute("delete from indexed")
            self.conn.execute("delete from failed")
            self.conn.execute("delete from meta where key in ('model','dim')")
            self._meta.pop("model", None)
            self._meta.pop("dim", None)
            if self.vec_path.exists():
                self.vec_path.unlink()
        self._view = None

    # ---- catalogue
    def sync_catalog(self, rows: list[dict]) -> dict:
        with self.lock, self.conn:
            self.conn.execute("update assets set gone=1")
            self.conn.executemany(
                "insert into assets (id, type, taken, name, preview, original, duration_ms, added, gone)"
                " values (?,?,?,?,?,?,?,?,0) on conflict(id) do update set type=excluded.type, taken=excluded.taken,"
                " name=excluded.name, preview=excluded.preview, original=excluded.original,"
                " duration_ms=excluded.duration_ms, added=excluded.added, gone=0",
                [(r["id"], r["type"], r["taken"], r.get("name", ""), r.get("preview", ""), r.get("original", ""),
                  int(r.get("duration_ms") or 0), int(r.get("added") or 0)) for r in rows])
            self.set_meta("catalog_at", _now())
        self._view = None
        return {"assets": len(rows)}

    def todo(self, n: int) -> list[dict]:
        with self.lock:
            return self._todo(n)

    def _todo(self, n: int) -> list[dict]:
        # an asset Immich has not finished (no preview yet, added a moment ago) is left out, not failed: see READY
        cur = self.conn.execute(
            "select a.id, a.type, a.preview, a.original, a.duration_ms, a.taken from assets a"
            " where a.gone=0 and not exists (select 1 from indexed i where i.id=a.id)"
            " and not exists (select 1 from failed f where f.id=a.id and (f.attempts >= ? or f.at > ?))"
            f" and {READY} order by a.taken desc, a.id limit ?", (MAX_ATTEMPTS, _ago(RETRY_AFTER), preview_cutoff(), n))
        return [dict(zip(("id", "type", "preview", "original", "duration_ms", "taken"), r)) for r in cur]

    def held(self) -> int:
        """How many assets are waiting for Immich to make their preview (see ``READY``)."""
        with self.lock:
            return self.conn.execute(
                "select count(*) from assets a where a.gone=0 and not exists (select 1 from indexed i where i.id=a.id)"
                f" and not {READY}", (preview_cutoff(),)).fetchone()[0]

    def append(self, asset_id: str, kind: str, vectors: list) -> None:
        import numpy as np

        data = np.ascontiguousarray(np.stack(vectors), dtype="<f2")
        if data.shape[1] != self.dim:
            raise ValueError(f"vector size {data.shape[1]} != index size {self.dim}")
        with self.lock:
            first = self.row_count()
            with open(self.vec_path, "ab") as fh:
                fh.write(data.tobytes())
                fh.flush()
            with self.conn:
                self.conn.execute("insert or replace into indexed values (?,?,?,?,?)",
                                  (asset_id, first, len(data), kind, _now()))
                self.conn.execute("delete from failed where id=?", (asset_id,))

    def fail(self, asset_id: str, error: str, final: bool = False) -> None:
        """Note a failure; it is tried again later unless `final` (e.g. the file isn't media at all)."""
        first = MAX_ATTEMPTS if final else 1
        with self.lock, self.conn:
            self.conn.execute(
                "insert into failed values (?,?,?,?) on conflict(id) do update set error=excluded.error,"
                " attempts=max(attempts+1, excluded.attempts), at=excluded.at", (asset_id, error[:300], first, _now()))

    def retry_failed(self) -> int:
        """Forget every failure (cleared ones too), so they are tried again."""
        with self.lock, self.conn:
            return self.conn.execute("delete from failed").rowcount

    def clear_failed(self) -> int:
        """Take the failures off the list; they are not tried again until 'Try again'."""
        with self.lock, self.conn:
            return self.conn.execute("update failed set attempts=? where attempts < ?", (CLEARED, CLEARED)).rowcount

    def counts(self) -> dict:
        with self.lock:
            return self._counts()

    def _counts(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0] or 0  # noqa: E731
        total = q("select count(*) from assets where gone=0")
        done = q("select count(*) from indexed i join assets a on a.id=i.id where a.gone=0")
        not_done = "join assets a on a.id=f.id where a.gone=0 and not exists (select 1 from indexed i where i.id=f.id)"
        failed = q(f"select count(*) from failed f {not_done} and f.attempts < {CLEARED}")
        retrying = q(f"select count(*) from failed f {not_done} and f.attempts < {MAX_ATTEMPTS}")
        cleared = q(f"select count(*) from failed f {not_done} and f.attempts >= {CLEARED}")
        return {
            "assets": total, "indexed": done, "failed": failed, "retrying": retrying, "cleared": cleared,
            "pending": max(total - done - failed - cleared, 0),
            "videos": q("select count(*) from assets where gone=0 and type='VIDEO'"),
            "videosIndexed": q("select count(*) from indexed i join assets a on a.id=i.id where a.gone=0 and a.type='VIDEO'"),
            "frames": self.row_count(), "sizeMB": round(self.vec_path.stat().st_size / 1e6, 1) if self.vec_path.exists() else 0,
            "catalogAt": self.meta("catalog_at") or None,
        }

    def failures(self, limit: int = 8) -> list[dict]:
        with self.lock:
            return self._failures(limit)

    def _failures(self, limit: int) -> list[dict]:
        cur = self.conn.execute(
            f"select f.id, f.error, f.attempts from failed f join assets a on a.id=f.id where a.gone=0"
            f" and f.attempts < {CLEARED} and not exists (select 1 from indexed i where i.id=f.id)"
            f" order by f.at desc limit ?", (limit,))
        return [{"id": r[0], "error": r[1], "attempts": r[2]} for r in cur]

    # ---- searching
    def view(self) -> View:
        with self.lock:
            key = (self.row_count(), self.conn.execute("select count(*), max(indexed_at) from indexed").fetchone(),
                   self.meta("catalog_at"))
            if self._view is None or key != self._view_key:
                self._view, self._view_key = View(self), key
            return self._view


def best_scores(view: View, vector, only=None):
    """Each asset's score for a query vector: its best-matching frame (-inf when not indexed).

    ``only`` (asset positions, or None for every asset) limits the work to those assets: only their matrix rows are
    read and scored, everything else stays -inf. That is how a search over a few hundred assets (the ones with a
    tag, say) costs a few hundred rows instead of the whole index."""
    import numpy as np

    best = np.full(len(view.ids), -np.inf, dtype=np.float32)
    if only is None:
        scores = view.row_scores(vector)
        rows = view.owner >= 0
        np.maximum.at(best, view.owner[rows], scores[rows])
        return best
    rows = view.rows_of(only)
    if len(rows):
        np.maximum.at(best, view.owner[rows], view.row_scores_of(vector, rows))
    return best


def positions_of(view: View, ids):
    """The asset positions in ``view`` of these asset ids (ids the view does not know are left out)."""
    import numpy as np

    return np.fromiter((view.pos[i] for i in ids if i in view.pos), dtype=np.int64)


def search(store: Store, vector, *, media: str | None = None, after: str | None = None, before: str | None = None,
           limit: int = 200, exclude=(), skip: str | None = None, only=None) -> list[dict]:
    """Best matches first: each asset scores as its best-matching frame.

    ``only`` is a set of asset ids to rank (None: the whole index). Only those assets' vectors are read and scored,
    and nothing else can be returned; the media, date and exclusion filters narrow them further before scoring."""
    import numpy as np

    view = store.view()
    if not len(view.matrix):
        return []
    mask = view.live & view.indexed
    if media in ("IMAGE", "VIDEO"):
        mask &= view.types == media
    if after:
        mask &= view.taken >= str(after)[:10]
    if before:
        mask &= view.taken <= str(before)[:10]
    for aid in list(exclude) + ([skip] if skip else []):
        p = view.pos.get(aid)
        if p is not None:
            mask[p] = False
    if only is not None:
        allowed = np.zeros(len(view.ids), dtype=bool)
        allowed[positions_of(view, only)] = True
        mask &= allowed
        idx = np.flatnonzero(mask)
        best = best_scores(view, vector, only=idx) if len(idx) else None
    else:
        best = best_scores(view, vector)
        idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    k = min(int(limit), len(idx))
    top = idx[np.argpartition(-best[idx], k - 1)[:k]]
    top = top[np.argsort(-best[top], kind="stable")]
    return [{"id": view.ids[i], "score": round(float(best[i]), 4), "type": str(view.types[i]),
             "date": str(view.taken[i]), "name": view.names[i]} for i in top]


# ---------------------------------------------------------------- building the index

class Indexer(IdleLoop):
    """Background thread that fills the index; stop/start at will, it resumes where it was.

    Keeping up to date: a cheap probe of Immich's database every ``check_every`` minutes (``CatalogWatch``) decides
    whether the library list is read again, and a full read is made at least once an hour. When there is nothing left
    to do, the loop waits for at most ``IDLE_POLL`` seconds and, each time it wakes, lets the unload rules look at the
    model server (``IdleLoop``): the model does not stay in the graphics card for a single new photo."""

    BUSY_WAIT = 30              # seconds between looks while the AI Tagger has the graphics card
    CHUNK = 96                  # assets prepared per round
    IMAGES_PER_REQUEST = 32
    WORKERS = 6                 # threads reading previews / cutting video frames
    IDLE_POLL = IDLE_POLL       # seconds between looks (and unload checks) while there is nothing to do, at most

    def __init__(self, store: Store, service: Service, *, catalog=fetch_catalog, frames=prepare_frames,
                 clock=time.monotonic, probe=None):
        self.store, self.service, self.catalog, self.frames, self.clock = store, service, catalog, frames, clock
        # the real probe goes with the real catalogue; a test that injects a catalogue has no database to ask
        self._watch = CatalogWatch(probe if probe is not None else (fetch_probe if catalog is fetch_catalog else None),
                                   clock)
        self.state, self.detail, self.error = "stopped", "", ""
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.last_sync = None                   # when the library list was last read in full (None: read it next)
        self.done_times: collections.deque = collections.deque(maxlen=2000)
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._wake = threading.Event()          # cuts the "up to date" wait short (Try again, Build index)

    # ---- IdleLoop
    def _model_server(self):
        return self.service

    def _unload_settings(self) -> dict:
        return load_settings()

    def _stop_model(self) -> bool:
        return self.service.stop()

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.running():
                self._watch.force = True        # already waiting for new photos: look again now
                self._wake.set()
                return
            self.stop_event = threading.Event()
            self.error = ""
            self.state, self.detail = "starting", "starting"
            self.thread = threading.Thread(target=self._run, args=(self.stop_event,), name="searchplus-indexer",
                                           daemon=True)
            self.thread.start()

    def stop(self, wait_s: float = 0) -> None:
        self.stop_event.set()
        self._wake.set()
        if wait_s and self.thread:
            self.thread.join(wait_s)

    def rate_per_min(self, frames: bool = False) -> float:
        """Items (or pictures, with frames=True) finished per minute over the last five minutes."""
        now = self.clock()
        recent = [d for d in self.done_times if now - d[0] < 300]
        if len(recent) < 5:
            return 0.0
        span = max(now - recent[0][0], 1.0)
        return round(sum(d[1] if frames else 1 for d in recent) * 60 / span, 1)

    # ---- the loop
    def _run(self, stop: threading.Event) -> None:
        drops = 0
        try:
            while not stop.is_set():
                settings = load_settings()
                self._sync_if_due(settings)
                todo = self.store.todo(self.CHUNK)
                if not todo:
                    if not settings["keep_updated"]:
                        self.state, self.detail = "done", "everything is indexed"
                        if self._wind_down(stop):
                            continue
                        return
                    self.state, self.detail = "done", self._idle_text(settings, "indexed")
                    self.waiting = True             # nothing to do: the unload rules may look at the model server
                    self._wake.wait(self._idle_wait(settings, self.unload_check()))
                    self._wake.clear()
                    continue
                self.waiting = False
                self.state = "starting"
                try:
                    health = self.service.ready(wait=3600, stop=stop,
                                                progress=lambda d: setattr(self, "detail", d))
                except GpuBusy as exc:          # waiting, not a drop: the AI Tagger lets go after a while
                    self.state, self.detail = "starting", f"waiting: {exc}"
                    stop.wait(self.BUSY_WAIT)
                    continue
                self.store.adopt_model(health["model"], int(health["dim"]))
                self.state, self.detail = "running", "indexing"
                try:
                    self._index(todo, settings, stop)
                    drops = 0
                except ServiceDown:
                    drops += 1                  # the server went away (crash, restart): start it again
                    if stop.is_set() or drops >= 3:
                        raise
                    self.detail = "the model server went away; starting it again"
                    stop.wait(10)
            self.state, self.detail = "stopped", "paused"
        except ServiceDown as exc:
            if stop.is_set():
                self.state, self.detail = "stopped", "paused"
            else:
                self.state, self.detail, self.error = "error", f"the model server stopped: {exc}", str(exc)
        except Exception as exc:  # noqa: BLE001
            self.state, self.detail, self.error = "error", f"{type(exc).__name__}: {exc}", f"{type(exc).__name__}: {exc}"
        finally:
            self.waiting = False

    def _index(self, todo: list[dict], settings: dict, stop: threading.Event) -> None:
        n = int(settings["video_frames"])
        prep, send = ThreadPoolExecutor(self.WORKERS), ThreadPoolExecutor(2)
        try:
            futures = {prep.submit(self.frames, item, n): item for item in todo}
            batch, size, sending = [], 0, set()
            # whichever is ready first: one slow video (a huge file) must not hold up the ones behind it
            for fut in as_completed(futures):
                item = futures[fut]
                if stop.is_set():
                    break
                try:
                    frames, kind = fut.result()
                except Exception as exc:  # noqa: BLE001 - unreadable file: note it and go on
                    # ValueError = we looked at the file and it isn't usable media: no point retrying
                    self.store.fail(item["id"], f"{type(exc).__name__}: {exc}", final=isinstance(exc, ValueError))
                    continue
                batch.append((item, kind, frames))
                size += len(frames)
                if size >= self.IMAGES_PER_REQUEST:
                    sending = self._submit(send, sending, batch)
                    batch, size = [], 0
            if batch and not stop.is_set():
                sending = self._submit(send, sending, batch)
            for f in list(sending):
                f.result()              # re-raises ServiceDown
        finally:
            prep.shutdown(wait=False, cancel_futures=True)
            send.shutdown(wait=True)

    def _submit(self, pool: ThreadPoolExecutor, sending: set, batch: list) -> set:
        while len(sending) >= 2:        # at most two requests in flight: one on the GPU, one being prepared
            done, sending = wait(sending, return_when=FIRST_COMPLETED)
            for f in done:
                f.result()
        sending = set(sending)
        sending.add(pool.submit(self._send, batch))
        return sending

    def _send(self, batch: list) -> None:
        images = [f for _, _, frames in batch for f in frames]
        vecs, errors = self.service.embed_images(images)
        i = 0
        for item, kind, frames in batch:
            got = [v for v in vecs[i:i + len(frames)] if v is not None]
            errs = [e for e in errors[i:i + len(frames)] if e]
            i += len(frames)
            if got:
                self.store.append(item["id"], kind, got)
                self.done_times.append((self.clock(), len(got)))
            else:
                self.store.fail(item["id"], errs[0] if errs else "the model could not read this image")

    def status(self) -> dict:
        counts = self.store.counts()
        rate = self.rate_per_min() if self.state == "running" else 0.0
        # videos cost several pictures each, photos one: estimate the time from pictures still to make
        per_min = self.rate_per_min(frames=True) if rate else 0.0
        videos_left = max(counts["videos"] - counts["videosIndexed"], 0)
        pictures_left = max(counts["pending"] - videos_left, 0) + videos_left * int(load_settings()["video_frames"])
        eta = round(pictures_left / per_min) if per_min else None
        return {"state": self.state, "detail": self.detail, "error": self.error, "running": self.running(),
                "ratePerMin": rate, "etaMinutes": eta}


# ---------------------------------------------------------------- one per panel process

_LOCK = threading.Lock()
_INSTANCE: dict = {}


def instance() -> tuple[Store, Service, Indexer]:
    """The panel's store / model server / indexer (created on first use)."""
    with _LOCK:
        key = str(home())
        if key not in _INSTANCE:
            store, service = Store(), Service()
            _INSTANCE[key] = (store, service, Indexer(store, service))
        return _INSTANCE[key]


def autostart() -> None:
    """Resume indexing after a panel restart if it was on."""
    try:
        if load_settings().get("indexing"):
            instance()[2].start()
    except Exception:  # noqa: BLE001 - the panel must start regardless
        pass
