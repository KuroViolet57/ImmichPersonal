"""Smart albums (called "themes" in the code): albums that fill themselves.

Rules and themes used to be two features; a smart album now covers both.
Each one says *what to look for* -- a description, "photos like this one", or
only people -- and *how to pick*:

* **cut-off** (default): every photo whose similarity is at or above a score;
* **top N**: the N best matches, like a normal search.

Plus the old rules' filters: photo/video, people (all/any), taken between,
skip albums, only photos in no album, "must also / must not match" terms, and
optionally archive or favourite what it adds. Each can run every hour.

The original "themes" notes follow.

A theme is "a description + how strict to be". Each run scores every photo in
the library against the description with Immich's own CLIP model, and *adds*
new matches to the theme's album. It never moves or removes anything.

How a run decides what to add
-----------------------------
* The description is turned into a CLIP text vector by Immich's machine-
  learning service (the same model smart search uses), cached per model.
* Every photo's similarity to it is read from Postgres (``smart_search``),
  which takes ~2 s for 72k photos. Photos without an embedding yet (fresh
  uploads Immich has not analysed) simply are not scored until it has.
* Photos at or above the theme's cut-off are candidates.
* A photo is added **once per theme, ever**: every added id is remembered, so
  a photo you take out of the album is never put back. Lowering the cut-off
  later adds the newly qualifying photos; raising it adds nothing new.
* Only photos in the timeline or archive are considered -- never trashed,
  hidden or Locked Folder items.

Every run is journalled, so History -> Undo removes what that run added.

Similarity scales differ per description (e.g. 0.10 is a tight match for a
franchise name but loose for "chat screenshot"), which is why each theme has
its own cut-off, chosen from a preview.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import re
import subprocess
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from .client import ImmichClient, ImmichError
from .config import state_dir
from .engine import album_asset_ids, find_album, journal_path

DEFAULT_CUTOFF = 0.10
# Search+ (PE-Core) scores sit on their own scale: a text's best matches score ~0.17-0.23, the
# middle of the library ~0.10, so its default cut-off is higher (measured on this library).
SP_DEFAULT_CUTOFF = 0.17
SP_TERM_CUTOFF = 0.15            # "must also / must not" words next to "like this photo"
ENGINES = ("immich", "searchplus")
SP_KEY = "sp:"                   # prefix of cached Search+ text vectors
MEDIA_TYPES = ("IMAGE", "VIDEO")   # a theme can be limited to photos or to videos
DESCRIPTION_PREFIX = "Auto theme"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ storage


def store_path() -> Path:
    return state_dir() / "themes.json"


def _seen_path(theme_id: str) -> Path:
    return state_dir() / "themes" / f"{theme_id}.seen.json"


def load_store() -> dict:
    try:
        data = json.loads(store_path().read_text("utf-8"))
        return data if isinstance(data, dict) else {"themes": []}
    except (OSError, ValueError):
        return {"themes": []}


def load_themes() -> list[dict]:
    return [normalise(t) for t in load_store().get("themes", [])]


def save_themes(themes: list[dict], **meta) -> None:
    store = load_store()
    store.update(meta)
    store["themes"] = themes
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1), "utf-8")
    tmp.replace(path)


def load_seen(theme_id: str) -> set[str]:
    try:
        return set(json.loads(_seen_path(theme_id).read_text("utf-8")))
    except (OSError, ValueError):
        return set()


def save_seen(theme_id: str, ids: set[str]) -> None:
    path = _seen_path(theme_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids)), "utf-8")
    tmp.replace(path)


@contextmanager
def run_lock():
    """One theme run at a time (the hourly timer and "Run now" can overlap)."""
    path = state_dir() / "themes.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ImmichError("A theme run is already in progress.")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ------------------------------------------------------- model + database IO


class Backend:
    """How themes reach CLIP and the similarity table. Swappable in tests."""

    def __init__(self, ml_container: str = "immich_machine_learning", db_container: str = "immich_postgres"):
        self.ml_container = ml_container
        self.db_container = db_container

    def _psql(self, sql: str) -> str:
        return subprocess.run(
            ["docker", "exec", "-i", self.db_container, "psql", "-U", "postgres", "-d", "immich", "-At", "-F", "\t"],
            input=sql, capture_output=True, text=True, timeout=300, check=True,
        ).stdout

    def model_name(self, client: ImmichClient) -> str:
        return client.system_config()["machineLearning"]["clip"]["modelName"]

    def text_vector(self, text: str, model: str) -> list[float]:
        ip = subprocess.run(
            ["docker", "inspect", self.ml_container, "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        boundary = uuid.uuid4().hex
        entries = json.dumps({"clip": {"textual": {"modelName": model}}})
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"entries\"\r\n\r\n{entries}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{text}\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        req = urllib.request.Request(f"http://{ip}:3003/predict", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            vec = json.loads(resp.read())["clip"]
        return json.loads(vec) if isinstance(vec, str) else vec

    def _literal(self, vec: list[float]) -> str:
        return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"

    _ELIGIBLE = """join asset a on a.id = s."assetId"
        where a."deletedAt" is null and a.visibility in ('timeline', 'archive')"""

    def _eligible(self, type_: str | None = None) -> str:
        """Timeline/archive assets; optionally only photos (IMAGE) or only videos (VIDEO)."""
        if type_ in MEDIA_TYPES:
            return self._ELIGIBLE + f" and a.type = '{type_}'"
        return self._ELIGIBLE

    def above(self, vec: list[float], cutoff: float, type_: str | None = None) -> dict[str, float]:
        lit = self._literal(vec)
        out = self._psql(
            f"""select s."assetId", 1 - (s.embedding <=> '{lit}') from smart_search s {self._eligible(type_)}
                and 1 - (s.embedding <=> '{lit}') >= {float(cutoff)};"""
        )
        return {row.split("\t")[0]: float(row.split("\t")[1]) for row in out.splitlines() if "\t" in row}

    def top(self, vec: list[float], limit: int, type_: str | None = None) -> list[tuple[str, float]]:
        lit = self._literal(vec)
        out = self._psql(
            f"""select s."assetId", 1 - (s.embedding <=> '{lit}') from smart_search s {self._eligible(type_)}
                order by s.embedding <=> '{lit}' limit {int(limit)};"""
        )
        return [(r.split("\t")[0], float(r.split("\t")[1])) for r in out.splitlines() if "\t" in r]

    def album_scores(self, vec: list[float], album_id: str) -> dict[str, float]:
        """How well each photo of one album matches a description (same score as smart search)."""
        album_id = str(uuid.UUID(album_id))
        lit = self._literal(vec)
        out = self._psql(
            f"""select s."assetId", 1 - (s.embedding <=> '{lit}') from smart_search s
                join album_asset aa on aa."assetId" = s."assetId" where aa."albumId" = '{album_id}';"""
        )
        return {row.split("\t")[0]: float(row.split("\t")[1]) for row in out.splitlines() if "\t" in row}

    # --- smart albums: one query with every filter ---------------------------------------
    def _query_parts(self, *, vec=None, like=None, media=None, taken_after=None, taken_before=None, people=(),
                     people_match="all", only_unfiled=False, exclude_album_ids=(), must=(), must_not=(),
                     term_cutoff=DEFAULT_CUTOFF):
        qv = None
        if like:
            qv = f"""(select embedding from smart_search where "assetId" = '{uuid.UUID(like)}')"""
        elif vec is not None:
            qv = f"'{self._literal(vec)}'"
        scored = qv is not None or must or must_not
        frm = 'smart_search s join asset a on a.id = s."assetId"' if scored else "asset a"
        where = ["""a."deletedAt" is null""", "a.visibility in ('timeline', 'archive')"]
        if media in MEDIA_TYPES:
            where.append(f"a.type = '{media}'")
        if taken_after:
            where.append(f"""a."localDateTime" >= '{dt.date.fromisoformat(str(taken_after)[:10])}'::date""")
        if taken_before:
            where.append(f"""a."localDateTime" < ('{dt.date.fromisoformat(str(taken_before)[:10])}'::date + 1)""")
        ids = [str(uuid.UUID(p)) for p in people]
        if ids and people_match == "any":
            where.append("""exists (select 1 from asset_face f where f."assetId" = a.id and f."deletedAt" is null"""
                         f""" and f."personGroupId" in ({", ".join(f"'{p}'" for p in ids)}))""")
        else:
            for p in ids:
                where.append("""exists (select 1 from asset_face f where f."assetId" = a.id and f."deletedAt" is null"""
                             f""" and f."personGroupId" = '{p}')""")
        if only_unfiled:
            where.append("""not exists (select 1 from album_asset aa where aa."assetId" = a.id)""")
        albums = [str(uuid.UUID(x)) for x in exclude_album_ids]
        if albums:
            where.append("""not exists (select 1 from album_asset aa where aa."assetId" = a.id"""
                         f""" and aa."albumId" in ({", ".join(f"'{x}'" for x in albums)}))""")
        for tv in must:
            where.append(f"1 - (s.embedding <=> '{self._literal(tv)}') >= {float(term_cutoff)}")
        for tv in must_not:
            where.append(f"1 - (s.embedding <=> '{self._literal(tv)}') < {float(term_cutoff)}")
        score = f"1 - (s.embedding <=> {qv})" if qv else "null"
        order = f"s.embedding <=> {qv}" if qv else """a."localDateTime" desc"""
        return frm, where, score, order

    def eligible_ids(self, **kw) -> set[str]:
        """Ids passing the filters that need the database (people, albums), without any scoring."""
        frm, where, _, _ = self._query_parts(**kw)
        return {line.strip() for line in self._psql(f"select a.id from {frm} where {' and '.join(where)};").splitlines()
                if line.strip()}

    def select(self, *, cutoff=None, limit=None, **kw) -> list[tuple[str, float | None]]:
        """Matching asset ids, best first, with their similarity (None when there is no description)."""
        frm, where, score, order = self._query_parts(**kw)
        if cutoff is not None and score != "null":
            where = where + [f"{score} >= {float(cutoff)}"]
        sql = f"""select a.id, {score} from {frm} where {' and '.join(where)} order by {order}"""
        if limit:
            sql += f" limit {int(limit)}"
        rows = []
        for line in self._psql(sql + ";").splitlines():
            if "\t" in line:
                aid, sc = line.split("\t", 1)
                rows.append((aid, float(sc) if sc else None))
        return rows

    def select_counts(self, cutoffs: list[float], **kw) -> dict[str, int]:
        frm, where, score, _ = self._query_parts(**kw)
        if score == "null":
            total = int(self._psql(f"select count(*) from {frm} where {' and '.join(where)};").strip() or 0)
            return {"total": total}
        cols = ", ".join(f"count(*) filter (where sim >= {float(c)})" for c in cutoffs)
        out = self._psql(f"""with x as (select {score} sim from {frm} where {' and '.join(where)})
                             select count(*), {cols} from x;""").strip().split("\t")
        total, *rest = [int(v) for v in out]
        return {"total": total, **{f"{c:.3f}": n for c, n in zip(cutoffs, rest)}}

    def counts(self, vec: list[float], cutoffs: list[float], type_: str | None = None) -> dict[str, int]:
        lit = self._literal(vec)
        cols = ", ".join(f"count(*) filter (where sim >= {float(c)})" for c in cutoffs)
        out = self._psql(
            f"""with x as (select 1 - (s.embedding <=> '{lit}') sim from smart_search s {self._eligible(type_)})
                select count(*), {cols} from x;"""
        ).strip().split("\t")
        total, *rest = [int(v) for v in out]
        return {"total": total, **{f"{c:.3f}": n for c, n in zip(cutoffs, rest)}}


class SearchPlusEngine:
    """Scores smart albums with the Search+ index (PE-Core G/14) instead of Immich's own model.

    Photo scores come straight from the Search+ index (each photo/video scores as its best
    frame); only a new or changed description needs the model server, and its vector is cached
    in the smart album, so hourly runs normally don't touch the graphics card.
    """

    def __init__(self, store=None, service=None, parts=None):
        self._store, self._service, self._get = store, service, parts

    def _parts(self):
        if self._store is None:              # only opened when a smart album actually uses Search+
            if self._get is None:
                from . import searchplus
                self._get = searchplus.instance
            self._store, self._service = self._get()[:2]
        return self._store, self._service

    def model_name(self) -> str:
        store, _ = self._parts()
        if not store.model:
            raise ValueError("The Search+ index is empty - build it in the Search+ tab first.")
        return store.model

    def text_vector(self, text: str) -> list[float]:
        store, service = self._parts()
        try:
            health = service.ready(wait=240)
            if store.model and health.get("model") != store.model:
                raise ValueError(f"The Search+ index was built with {store.model}, but the model server runs "
                                 f"{health.get('model')}.")
            vec = service.embed_text([text])[0]
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 - model server down / still loading: a normal error for the run
            raise ValueError(f"Search+ model: {exc}") from exc
        return [round(float(x), 6) for x in vec]

    def view(self):
        return self._parts()[0].view()

    def best(self, view, vector):
        from .searchplus import best_scores
        return best_scores(view, vector)


def theme_vector(theme: dict, client: ImmichClient, backend: Backend) -> list[float]:
    """The theme's text vector, cached per CLIP model (recomputed if you switch models)."""
    model = backend.model_name(client)
    cache = theme.setdefault("vectors", {})
    key = f"{model}::{theme['description']}"
    if key not in cache:
        cache.clear()
        cache[key] = backend.text_vector(theme["description"], model)
    return cache[key]


