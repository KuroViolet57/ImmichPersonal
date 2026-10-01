"""Album management: list, inspect, move/copy between albums, merge, rename, delete.

Everything that changes an album is journalled (the same journal the rest of
the tool uses), so History -> Undo can reverse it:

* photos added to an album are taken back out,
* photos removed from an album are put back,
* a deleted album is re-created with the same name, description and photos,
* a renamed album gets its old name back.

Albums in Immich are lists of references: moving a photo between albums or
deleting an album never deletes the photo itself.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import uuid
from typing import Iterable

from .client import ImmichClient, ImmichError
from .engine import album_asset_ids, find_album, journal_path

PAGE = 1000


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _journal(record: dict) -> str:
    record.setdefault("run_id", uuid.uuid4().hex[:12])
    record.setdefault("timestamp", _now())
    record.setdefault("source", "albums")
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record["run_id"]


# --------------------------------------------------------------------- reading


def summarise_albums(client: ImmichClient) -> list[dict]:
    out = []
    for a in client.list_albums():
        out.append({
            "id": a.get("id"),
            "name": a.get("albumName") or "",
            "count": a.get("assetCount", 0),
            "thumb": a.get("albumThumbnailAssetId"),
            "description": a.get("description") or "",
            "shared": bool(a.get("shared")),
            "createdAt": a.get("createdAt"),
            "updatedAt": a.get("updatedAt"),
            "startDate": a.get("startDate"),
            "endDate": a.get("endDate"),
        })
    return out


def album_items(client: ImmichClient, album_id: str) -> list[dict]:
    """Every asset in an album (paged metadata search), lightly projected."""
    items, page = [], 1
    while True:
        data = (client.search_metadata({"albumIds": [album_id], "size": PAGE, "page": page, "withExif": False}) or {})
        assets = data.get("assets") or {}
        for a in assets.get("items") or []:
            items.append({
                "id": a.get("id"),
                "name": a.get("originalFileName"),
                "type": a.get("type"),
                "taken": a.get("localDateTime") or a.get("fileCreatedAt") or "",
                "uploaded": a.get("createdAt") or "",
            })
        if not assets.get("nextPage"):
            return items
        page += 1


def added_to_album_times(album_id: str) -> dict[str, str] | None:
    """asset id -> when it was added to the album, read from Postgres.

    The API does not expose this; Immich stores it in ``album_asset.createdAt``.
    Returns None when the database is not reachable (e.g. no docker access).
    Photos added in one go share one timestamp.
    """
    try:
        uuid.UUID(album_id)
    except ValueError:
        return None
    sql = f'select "assetId", "createdAt" from album_asset where "albumId" = \'{album_id}\''
    try:
        out = subprocess.run(
            ["docker", "exec", "immich_postgres", "psql", "-U", "postgres", "-d", "immich", "-At", "-F", "\t", "-c", sql],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    times = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            times[parts[0]] = parts[1]
    return times


# --------------------------------------------------------------------- helpers


def _resolve(client: ImmichClient, ref: str, *, create: bool, albums: list[dict] | None = None) -> tuple[dict, bool]:
    """Album by id or name; optionally create it. Returns (album, created)."""
    albums = albums if albums is not None else client.list_albums()
    for a in albums:
        if a.get("id") == ref:
            return a, False
    album = find_album(albums, ref)
    if album:
        return album, False
    if not create:
        raise ImmichError(f"No album called {ref!r}.")
    if not ref.strip():
        raise ImmichError("An album name is required.")
    return client.create_album(ref.strip(), description="Created by immich-organizer"), True


def _ok_ids(responses: Iterable[dict]) -> tuple[list[str], list[str], list[str]]:
    added, dup, failed = [], [], []
    for r in responses:
        if r.get("success"):
            added.append(r["id"])
        elif r.get("error") == "duplicate":
            dup.append(r["id"])
        else:
            failed.append(f"{r.get('id')}: {r.get('error') or 'failed'}")
    return added, dup, failed


def _chunks(ids: list[str], n: int = 1000):
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def _add(client, album_id, ids):
    added, dup, failed = [], [], []
    for part in _chunks(ids):
        a, d, f = _ok_ids(client.add_assets_to_album(album_id, part))
        added += a; dup += d; failed += f
    return added, dup, failed


def _remove(client, album_id, ids):
    removed, failed = [], []
    for part in _chunks(ids):
        for r in client.remove_assets_from_album(album_id, part):
            if r.get("success"):
                removed.append(r["id"])
            else:
                failed.append(f"{r.get('id')}: {r.get('error') or 'failed'}")
    return removed, failed


# --------------------------------------------------------------------- actions


def transfer(client: ImmichClient, source_id: str, asset_ids: list[str], target: str, *, move: bool) -> dict:
    """Copy or move photos from one album to another (existing or new).

    Moving only takes the photos out of the *source* album; other albums they
    are in are left alone. Photos are added to the target first, so a failure
    never leaves a photo in neither album.
    """
    if not asset_ids:
        raise ImmichError("Select at least one photo.")
    albums = client.list_albums()
    source, _ = _resolve(client, source_id, create=False, albums=albums)
    dest, created = _resolve(client, target, create=True, albums=albums)
    if dest["id"] == source["id"]:
        raise ImmichError("The target is the album you are moving from.")
    added, dup, failed = _add(client, dest["id"], asset_ids)
    removed: list[str] = []
    if move:
        in_target = set(added) | set(dup)
        removed, rfail = _remove(client, source["id"], [i for i in asset_ids if i in in_target])
        failed += rfail
    run_id = _journal({
        "note": f"{'move' if move else 'copy'} {len(asset_ids)} from {source.get('albumName')} -> {dest.get('albumName')}",
        "created_albums": [dest.get("albumName")] if created else [],
        "added": {dest.get("albumName"): added} if added else {},
        "album_ids": {dest.get("albumName"): dest["id"]},
        "removed": {source["id"]: {"name": source.get("albumName"), "ids": removed}} if removed else {},
    })
    return {"runId": run_id, "target": dest.get("albumName"), "targetId": dest["id"], "created": created,
            "added": len(added), "alreadyThere": len(dup), "removedFromSource": len(removed), "failures": failed[:20]}


def remove_photos(client: ImmichClient, album_id: str, asset_ids: list[str]) -> dict:
    album, _ = _resolve(client, album_id, create=False)
    removed, failed = _remove(client, album["id"], asset_ids)
    run_id = _journal({
        "note": f"remove {len(removed)} from {album.get('albumName')}",
        "removed": {album["id"]: {"name": album.get("albumName"), "ids": removed}} if removed else {},
    })
    return {"runId": run_id, "removed": len(removed), "failures": failed[:20]}


def merge(client: ImmichClient, source_ids: list[str], target: str, *, delete_sources: bool = True,
          replace_target: bool = False) -> dict:
    """Pour several albums into one (existing or new).

    ``delete_sources`` deletes the emptied source albums (their photos live on
    in the target). ``replace_target`` first empties the target, i.e. the
    target ends up with exactly the sources' photos ("overwrite").
    """
    albums = client.list_albums()
    dest, created = _resolve(client, target, create=True, albums=albums)
    sources = []
    for sid in source_ids:
        album, _ = _resolve(client, sid, create=False, albums=albums)
        if album["id"] != dest["id"]:
            sources.append(album)
    if not sources:
        raise ImmichError("Pick at least one album other than the target.")

    removed_from_target: list[str] = []
    wanted: list[str] = []
    seen: set[str] = set()
    contents: dict[str, list[str]] = {}
    for album in sources:
        ids = sorted(album_asset_ids(client, album["id"]))
        contents[album["id"]] = ids
        for i in ids:
            if i not in seen:
                seen.add(i); wanted.append(i)

    if replace_target and not created:
        current = album_asset_ids(client, dest["id"])
        removed_from_target, _ = _remove(client, dest["id"], [i for i in current if i not in seen])

    added, dup, failed = _add(client, dest["id"], wanted)
    deleted = []
    if delete_sources:
        in_target = set(added) | set(dup)
        for album in sources:
            if all(i in in_target for i in contents[album["id"]]):
                client.delete_album(album["id"])
                deleted.append({"name": album.get("albumName"), "description": album.get("description") or "",
                                "ids": contents[album["id"]]})
            else:
                failed.append(f"kept {album.get('albumName')!r}: not all of its photos reached the target")

    run_id = _journal({
        "note": f"merge {len(sources)} album(s) -> {dest.get('albumName')}",
        "created_albums": [dest.get("albumName")] if created else [],
        "added": {dest.get("albumName"): added} if added else {},
        "album_ids": {dest.get("albumName"): dest["id"]},
        "removed": {dest["id"]: {"name": dest.get("albumName"), "ids": removed_from_target}} if removed_from_target else {},
        "deleted_albums": deleted,
    })
    return {"runId": run_id, "target": dest.get("albumName"), "targetId": dest["id"], "created": created,
            "added": len(added), "alreadyThere": len(dup), "deletedAlbums": [d["name"] for d in deleted],
            "removedFromTarget": len(removed_from_target), "failures": failed[:20]}


def delete_albums(client: ImmichClient, album_ids: list[str]) -> dict:
    """Delete albums (never their photos); contents are journalled for undo."""
    albums = client.list_albums()
    deleted = []
    for aid in album_ids:
        album, _ = _resolve(client, aid, create=False, albums=albums)
        ids = sorted(album_asset_ids(client, album["id"]))
        client.delete_album(album["id"])
        deleted.append({"name": album.get("albumName"), "description": album.get("description") or "", "ids": ids})
    run_id = _journal({"note": f"delete {len(deleted)} album(s)", "deleted_albums": deleted})
    return {"runId": run_id, "deleted": [d["name"] for d in deleted]}


def rename(client: ImmichClient, album_id: str, new_name: str) -> dict:
    new_name = (new_name or "").strip()
    if not new_name:
        raise ImmichError("The new name is empty.")
    album, _ = _resolve(client, album_id, create=False)
    client.update_album(album["id"], albumName=new_name)
    run_id = _journal({"note": f"rename {album.get('albumName')} -> {new_name}",
                       "renamed": {album["id"]: album.get("albumName")}})
    return {"runId": run_id, "name": new_name}


def create(client: ImmichClient, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ImmichError("Give the album a name.")
    if find_album(client.list_albums(), name):
        raise ImmichError(f"An album called {name!r} already exists.")
    album = client.create_album(name, description="Created by immich-organizer")
    return {"id": album.get("id"), "name": name}
