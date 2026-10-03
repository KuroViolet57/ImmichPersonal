"""Search by the AI Tagger's tags and by the description text (docs/AI-TAGGER.md, "Search by tags and description").

Neither is something Immich's smart search can filter on: its ``tagIds`` are Immich's own tags, and the AI Tagger's
tags live in the asset's description block and in the tagger store's ``asset_tags`` table. So the panel works out the
set of assets that match (the "candidates") and every search then ranks, or lists, only those:

* tags: ``Store.ids_with_tags``: exact tags (normalised like every tag), all of them or any of them;
* description: Immich's metadata search with ``description`` ("the description contains this text", case and accents
  ignored), paged, newest first, up to ``DESCRIPTION_CAP`` assets;
* both: the candidates are the assets in both sets.

The routes (``web/server.py``) take it from there: Search+ ranks only the candidates' vectors
(``searchplus.search(only=...)``), Immich's smart search is read page by page and the candidates picked out of it
(``engine.evaluate_rule(only_ids=...)``), and with no text or reference photo the candidates are listed newest first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .aitagger import norm_tag
from .client import ImmichClient, ImmichError

MAX_TAGS = 20               # most tags in one request
MAX_DESCRIPTION = 200       # longest "description contains" text
DESCRIPTION_CAP = 5000      # most assets collected from Immich's description search; ``capped`` says it had more
SCAN_CAP = 10000            # most results of Immich's smart-search ranking looked at when it is filtered by candidates
MODES = ("all", "any")
# Search filters Immich's smart search takes but its metadata search does not
NOT_FOR_METADATA = {"language"}
# The filters (in Immich's names) that can be applied to a plain list of assets from the library list the tagger store
# keeps; every other one (people, "in no album", ...) is something only Immich can answer
LOCAL_FILTERS = {"type", "takenAfter", "takenBefore"}


@dataclass
class TagFilter:
    tags: list[str]             # normalised, no repeats, in the order given
    mode: str                   # "all" | "any"
    description: str


@dataclass
class Found:
    """The assets that pass the tag and description filters."""

    ids: set[str]
    capped: bool = False                                    # the description search stopped at DESCRIPTION_CAP
    items: dict[str, dict] = field(default_factory=dict)    # id -> Immich's asset, when the description was searched


def parse(body: dict) -> TagFilter | None:
    """The ``tags``, ``tagMode`` and ``description`` of a search request, checked (ValueError says what is wrong);
    None when it asks for neither tags nor a description."""
    raw = body.get("tags")
    if raw is None:
        raw = []
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        raise ValueError("tags must be a list of tag names")
    if len(raw) > MAX_TAGS:
        raise ValueError(f"At most {MAX_TAGS} tags at a time.")
    tags: list[str] = []
    for text in raw:
        if not text.strip():
            continue
        tag = norm_tag(text)
        if not tag:
            raise ValueError(f"“{text.strip()[:40]}” is not a usable tag.")
        if tag not in tags:
            tags.append(tag)
    mode = body.get("tagMode")
    mode = "all" if mode is None else mode
    if mode not in MODES:
        raise ValueError("tagMode must be all or any")
    description = body.get("description")
    description = "" if description is None else description
    if not isinstance(description, str):
        raise ValueError("description must be text")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"The description text can be at most {MAX_DESCRIPTION} characters.")
    return TagFilter(tags, mode, description) if tags or description else None


def description_items(client: ImmichClient, text: str, filters: dict | None = None,
                      cap: int | None = None) -> tuple[list[dict], bool]:
    """Assets whose Immich description contains ``text``, newest first (``POST /search/metadata``), at most ``cap``
    (``DESCRIPTION_CAP``) of them, and whether Immich had more. ``filters`` (a search request's filters in Immich's own
    names: type, dates, people, ...) narrow the search on Immich's side, so the cap is spent on assets that can still be
    used."""
    cap = DESCRIPTION_CAP if cap is None else cap
    payload = {k: v for k, v in (filters or {}).items() if k not in NOT_FOR_METADATA and v is not None}
    payload.update(description=text, order="desc")
    try:
        items = list(client.iter_metadata_search(payload, limit=cap + 1))
    except ImmichError as exc:
        if exc.status == 400:
            raise ValueError(f"Immich did not accept the description search: {exc.body[:200] or exc}") from exc
        raise
    return items[:cap], len(items) > cap


def find(store, client: ImmichClient, flt: TagFilter, immich_filters: dict | None = None) -> Found:
    """The candidates for this filter: tags from the tagger store, description from Immich, both when both are given."""
    tag_ids = store.ids_with_tags(flt.tags, flt.mode) if flt.tags else None
    if not flt.description:
        return Found(ids=tag_ids if tag_ids is not None else set())
    if tag_ids is not None and not tag_ids:
        return Found(ids=set())                         # no asset has the tags: nothing the description could add
    items, capped = description_items(client, flt.description, immich_filters)
    by_id = {a["id"]: a for a in items if a.get("id")}
    ids = set(by_id) if tag_ids is None else set(by_id) & tag_ids
    return Found(ids=ids, capped=capped, items=by_id)


def _day(asset: dict) -> str:
    return (asset.get("localDateTime") or asset.get("fileCreatedAt") or "")[:10]


def newest_first(store, found: Found, *, media: str | None = None, after: str | None = None, before: str | None = None,
                 skip=(), limit: int = 200) -> tuple[list[dict], int]:
    """The candidates, newest first (the first ``limit`` of them) and how many there are after the type and date
    filters (``after`` / ``before``: ``YYYY-MM-DD``) and ``skip``. Dates and names come from the library list the tagger
    store keeps, or, for a description search, from what Immich answered."""
    if not found.items:
        return store.newest(found.ids, media=media, after=after, before=before, skip=skip, limit=limit)
    after, before, skip = (after or "")[:10], (before or "")[:10], set(skip)
    rows = []
    for aid in found.ids:
        a = found.items[aid]
        day = _day(a)
        if aid in skip or (media and a.get("type") != media) or (after and day < after) or (before and day > before):
            continue
        rows.append({"id": aid, "name": a.get("originalFileName") or "", "type": a.get("type") or "IMAGE", "date": day})
    rows.sort(key=lambda r: r["id"])
    rows.sort(key=lambda r: r["date"], reverse=True)    # two stable sorts: newest first, then by id
    return rows[:limit], len(rows)


def _count(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def summary(flt: TagFilter, found: Found, ranking: str, **extra) -> dict:
    """The ``filters`` object of a search answer. ``ranking`` says how the candidates were put in order: ``none``
    (listed newest first), ``searchplus`` (the Search+ vectors of the candidates only; ``ranked`` is how many of them
    are in its index) or ``immich`` (Immich's ranking looked through; ``scanned`` results, ``scanCapped`` when the
    cap stopped it). ``text`` is the line the screens show."""
    out = {"tags": flt.tags, "tagMode": flt.mode, "description": flt.description, "candidates": len(found.ids),
           "capped": found.capped, "ranking": ranking, **extra}
    out["text"] = describe(out)
    return out


def describe(f: dict) -> str:
    """"Filtered to 312 assets with all of: anthro, wolf." and what else the reader should know."""
    what = []
    tags = f.get("tags") or []
    if len(tags) == 1:
        what.append(f"with the tag {tags[0]}")
    elif tags:
        what.append(f"with {'all' if f.get('tagMode') != 'any' else 'any'} of: {', '.join(tags)}")
    if f.get("description"):
        what.append(f"whose description contains “{f['description']}”")
    text = f"Filtered to {_count(f.get('candidates') or 0, 'asset')} {' and '.join(what)}."
    if f.get("capped"):
        text += (f" Immich found more than {DESCRIPTION_CAP:,} assets with that description text; only the newest "
                 f"{DESCRIPTION_CAP:,} were used.")
    ranking = f.get("ranking")
    if ranking == "searchplus":
        text += f" Ranked only those with the Search+ model ({f.get('ranked') or 0:,} of them are in its index)."
    elif ranking == "immich":
        text += (f" Looked through the top {f.get('scanned') or 0:,} of Immich's ranking for them; the tags come from "
                 "the AI Tagger, which Immich's smart search cannot rank on.")
        if f.get("scanCapped"):
            text += (f" It stopped at {SCAN_CAP:,}, so matches further down are missing; the Search+ model ranks "
                     "exactly the matching assets.")
    else:
        text += " Newest first."
    return text