# ------------------------------------------------------------------ themes


def check_media(media) -> str | None:
    if media in (None, "", "all"):
        return None
    if media not in MEDIA_TYPES:
        raise ValueError("media must be IMAGE, VIDEO or empty")
    return media


SOURCES = ("text", "like", "none")
MODES = ("cutoff", "top")
DEFAULTS = {
    "source": "text", "description": "", "like": None, "mode": "cutoff", "cutoff": DEFAULT_CUTOFF, "limit": 200,
    "media": None, "people": [], "people_match": "all", "taken_after": None, "taken_before": None,
    "exclude_albums": [], "only_unfiled": False, "all_of": [], "none_of": [], "archive": False, "favorite": False,
    "enabled": True, "engine": "immich",
}


def normalise(theme: dict) -> dict:
    """Fill in the smart-album fields for themes saved before they existed."""
    for key, value in DEFAULTS.items():
        theme.setdefault(key, list(value) if isinstance(value, list) else value)
    return theme


def _terms(value) -> list[str]:
    if isinstance(value, str):
        value = value.split(",")
    return [t.strip() for t in (value or []) if isinstance(t, str) and t.strip()]


def _date(value, label):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"{label} must be a date (YYYY-MM-DD)") from None


def build_theme(data: dict, existing: dict | None = None) -> dict:
    """Validate what the editor sends and return a complete smart album (new or updated)."""
    t = normalise(dict(existing or {}))
    source = data.get("source", t["source"]) or "text"
    if source not in SOURCES:
        raise ValueError("source must be text, like or none")
    description = str(data.get("description", t["description"]) or "").strip()
    like = str(data.get("like", t["like"]) or "").strip() or None
    if source == "text" and not description:
        raise ValueError("Describe what the photos should show.")
    if source == "like":
        if not like:
            raise ValueError("Paste the ID of the photo to match.")
        try:
            like = str(uuid.UUID(like))
        except ValueError:
            raise ValueError("That is not a photo ID.") from None
    people = data.get("people", t["people"]) or []
    if not isinstance(people, list) or not all(isinstance(p, str) for p in people):
        raise ValueError("people must be a list of person ids")
    for p in people:
        uuid.UUID(p)
    if source == "none" and not people:
        raise ValueError("Pick at least one person (or describe what the photos should show).")
    mode = data.get("mode", t["mode"]) or "cutoff"
    if mode not in MODES:
        raise ValueError("mode must be cutoff or top")
    cutoff = data.get("cutoff", t["cutoff"])
    if not isinstance(cutoff, (int, float)) or isinstance(cutoff, bool) or not 0 < float(cutoff) < 1:
        raise ValueError("The cut-off must be between 0 and 1.")
    limit = data.get("limit", t["limit"])
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise ValueError("Top N must be between 1 and 10,000.")
    engine = data.get("engine", t["engine"]) or "immich"
    if engine not in ENGINES:
        raise ValueError("engine must be immich or searchplus")
    match = data.get("people_match", t["people_match"]) or "all"
    if match not in ("all", "any"):
        raise ValueError("people_match must be all or any")
    excl = data.get("exclude_albums", t["exclude_albums"]) or []
    if not isinstance(excl, list) or not all(isinstance(a, str) for a in excl):
        raise ValueError("exclude_albums must be a list of album names")
    label = description or ("photos like " + like[:8] if like else "people")
    name = str(data.get("name") or t.get("name") or label).strip()
    album = str(data.get("album") or t.get("album") or name).strip()
    t.update({
        "source": source, "description": description, "like": like, "mode": mode,
        "cutoff": round(float(cutoff), 4), "limit": limit, "media": check_media(data.get("media", t["media"])),
        "people": people, "people_match": match,
        "taken_after": _date(data.get("taken_after", t["taken_after"]), "Taken after"),
        "taken_before": _date(data.get("taken_before", t["taken_before"]), "Taken before"),
        "exclude_albums": [a.strip() for a in excl if a.strip()],
        "only_unfiled": bool(data.get("only_unfiled", t["only_unfiled"])),
        "all_of": _terms(data.get("all_of", t["all_of"])), "none_of": _terms(data.get("none_of", t["none_of"])),
        "archive": bool(data.get("archive", t["archive"])), "favorite": bool(data.get("favorite", t["favorite"])),
        "enabled": bool(data.get("enabled", t["enabled"])), "name": name, "album": album, "engine": engine,
    })
    if not existing:
        t.update({"id": uuid.uuid4().hex[:12], "albumId": None, "created": _now(), "lastRun": None, "lastAdded": 0})
    return t


