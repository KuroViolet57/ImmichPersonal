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
AITAGGER_VLM_CONTAINER = os.environ.get("AITAGGER_VLM_CONTAINER", "immich_aitagger_vlm")   # the AI Tagger's GPU hog
# Whether Search+ and the AI Tagger take turns on the graphics card. True: Search+ will not start while the tagger's
# language model runs (GpuBusy), and the AI Tagger stops a running Search+ before it starts its own containers.
# False: they may run at the same time (set it when the card has room for both); nothing is stopped or refused.
AITAGGER_EXCLUSIVE = True
DEFAULTS = {"indexing": False, "keep_updated": True, "video_frames": 4}
LIMITS = {"video_frames": (1, 8)}
FRAME_SIDE = 640              # frames are sent at most this big; the model looks at 448 x 448
MAX_ATTEMPTS = 3
CLEARED = 99                  # attempts value for failures the user cleared from the list
RETRY_AFTER = 900             # seconds before a photo that failed is tried again (e.g. its preview was not made yet)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat(timespec="seconds")


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
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


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

def fetch_catalog() -> list[dict]:
    """Every photo and video Immich shows (timeline + archive), with host paths to its files."""
    sql = """select a.id, a.type, coalesce(p.path,''), a."originalPath", coalesce(a.duration,0),
                    coalesce(a."localDateTime"::text, a."fileCreatedAt"::text, ''), a."originalFileName"
             from asset a
             left join asset_file p on p."assetId" = a.id and p.type = 'preview'
             where a."deletedAt" is null and a.visibility in ('timeline','archive') and a.type in ('IMAGE','VIDEO')"""
    out = subprocess.run(
        ["docker", "exec", "-i", "immich_postgres", "psql", "-U", "postgres", "-d", "immich", "-At", "-F", "\x1f",
         "-R", "\x1e"], input=sql, capture_output=True, text=True, timeout=600, check=True).stdout
    mounts = json.loads(subprocess.run(["docker", "inspect", "immich_server", "--format", "{{json .Mounts}}"],
                                       capture_output=True, text=True, timeout=30, check=True).stdout)
    outside = next(m["Source"] for m in mounts if m.get("Destination") == "/data")

    def host(path: str) -> str:
        return outside + path[len("/data"):] if path.startswith("/data") else path

    rows = []
    for rec in out.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) != 7:
            continue
        aid, typ, preview, original, duration, taken, name = parts
        rows.append({"id": aid, "type": typ, "preview": host(preview), "original": host(original),
                     "duration_ms": int(duration or 0), "taken": taken, "name": name})
    return rows


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
    """The graphics card is taken by the AI Tagger's language model: Search+ must wait for it to be freed."""


def container_running(name: str) -> bool:
    """Whether a container is up (False when docker or the container is not there)."""
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


class Service:
    """Talks to the model server container."""

    def __init__(self, url: str = SERVICE_URL, container: str = CONTAINER, compose: Path = COMPOSE):
        self.url, self.container, self.compose = url.rstrip("/"), container, compose

    def health(self, timeout: float = 3) -> dict | None:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=timeout) as resp:
                return json.loads(resp.read())
        except (OSError, ValueError):
            return None

    def container_state(self) -> str:
        try:
            out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", self.container],
                                 capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        if out.returncode != 0:
            return "missing"
        return "running" if out.stdout.strip() == "true" else "stopped"

    def start(self) -> None:
        state = self.container_state()
        if state == "running":
            return
        if AITAGGER_EXCLUSIVE and container_running(AITAGGER_VLM_CONTAINER):       # take turns on the card
            raise GpuBusy("The GPU is in use by the AI Tagger — pause it to use Search+")
        if state == "missing":
            cmd = ["docker", "compose", "-f", str(self.compose), "up", "-d"]
        else:
            cmd = ["docker", "start", self.container]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if out.returncode != 0:
            raise RuntimeError(f"could not start the Search+ model server: {(out.stderr or out.stdout).strip()[-300:]}")

    def stop(self) -> bool:
        if self.container_state() != "running":
            return False
        subprocess.run(["docker", "stop", "-t", "10", self.container], capture_output=True, timeout=60)
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

    @staticmethod
    def _decode(b64: str):
        import numpy as np
        return np.frombuffer(base64.b64decode(b64), dtype="<f2").astype(np.float32)

    def embed_text(self, texts: list[str]):
        import numpy as np
        data = self._post("/embed/text", {"texts": texts}, timeout=120)
        return np.stack([self._decode(v) for v in data["vectors"]])

    def embed_images(self, images: list[bytes]) -> tuple[list, list]:
        data = self._post("/embed/images", {"images": [base64.b64encode(b).decode() for b in images]})
        vecs = [None if v is None else self._decode(v) for v in data["vectors"]]
        return vecs, data.get("errors") or [None] * len(vecs)


