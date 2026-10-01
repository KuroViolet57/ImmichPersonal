"""Measure how sharp each detected face is, so blurry "people" can be reviewed.

Immich keeps a face when its detector is confident it *is* a face; it has no
notion of whether the face is sharp enough to recognise, and it does not store
the detector's score either. This module adds that missing signal.

For every face assigned to a person it cuts the face out of the preview image
the detector ran on, scales it to 112x112 -- the input size of the recognition
model -- and takes the variance of the Laplacian (a standard blur measure).
The crop's contrast is normalised first, so dark or low-contrast faces are not
penalised. Tiny faces are stretched and lose detail, blurry faces have none,
sharp faces of any size keep it. Known blind spot: heavy pixelation and JPEG blockiness
create edges and can look "sharp", so the result is for review, not for
automatic deletion.

Needs numpy and Pillow (``apt install python3-numpy python3-pil`` or
``pip install numpy pillow``); the rest of immich-organizer does not.

Reads face boxes straight from Immich's Postgres container (read-only) and
preview files from the library folder, because the API has no bulk face list.

    python3 -m immich_organizer.face_quality --out face-quality.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

CROP = 112  # ArcFace input size used by Immich's recognition models

FACES_SQL = """
select fa.id, fa."assetId", fa."personGroupId", p.name,
       fa."imageWidth", fa."imageHeight",
       fa."boundingBoxX1", fa."boundingBoxY1", fa."boundingBoxX2", fa."boundingBoxY2",
       af.path
from asset_face fa
join person p on p."personGroupId" = fa."personGroupId"
join asset_file af on af."assetId" = fa."assetId" and af.type = 'preview'
where fa."deletedAt" is null and fa."isVisible"
"""


@dataclass
class Face:
    id: str
    asset_id: str
    person_id: str
    person_name: str
    image_w: int
    image_h: int
    box: tuple[int, int, int, int]
    preview: str


def laplacian_variance(gray):
    """Contrast-normalised variance of the 4-neighbour Laplacian, x1000.

    The crop is standardised (zero mean, unit variance) first, so a dark or
    low-contrast but sharp face is not mistaken for a blurry one: what is
    measured is fine detail *relative to* the face's own brightness range.
    """
    import numpy as np

    g = np.asarray(gray, dtype=np.float64)
    std = g.std()
    if std < 1e-6:
        return 0.0
    g = (g - g.mean()) / std
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1] + g[2:, 1:-1]
        + g[1:-1, :-2] + g[1:-1, 2:]
    )
    return float(lap.var()) * 1000.0


def face_sharpness(image, face: Face) -> float:
    """Sharpness of one face cut out of an already-opened PIL image."""
    from PIL import Image

    w, h = image.size
    sx = w / face.image_w if face.image_w else 1.0
    sy = h / face.image_h if face.image_h else 1.0
    x1, y1, x2, y2 = face.box
    left, top = max(0, int(x1 * sx)), max(0, int(y1 * sy))
    right, bottom = min(w, int(round(x2 * sx))), min(h, int(round(y2 * sy)))
    if right - left < 2 or bottom - top < 2:
        return 0.0
    crop = image.crop((left, top, right, bottom)).convert("L").resize((CROP, CROP), Image.BILINEAR)
    return laplacian_variance(crop)


def score_faces(faces: list[Face], path_for, *, workers: int = 8, progress=lambda _m: None) -> dict[str, float]:
    """Face id -> sharpness. Each preview file is decoded once for all its faces."""
    from PIL import Image

    by_file: dict[str, list[Face]] = defaultdict(list)
    for face in faces:
        by_file[face.preview].append(face)

    def one(item):
        preview, group = item
        out = {}
        try:
            with Image.open(path_for(preview)) as im:
                im.load()
                for face in group:
                    out[face.id] = face_sharpness(im, face)
        except OSError:
            pass  # missing/corrupt preview: leave those faces unscored
        return out

    scores: dict[str, float] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(one, by_file.items()):
            scores.update(part)
            done += 1
            if done % 1000 == 0:
                progress(f"  {done}/{len(by_file)} previews")
    return scores


def summarise(faces: list[Face], scores: dict[str, float]) -> dict[str, dict]:
    """Per person: face count, best and median sharpness, and the sharpest face."""
    people: dict[str, list[Face]] = defaultdict(list)
    for face in faces:
        if face.id in scores:
            people[face.person_id].append(face)
    out = {}
    for person_id, group in people.items():
        values = [scores[f.id] for f in group]
        best = max(group, key=lambda f: scores[f.id])
        out[person_id] = {
            "name": group[0].person_name,
            "faces": len(group),
            "best": round(scores[best.id], 1),
            "median": round(statistics.median(values), 1),
            "bestFaceId": best.id,
            "bestAssetId": best.asset_id,
        }
    return out


# ------------------------------------------------------------------ plumbing


def load_faces_from_docker(container: str = "immich_postgres", db: str = "immich", user: str = "postgres") -> list[Face]:
    rows = subprocess.run(
        ["docker", "exec", container, "psql", "-U", user, "-d", db, "-At", "-F", "\t", "-c", FACES_SQL],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    faces = []
    for row in rows:
        parts = row.split("\t")
        if len(parts) != 11:
            continue
        fid, aid, pid, name, iw, ih, x1, y1, x2, y2, path = parts
        faces.append(Face(fid, aid, pid, name, int(iw), int(ih), (int(x1), int(y1), int(x2), int(y2)), path))
    return faces


def library_root_from_docker(container: str = "immich_server") -> tuple[str, str]:
    """(path inside the container, path on this machine) of the upload folder."""
    mounts = json.loads(subprocess.run(
        ["docker", "inspect", container, "--format", "{{json .Mounts}}"],
        capture_output=True, text=True, check=True,
    ).stdout)
    for mount in mounts:
        if mount.get("Destination") == "/data":
            return "/data", mount["Source"]
    raise RuntimeError("could not find the /data mount of the Immich server container")


def default_output() -> Path:
    from .config import state_dir
    return state_dir() / "face-quality.json"


def run(out: Path, *, workers: int = 8, progress=print) -> dict:
    inside, outside = library_root_from_docker()
    progress("reading face boxes from the database")
    faces = load_faces_from_docker()
    progress(f"measuring {len(faces)} faces")
    scores = score_faces(
        faces, lambda p: outside + p[len(inside):] if p.startswith(inside) else p,
        workers=workers, progress=progress,
    )
    people = summarise(faces, scores)
    result = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "method": f"variance of Laplacian on {CROP}x{CROP} grey face crop from the preview",
        "faces": len(scores),
        "people": people,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result), "utf-8")
    tmp.replace(out)
    progress(f"wrote {out}: {len(people)} people, {len(scores)} faces")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    run(args.out or default_output(), workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