def new_theme(name: str, description: str, cutoff: float = DEFAULT_CUTOFF, album: str | None = None,
              media: str | None = None) -> dict:
    """A description + cut-off smart album (the original theme)."""
    return build_theme({"name": name, "description": description, "cutoff": cutoff, "album": album, "media": media})


def _vector(cache: dict, text: str, model: str, backend) -> list[float]:
    key = f"{model}::{text}"
    if key not in cache:
        cache[key] = backend.text_vector(text, model)
    return cache[key]


def selection(client: ImmichClient, theme: dict, backend, albums: list[dict] | None = None) -> dict:
    """The filters of a smart album as keyword arguments for ``Backend.select``."""
    theme = normalise(theme)
    model = backend.model_name(client) if (theme["source"] == "text" or theme["all_of"] or theme["none_of"]) else ""
    cache = theme.setdefault("vectors", {})
    wanted = {f"{model}::{x}" for x in [theme["description"], *theme["all_of"], *theme["none_of"]] if x}
    for key in list(cache):
        if key not in wanted:
            del cache[key]
    kw = {
        "media": theme["media"], "taken_after": theme["taken_after"], "taken_before": theme["taken_before"],
        "people": theme["people"], "people_match": theme["people_match"], "only_unfiled": theme["only_unfiled"],
        # "must also / must not match" words are text, scored like a description: with a description
        # they use its cut-off; next to "like this photo" (a different score scale) the default one
        "term_cutoff": theme["cutoff"] if theme["source"] == "text" else DEFAULT_CUTOFF,
        "must": [_vector(cache, x, model, backend) for x in theme["all_of"]],
        "must_not": [_vector(cache, x, model, backend) for x in theme["none_of"]],
    }
    if theme["source"] == "text":
        kw["vec"] = _vector(cache, theme["description"], model, backend)
    elif theme["source"] == "like":
        kw["like"] = theme["like"]
    if theme["exclude_albums"]:
        kw["exclude_album_ids"] = _exclude_album_ids(client, theme, albums)
    return kw