# ---------------------------------------------------------------- the index

SCHEMA = """
create table if not exists assets (
  id text primary key, type text, taken text, name text, preview text, original text,
  duration_ms integer default 0, gone integer default 0
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
        self.rows: dict[str, tuple[int, int]] = {}
        for aid, first, n in conn.execute("select id, first_row, n_rows from indexed"):
            p = self.pos.get(aid)
            if p is None or first + n > n_rows:
                continue
            self.owner[first:first + n] = p
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
                "insert into assets (id, type, taken, name, preview, original, duration_ms, gone)"
                " values (?,?,?,?,?,?,?,0) on conflict(id) do update set type=excluded.type, taken=excluded.taken,"
                " name=excluded.name, preview=excluded.preview, original=excluded.original,"
                " duration_ms=excluded.duration_ms, gone=0",
                [(r["id"], r["type"], r["taken"], r.get("name", ""), r.get("preview", ""), r.get("original", ""),
                  int(r.get("duration_ms") or 0)) for r in rows])
            self.set_meta("catalog_at", _now())
        self._view = None
        return {"assets": len(rows)}

    def todo(self, n: int) -> list[dict]:
        with self.lock:
            return self._todo(n)

    def _todo(self, n: int) -> list[dict]:
        cur = self.conn.execute(
            "select a.id, a.type, a.preview, a.original, a.duration_ms, a.taken from assets a"
            " where a.gone=0 and not exists (select 1 from indexed i where i.id=a.id)"
            " and not exists (select 1 from failed f where f.id=a.id and (f.attempts >= ? or f.at > ?))"
            " order by a.taken desc, a.id limit ?", (MAX_ATTEMPTS, _ago(RETRY_AFTER), n))
        return [dict(zip(("id", "type", "preview", "original", "duration_ms", "taken"), r)) for r in cur]

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


def best_scores(view: View, vector):
    """Each asset's score for a query vector: its best-matching frame (-inf when not indexed)."""
    import numpy as np

    scores = view.row_scores(vector)
    best = np.full(len(view.ids), -np.inf, dtype=np.float32)
    rows = view.owner >= 0
    np.maximum.at(best, view.owner[rows], scores[rows])
    return best


def search(store: Store, vector, *, media: str | None = None, after: str | None = None, before: str | None = None,
           limit: int = 200, exclude=(), skip: str | None = None) -> list[dict]:
    """Best matches first: each asset scores as its best-matching frame."""
    import numpy as np

    view = store.view()
    if not len(view.matrix):
        return []
    best = best_scores(view, vector)
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
    idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    k = min(int(limit), len(idx))
    top = idx[np.argpartition(-best[idx], k - 1)[:k]]
    top = top[np.argsort(-best[top], kind="stable")]
    return [{"id": view.ids[i], "score": round(float(best[i]), 4), "type": str(view.types[i]),
             "date": str(view.taken[i]), "name": view.names[i]} for i in top]


# ---------------------------------------------------------------- building the index

class Indexer:
    """Background thread that fills the index; stop/start at will, it resumes where it was."""

    CATALOG_EVERY = 600         # re-read the library list this often (and look for new photos)
    BUSY_WAIT = 30              # seconds between looks while the AI Tagger has the graphics card
    CHUNK = 96                  # assets prepared per round
    IMAGES_PER_REQUEST = 32
    WORKERS = 6                 # threads reading previews / cutting video frames

    def __init__(self, store: Store, service: Service, *, catalog=fetch_catalog, frames=prepare_frames,
                 clock=time.monotonic):
        self.store, self.service, self.catalog, self.frames, self.clock = store, service, catalog, frames, clock
        self.state, self.detail, self.error = "stopped", "", ""
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.last_sync = None
        self.done_times: collections.deque = collections.deque(maxlen=2000)
        self._lock = threading.Lock()
        self._wake = threading.Event()          # cuts the "up to date" wait short (Try again, Build index)

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.running():
                self._wake.set()                # already waiting for new photos: look again now
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
                if self.last_sync is None or self.clock() - self.last_sync >= self.CATALOG_EVERY:
                    self.state, self.detail = "running", "reading the library list"
                    self.store.sync_catalog(self.catalog())
                    self.last_sync = self.clock()
                todo = self.store.todo(self.CHUNK)
                if not todo:
                    if not settings["keep_updated"]:
                        self.state, self.detail = "done", "everything is indexed"
                        return
                    self.state, self.detail = "done", "everything is indexed; looks for new photos every 10 minutes"
                    self._wake.wait(max(self.CATALOG_EVERY - (self.clock() - self.last_sync), 5))
                    self._wake.clear()
                    continue
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
