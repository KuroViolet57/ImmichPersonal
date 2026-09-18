"""Turning rules into a reviewable plan, and plans into album changes."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .client import ImmichClient, ImmichError
from .config import state_dir
from .rules import Rule, RuleSet

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class RuleMatch:
    """What one rule found, and what applying it would change."""

    rule: Rule
    album_name: str
    album_id: str | None = None
    album_exists: bool = False
    matched: list[dict] = field(default_factory=list)
    already_in_album: list[str] = field(default_factory=list)
    to_add: list[dict] = field(default_factory=list)
    considered: int = 0
    dropped_by_refine: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def summary(self) -> str:
        if self.error:
            return f"{self.rule.name}: ERROR - {self.error}"
        bits = [f"{len(self.to_add)} to add"]
        if self.already_in_album:
            bits.append(f"{len(self.already_in_album)} already there")
        if self.dropped_by_refine:
            bits.append(f"{self.dropped_by_refine} dropped by refine")
        return f"{self.rule.name} -> {self.album_name}: " + ", ".join(bits)


@dataclass
class Plan:
    entries: list[RuleMatch]
    created_at: str = field(default_factory=_now)
    server_url: str = ""

    @property
    def total_to_add(self) -> int:
        return sum(len(e.to_add) for e in self.entries)

    @property
    def errors(self) -> list[RuleMatch]:
        return [e for e in self.entries if e.error]

    def albums_to_create(self) -> list[str]:
        return [e.album_name for e in self.entries if e.ok and not e.album_exists]


@dataclass
class ApplyResult:
    run_id: str
    created_albums: list[str] = field(default_factory=list)
    added: dict[str, list[str]] = field(default_factory=dict)  # album name -> asset ids
    skipped: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    archived: int = 0
    favorited: int = 0

    @property
    def total_added(self) -> int:
        return sum(len(v) for v in self.added.values())


# ------------------------------------------------------------------ album help


def find_album(albums: Iterable[dict], name: str) -> dict | None:
    """Match an album by name, exact first then case-insensitive.

    Immich lets two albums share a name, so an ambiguous case-insensitive
    match is treated as "not found" rather than guessing wrong.
    """
    albums = list(albums)
    exact = [a for a in albums if a.get("albumName") == name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ImmichError(
            f"{len(exact)} albums are named {name!r}. Rename one, or target it by a unique name."
        )
    folded = [a for a in albums if (a.get("albumName") or "").casefold() == name.casefold()]
    if len(folded) == 1:
        return folded[0]
    if len(folded) > 1:
        raise ImmichError(
            f"{len(folded)} albums differ from {name!r} only by capitalisation. "
            "Rename one so the target is unambiguous."
        )
    return None


def album_asset_ids(client: ImmichClient, album_id: str, *, page_size: int = 1000) -> set[str]:
    """Every asset id currently in an album.

    ``AlbumResponseDto`` carries only a count, not the asset list, so album
    membership comes from a metadata search scoped to the album.
    """
    ids: set[str] = set()
    page = 1
    while True:
        body = {"albumIds": [album_id], "size": page_size, "page": page, "withExif": False}
        assets = (client.search_metadata(body) or {}).get("assets") or {}
        items = assets.get("items") or []
        if not items:
            break
        ids.update(a["id"] for a in items if a.get("id"))
        if not assets.get("nextPage"):
            break
        page += 1
    return ids


# --------------------------------------------------------------------- matching


def _refine_ids(client: ImmichClient, rule: Rule, phrase: str) -> set[str]:
    """Run one refinement search and return its asset ids as a set."""
    payload = dict(rule.filters)
    payload["query"] = phrase
    return {a["id"] for a in client.iter_smart_search(payload, limit=rule.refine.pool) if a.get("id")}


def evaluate_rule(client: ImmichClient, rule: Rule, *, progress: Progress = _noop) -> list[dict]:
    """Return the assets a rule matches, best match first.

    Smart search exposes no similarity score, so precision is bought two ways:
    a hard top-N cap (``limit``), and optional ``refine`` searches that the
    candidate must also appear in (``all_of``) or must not (``none_of``).
    """
    refining = bool(rule.refine.all_of or rule.refine.none_of)
    # When refining we search deeper than `limit`, because the intersection
    # will discard some of the candidates before the cap is applied.
    search_depth = max(rule.limit, rule.refine.pool) if refining else rule.limit

    progress(f"  searching {rule.describe_match()} (top {search_depth})")
    candidates = list(client.iter_smart_search(rule.search_payload(), limit=search_depth))
    if not refining:
        return candidates[: rule.limit]

    keep: set[str] | None = None
    for phrase in rule.refine.all_of:
        progress(f"  refine all_of: {phrase!r}")
        found = _refine_ids(client, rule, phrase)
        keep = found if keep is None else (keep & found)

    drop: set[str] = set()
    for phrase in rule.refine.none_of:
        progress(f"  refine none_of: {phrase!r}")
        drop |= _refine_ids(client, rule, phrase)

    result = []
    for asset in candidates:
        asset_id = asset.get("id")
        if not asset_id or asset_id in drop:
            continue
        if keep is not None and asset_id not in keep:
            continue
        result.append(asset)
        if len(result) >= rule.limit:
            break
    return result


def build_plan(
    client: ImmichClient,
    ruleset: RuleSet,
    *,
    only: list[str] | None = None,
    progress: Progress = _noop,
) -> Plan:
    """Evaluate every enabled rule without changing anything."""
    rules = ruleset.enabled_rules()
    if only:
        wanted = {name.casefold() for name in only}
        rules = [r for r in rules if r.name.casefold() in wanted]
        missing = wanted - {r.name.casefold() for r in rules}
        if missing:
            raise ImmichError(f"No rule named: {', '.join(sorted(missing))}")
    if not rules:
        raise ImmichError("No enabled rules to run.")

    albums = client.list_albums()
    plan = Plan(entries=[], server_url=client.base_url)
    membership_cache: dict[str, set[str]] = {}

    for rule in rules:
        progress(f"[{rule.name}] {rule.describe_match()} -> album {rule.album!r}")
        entry = RuleMatch(rule=rule, album_name=rule.album)
        try:
            album = find_album(albums, rule.album)
            if album:
                entry.album_id = album["id"]
                entry.album_exists = True
                if entry.album_id not in membership_cache:
                    progress(f"  reading existing album ({album.get('assetCount', 0)} assets)")
                    membership_cache[entry.album_id] = album_asset_ids(client, entry.album_id)
                existing = membership_cache[entry.album_id]
            elif not rule.create_album:
                raise ImmichError(
                    f"Album {rule.album!r} does not exist and create_album is false."
                )
            else:
                existing = set()

            matched = evaluate_rule(client, rule, progress=progress)
            entry.matched = matched
            entry.considered = len(matched)
            entry.already_in_album = [a["id"] for a in matched if a.get("id") in existing]
            entry.to_add = [a for a in matched if a.get("id") not in existing]
            progress("  " + entry.summary())
        except ImmichError as exc:
            entry.error = str(exc)
            progress(f"  failed: {exc}")
        plan.entries.append(entry)

    return plan


# ---------------------------------------------------------------------- journal


def journal_path() -> Path:
    return state_dir() / "journal.jsonl"


def write_journal(result: ApplyResult, plan: Plan) -> Path:
    """Append an apply record so `undo` can reverse exactly what was done."""
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": result.run_id,
        "timestamp": _now(),
        "server_url": plan.server_url,
        "created_albums": result.created_albums,
        "added": result.added,
        "album_ids": {
            e.album_name: e.album_id for e in plan.entries if e.album_id and e.ok
        },
        "archived": result.archived,
        "favorited": result.favorited,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return path


def read_journal(limit: int = 20) -> list[dict]:
    path = journal_path()
    if not path.exists():
        return []
    records = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a truncated line should not make history unreadable
    return records[-limit:]


# ------------------------------------------------------------------------ apply


def apply_plan(client: ImmichClient, plan: Plan, *, progress: Progress = _noop) -> ApplyResult:
    """Execute a plan: create albums as needed, add assets, run extra actions."""
    result = ApplyResult(run_id=uuid.uuid4().hex[:12])

    for entry in plan.entries:
        if not entry.ok:
            continue
        asset_ids = [a["id"] for a in entry.to_add if a.get("id")]
        if not asset_ids and entry.album_exists:
            progress(f"[{entry.rule.name}] nothing to add")
            continue

        try:
            if not entry.album_id:
                progress(f"[{entry.rule.name}] creating album {entry.album_name!r}")
                album = client.create_album(
                    entry.album_name,
                    description=f"Filed by immich-organizer rule {entry.rule.name!r}",
                )
                entry.album_id = album.get("id")
                entry.album_exists = True
                result.created_albums.append(entry.album_name)

            if not asset_ids:
                continue

            progress(f"[{entry.rule.name}] adding {len(asset_ids)} asset(s) to {entry.album_name!r}")
            responses = client.add_assets_to_album(entry.album_id, asset_ids)

            added, skipped = [], 0
            for item in responses:
                if item.get("success"):
                    added.append(item["id"])
                elif item.get("error") == "duplicate":
                    skipped += 1
                else:
                    result.failures.append(
                        f"{entry.album_name}/{item.get('id')}: {item.get('error') or 'unknown error'}"
                    )
            if added:
                result.added.setdefault(entry.album_name, []).extend(added)
            if skipped:
                result.skipped[entry.album_name] = result.skipped.get(entry.album_name, 0) + skipped

            # Extra actions apply only to assets this run actually filed.
            if added and entry.rule.actions.favorite:
                client.update_assets(added, isFavorite=True)
                result.favorited += len(added)
            if added and entry.rule.actions.archive:
                client.update_assets(added, visibility="archive")
                result.archived += len(added)

        except ImmichError as exc:
            result.failures.append(f"{entry.rule.name}: {exc}")
            progress(f"[{entry.rule.name}] failed: {exc}")

    if result.total_added or result.created_albums:
        write_journal(result, plan)
    return result


def undo_run(client: ImmichClient, record: dict, *, progress: Progress = _noop) -> tuple[int, list[str]]:
    """Remove the assets a previous run added. Albums it created are left alone.

    Deleting an album would also discard anything added to it since, so the
    safer move is to empty out only what this tool put there.
    """
    album_ids = record.get("album_ids") or {}
    removed = 0
    failures: list[str] = []
    for album_name, asset_ids in (record.get("added") or {}).items():
        album_id = album_ids.get(album_name)
        if not album_id:
            failures.append(f"{album_name}: album id not recorded, skipping")
            continue
        if not asset_ids:
            continue
        progress(f"removing {len(asset_ids)} asset(s) from {album_name!r}")
        try:
            responses = client.remove_assets_from_album(album_id, list(asset_ids))
        except ImmichError as exc:
            failures.append(f"{album_name}: {exc}")
            continue
        for item in responses:
            if item.get("success"):
                removed += 1
            else:
                failures.append(f"{album_name}/{item.get('id')}: {item.get('error') or 'failed'}")
    return removed, failures