def _exclude_album_ids(client: ImmichClient, theme: dict, albums: list[dict] | None) -> list[str]:
    albums = albums if albums is not None else client.list_albums()
    ids = []
    for ref in theme["exclude_albums"]:
        a = next((x for x in albums if x.get("id") == ref), None) or find_album(albums, ref)
        if a:
            ids.append(a["id"])
    return ids


def _sp_vector(cache: dict, text: str, model: str, engine) -> list[float]:
    key = f"{SP_KEY}{model}::{text}"
    if key not in cache:
        cache[key] = engine.text_vector(text)
    return cache[key]


def searchplus_select(client: ImmichClient, theme: dict, backend, engine, *, cutoff=None, limit=None,
                      albums=None) -> tuple[list[tuple[str, float | None]], object]:
    """The Search+ version of ``Backend.select``: (matches best first, every eligible score).

    Same filters as with Immich's model; photos not in the Search+ index (yet) are not scored.
    """
    import numpy as np

    theme = normalise(theme)
    view = engine.view()
    mask = np.asarray(view.live, dtype=bool) & np.asarray(view.indexed, dtype=bool)
    if theme["media"] in MEDIA_TYPES:
        mask &= view.types == theme["media"]
    if theme["taken_after"]:
        mask &= view.taken >= str(theme["taken_after"])[:10]
    if theme["taken_before"]:
        mask &= view.taken <= str(theme["taken_before"])[:10]

    texts = [x for x in [theme["description"] if theme["source"] == "text" else "", *theme["all_of"],
                         *theme["none_of"]] if x]
    model = engine.model_name() if texts else ""
    cache = theme.setdefault("vectors", {})
    wanted = {f"{SP_KEY}{model}::{x}" for x in texts}
    for key in list(cache):
        if key not in wanted:
            del cache[key]

    db = {}
    if theme["people"]:
        db.update(people=theme["people"], people_match=theme["people_match"])
    if theme["only_unfiled"]:
        db["only_unfiled"] = True
    if theme["exclude_albums"]:
        db["exclude_album_ids"] = _exclude_album_ids(client, theme, albums)
    if db:
        allowed = backend.eligible_ids(**db)
        mask &= np.fromiter((i in allowed for i in view.ids), dtype=bool, count=len(view.ids))

    term_cutoff = theme["cutoff"] if theme["source"] == "text" else SP_TERM_CUTOFF
    for term in theme["all_of"]:
        mask &= engine.best(view, _sp_vector(cache, term, model, engine)) >= term_cutoff
    for term in theme["none_of"]:
        mask &= engine.best(view, _sp_vector(cache, term, model, engine)) < term_cutoff

    if theme["source"] == "text":
        best = engine.best(view, _sp_vector(cache, theme["description"], model, engine))
    elif theme["source"] == "like":
        best = engine.best(view, view.asset_vector(theme["like"]))
    else:
        best = None
    idx = np.flatnonzero(mask)
    if best is None:                                  # only people: newest first, nothing to score
        order = idx[np.argsort(view.taken[idx], kind="stable")[::-1]]
        return [(view.ids[i], None) for i in (order[:limit] if limit else order)], None
    eligible = best[idx]
    if cutoff is not None:
        idx = idx[best[idx] >= float(cutoff)]
    order = idx[np.argsort(-best[idx], kind="stable")]
    picked = order[:limit] if limit else order
    return [(view.ids[i], float(best[i])) for i in picked], eligible


def matches_for(client: ImmichClient, theme: dict, backend, albums=None, engine=None) -> list[tuple[str, float | None]]:
    if normalise(theme)["engine"] == "searchplus":
        rows, _ = searchplus_select(client, theme, backend, engine or SearchPlusEngine(), albums=albums,
                                    limit=theme["limit"] if theme["mode"] == "top" else None,
                                    cutoff=theme["cutoff"] if theme["mode"] == "cutoff" else None)
        return rows
    kw = selection(client, theme, backend, albums)
    if theme["mode"] == "top":
        return backend.select(limit=theme["limit"], **kw)
    return backend.select(cutoff=theme["cutoff"], **kw)


def preview(client: ImmichClient, description=None, *, limit: int = 300, backend: Backend | None = None,
            media: str | None = None, spec: dict | None = None, engine=None) -> dict:
    """Best matches with their scores, plus how many photos pass common cut-offs.

    ``spec`` is a full (unsaved) smart album from the editor; the older
    ``description``/``media`` arguments still work.
    """
    backend = backend or Backend()
    if spec is None:
        spec = {"description": description, "media": media}
    theme = build_theme(spec)
    if theme["engine"] == "searchplus":
        items, scores = searchplus_select(client, theme, backend, engine or SearchPlusEngine(), limit=limit)
        if scores is None:
            counts = {"total": len(searchplus_select(client, theme, backend, engine or SearchPlusEngine())[0])}
        else:
            cutoffs = ([0.5, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9] if theme["source"] == "like"
                       else [0.13, 0.14, 0.15, 0.16, 0.17, 0.18, 0.19, 0.20, 0.22])
            counts = {"total": int(len(scores)), **{f"{c:.3f}": int((scores >= c).sum()) for c in cutoffs}}
    else:
        kw = selection(client, theme, backend)
        items = backend.select(limit=limit, **kw)      # "best N" shows past N too (greyed), so N can be picked
        cutoffs = ([0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95] if theme["source"] == "like"   # photo-to-photo scale
                   else [0.06, 0.08, 0.09, 0.10, 0.11, 0.12, 0.13, 0.14, 0.16])
        counts = backend.select_counts(cutoffs, **kw)
    return {"description": theme["description"], "media": theme["media"], "mode": theme["mode"],
            "engine": theme["engine"], "scored": theme["source"] != "none", "counts": counts,
            "items": [{"id": i, "score": round(sc, 4) if sc is not None else None} for i, sc in items]}


def import_rules(raw: dict) -> list[dict]:
    """Turn the old rules file (as plain data) into smart albums (top N, not hourly)."""
    defaults = raw.get("defaults") or {}
    out = []
    for r in raw.get("rules") or []:
        filters = {**(defaults.get("filters") or {}), **(r.get("filters") or {})}
        refine = {**(defaults.get("refine") or {}), **(r.get("refine") or {})}
        actions = {**(defaults.get("actions") or {}), **(r.get("actions") or {})}
        people = filters.get("person_ids") or filters.get("people") or []
        if isinstance(people, str):
            people = [people]
        data = {
            "name": r.get("name") or r.get("album"), "album": r.get("album") or r.get("name"),
            "source": "like" if r.get("like_asset") else ("text" if r.get("query") else "none"),
            "description": r.get("query") or "", "like": r.get("like_asset"),
            "mode": "top", "limit": int(r.get("limit") or defaults.get("limit") or 200),
            "media": filters.get("type"), "people": people, "people_match": r.get("people_match") or "all",
            "taken_after": filters.get("taken_after"), "taken_before": filters.get("taken_before"),
            "only_unfiled": bool(filters.get("only_unfiled")),
            "exclude_albums": r.get("exclude_albums") or defaults.get("exclude_albums") or [],
            "all_of": refine.get("all_of") or [], "none_of": refine.get("none_of") or [],
            "archive": bool(actions.get("archive")), "favorite": bool(actions.get("favorite")),
            "enabled": False,
        }
        try:
            t = build_theme(data)
        except ValueError:
            continue
        t["origin"] = "rule"
        out.append(t)
    return out


def run_theme(client: ImmichClient, theme: dict, *, backend: Backend | None = None, progress=lambda _m: None,
              engine=None) -> dict:
    """Add this smart album's new matches to its album. Never removes anything."""
    backend = backend or Backend()
    theme = normalise(theme)
    albums = client.list_albums()
    matches = dict(matches_for(client, theme, backend, albums, engine))

    album = next((a for a in albums if a.get("id") == theme.get("albumId")), None) or find_album(albums, theme["album"])
    created = False
    if album is None:
        what = theme["description"] or "a smart album"
        album = client.create_album(theme["album"], description=f"{DESCRIPTION_PREFIX}: {what} "
                                                                 "(filled automatically by Immich Organizer)")
        created = True
    theme["albumId"] = album["id"]

    seen = load_seen(theme["id"])
    in_album = set() if created else album_asset_ids(client, album["id"])
    todo = [i for i in matches if i not in seen and i not in in_album]    # already best first

    added: list[str] = []
    for i in range(0, len(todo), 1000):
        for r in client.add_assets_to_album(album["id"], todo[i:i + 1000]):
            if r.get("success"):
                added.append(r["id"])
    if added and theme.get("favorite"):
        client.update_assets(added, isFavorite=True)
    if added and theme.get("archive"):
        client.update_assets(added, visibility="archive")
    # Remember everything that matched and is (or was) in the album, so a
    # photo you remove is never re-added.
    seen |= set(added) | (in_album & set(matches))
    save_seen(theme["id"], seen)

    run_id = None
    if added or created:
        run_id = uuid.uuid4().hex[:12]
        record = {
            "run_id": run_id, "timestamp": _now(), "source": "theme", "theme": theme["id"],
            "note": f"theme {theme['name']}: +{len(added)}",
            "created_albums": [album.get("albumName")] if created else [],
            "added": {album.get("albumName"): added} if added else {},
            "album_ids": {album.get("albumName"): album["id"]},
        }
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    theme["lastRun"] = _now()
    theme["lastAdded"] = len(added)
    theme["lastMatched"] = len(matches)
    progress(f"{theme['name']}: {len(matches)} match, {len(added)} added")
    return {"theme": theme["id"], "name": theme["name"], "album": album.get("albumName"), "albumId": album["id"],
            "created": created, "matched": len(matches), "added": len(added), "runId": run_id}


def run_all(client: ImmichClient, *, only: list[str] | None = None, backend: Backend | None = None,
            progress=lambda _m: None, engine=None) -> list[dict]:
    """Run every enabled theme (or just ``only``). Safe to call from a timer."""
    backend = backend or Backend()
    results = []
    with run_lock():
        themes = load_themes()
        for theme in themes:
            if only is not None and theme["id"] not in only:
                continue
            if only is None and not theme.get("enabled", True):
                continue
            try:
                results.append(run_theme(client, theme, backend=backend, progress=progress, engine=engine))
            except (ImmichError, OSError, subprocess.SubprocessError, KeyError, ValueError) as exc:
                theme["lastError"] = str(exc)
                results.append({"theme": theme["id"], "name": theme["name"], "error": str(exc)})
                progress(f"{theme['name']}: failed: {exc}")
            else:
                theme.pop("lastError", None)
        save_themes(themes)
    return results


def forget_removals(theme_id: str) -> None:
    """Let a theme re-add photos you had taken out of its album."""
    save_seen(theme_id, set())
