"""AI Tagger: tags every photo and video, and writes the result into the asset's Immich description.

Two image taggers (WD EVA02 and PixAI v1.0, both Danbooru-style: illustration / anime, people, clothing, characters,
series) say what is in the picture; a small text-only language model (the "VLM" container, which sees no pictures)
turns the final tags into a short description following the owner's instructions and may add or drop a few tags.
The result goes into a managed ``[AI Tagger]`` block inside the Immich description; the owner's own text is never
changed. Contract: ``docs/AI-TAGGER.md`` (its v2 section is binding).

Pieces:
* settings (``settings.json``) and the SQLite ``Store`` (catalogue, raw tagger scores, results, history, queue);
* pure functions: ``aggregate``/``detect``/``finalize`` (scores -> tags), ``apply_rules``, ``compose_block`` and
  ``merge_description`` (the block, with the owner's text kept exactly);
* ``Tagger`` and ``VLM``: HTTP clients of the two model containers (the VLM is sent text only); ``Services``: starts /
  stops those containers;
* ``Pipeline``: everything done to one asset (captures, tagging, VLM, write-back, read-back, history);
* ``Indexer``: a background thread that works through the queue and the untagged assets, like Search+.
"""

from __future__ import annotations

import base64
import collections
import json
import math
import os
import re
import sqlite3
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from pathlib import Path

from . import searchplus
from .client import AuthError, ImmichError, NotFoundError
from .config import state_dir
from .searchplus import GpuBusy, ServiceDown, fetch_catalog   # noqa: F401 - GpuBusy is re-exported

TAGGER_URL = os.environ.get("AITAGGER_URL", "http://127.0.0.1:11440")
VLM_URL = os.environ.get("AITAGGER_VLM_URL", "http://127.0.0.1:11441")
TAGGER_CONTAINER = os.environ.get("AITAGGER_CONTAINER", "immich_aitagger")
VLM_CONTAINER = searchplus.AITAGGER_VLM_CONTAINER
COMPOSE = Path(__file__).resolve().parent.parent / "deploy" / "aitagger" / "docker-compose.yml"
PROJECT = "immich-aitagger"
TAGGER_SERVICE, VLM_SERVICE = "tagger", "vlm"      # service names inside the compose file
VLM_MODEL = "tagger-vlm"
MODEL_LABELS = {"wd": "wd-eva02-large-tagger-v3", "pixai": "pixai-tagger-v1.0", "vlm": "Qwen3.5-2B (text)"}

# ---- graphics memory. PROVISIONAL: these come from measurements that are still being made; change them here only.
VRAM_GB_DEFAULT = 5           # setting `vram_gb`, its default: the memory cap of the two taggers (AITAGGER_VRAM_GB)
VRAM_GB_LIMITS = (3, 8)       # what `vram_gb` may be set to
VLM_UTIL = 0.22               # the describer's share of the whole card (AITAGGER_VLM_UTIL); measured 5.1 GB on 24 GB
# ----

DEFAULT_GPU_GB = 24           # when nvidia-smi can't say
IDLE_EXIT_MINUTES = 20        # the VLM container is stopped after this long without work
STATE_TTL = 5.0               # seconds a container's state is remembered (the status route is polled)
FLOOR = 0.05                  # the tagger returns calibrated scores from this up
DISPLAY_FLOOR = 0.2           # the Test card lists scores from this up (the kept ones always)
CAPTURE_SIDE = 1024           # captures are at most this big
PREVIEW_SIDE = 256            # pictures in the Test card
SEGMENTS = 8                  # a video is cut into this many equal parts; the first and last are skipped
MAX_IMAGES_PER_REQUEST = 64   # what the tagger accepts in one /tag call
MAX_RULE_PASSES = 5
VLM_ADD_SCORE = 0.7           # a tag only the VLM saw ranks below the taggers' sure ones (rules still score 1.0)
VLM_PROTECT = 0.9             # the VLM can't remove a tag a tagger is at least this sure of
MAX_ATTEMPTS = searchplus.MAX_ATTEMPTS
CLEARED = searchplus.CLEARED
RETRY_AFTER = searchplus.RETRY_AFTER
PREVIEW_WAIT = 90             # seconds Test / Write this wait for the models before saying "try again"
HISTORY_KEEP = 20             # old descriptions kept per asset
EPS = 1e-9

OPEN, CLOSE = "[AI Tagger]", "[/AI Tagger]"
RATING_PREFIX = "rating: "
NATIVE_PREFIX = "AI/"
MODES = ("retag", "describe", "full")
RANK = {"retag": 1, "describe": 2, "full": 3}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() - seconds, timezone.utc).isoformat(timespec="seconds")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class NotFound(LookupError):
    """Unknown asset or action; the message is shown to the user (the panel answers 404)."""


class GpuOOM(Exception):
    """The tagger ran out of graphics memory for this request: send fewer pictures at once."""


class ImmichDown(ServiceDown):
    """Immich does not answer (restarting, network). Not the asset's fault."""


class AssetGone(ValueError):
    """Immich does not know the asset any more."""


class WriteMismatch(RuntimeError):
    """Immich kept something else than what was written."""


def home() -> Path:
    path = state_dir() / "aitagger"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------- tag names

def norm_tag(text) -> str:
    """Canonical tag: lowercase, ``_`` as a space (so ``name_(qualifier)`` is ``name (qualifier)``), one line."""
    s = str(text).lower().replace("_", " ").replace(",", " ").replace("/", " ")
    s = s.replace("[", "(").replace("]", ")")
    s = " ".join(s.split()).rstrip(".").strip()
    return s[:60]


class Vocabulary:
    """``old -> new`` lines rename a tag; any other line is a preferred term for the VLM."""

    def __init__(self, text: str = ""):
        self.renames: dict[str, str] = {}
        self.terms: list[str] = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            arrow = "->" if "->" in line else ("→" if "→" in line else "")
            if not arrow:
                self.terms.append(line)
                continue
            old, new = (norm_tag(part) for part in line.split(arrow, 1))
            if old and new and old != new:
                self.renames[old] = new
                if new not in self.terms:
                    self.terms.append(new)

    def rename(self, tag: str) -> str:
        return self.renames.get(tag, tag)


# ---------------------------------------------------------------- settings

DEFAULTS = {
    "indexing": False, "keep_updated": True, "video_frames": 6, "batch_size": 8, "vlm_parallel": 16,
    "vram_gb": VRAM_GB_DEFAULT,
    "describe": True, "use_wd": True, "use_pixai": True, "wd_strictness": 0.5, "pixai_strictness": 0.5,
    "character_tags": True, "rating_tag": True, "max_tags": 30, "instructions": "", "vocabulary": "",
    "blocked": [], "rules": [], "write_tags": False, "language": "English",
}
LIMITS = {"video_frames": (1, 8), "batch_size": (1, 64), "vlm_parallel": (1, 32), "vram_gb": VRAM_GB_LIMITS,
          "wd_strictness": (0.05, 0.95), "pixai_strictness": (0.05, 0.95), "max_tags": (5, 100)}
TEXT_LIMITS = {"instructions": 4000, "vocabulary": 4000, "language": 40}
MAX_BLOCKED, MAX_RULES, MAX_RULE_TAGS = 500, 100, 50
RULE_KEYS = ("if_all", "if_any", "unless", "add", "remove")

# settings that change what an asset's result looks like (a change makes older results "outdated")
CONTENT = ("video_frames", "describe", "use_wd", "use_pixai", "wd_strictness", "pixai_strictness", "character_tags",
           "rating_tag", "max_tags", "instructions", "vocabulary", "blocked", "rules", "write_tags", "language")
# the cheapest way to bring results up to date after a change (the strongest of the changed keys wins)
REPROCESS = {
    "full": ("video_frames", "use_wd", "use_pixai"),
    "describe": ("describe", "instructions", "vocabulary", "language"),
    "retag": ("wd_strictness", "pixai_strictness", "character_tags", "rating_tag", "max_tags", "blocked", "rules",
              "write_tags"),
}
_SETTINGS_LOCK = threading.Lock()


def settings_path() -> Path:
    return home() / "settings.json"


def _tag_list(value, name: str, limit: int) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise ValueError(f"{name} must be a list of tags")
    out: list[str] = []
    for tag in value:
        tag = norm_tag(tag)
        if tag and tag not in out:
            out.append(tag)
    if len(out) > limit:
        raise ValueError(f"{name}: at most {limit} entries")
    return out


def clean_rule(rule, index: int) -> dict:
    where = f"Rule {index + 1}"
    if not isinstance(rule, dict):
        raise ValueError(f"{where} must be an object")
    extra = sorted(set(rule) - set(RULE_KEYS))
    if extra:
        raise ValueError(f"{where}: unknown field {extra[0]!r}")
    out = {key: _tag_list(rule.get(key, []), f"{where}: {key}", MAX_RULE_TAGS) for key in RULE_KEYS}
    if not (out["if_all"] or out["if_any"]):
        raise ValueError(f"{where}: give at least one tag under 'if all' or 'if any'")
    if not (out["add"] or out["remove"]):
        raise ValueError(f"{where}: give at least one tag to add or remove")
    if set(out["add"]) & set(out["remove"]):
        raise ValueError(f"{where}: a tag can't be both added and removed")
    return out


def validate_setting(key: str, value):
    """The canonical value of a setting, or ValueError. Types are checked explicitly (``"false"`` is not False)."""
    if key not in DEFAULTS:
        raise ValueError(f"Unknown AI Tagger setting: {key}")
    default = DEFAULTS[key]
    if key == "blocked":
        return _tag_list(value, "blocked", MAX_BLOCKED)
    if key == "rules":
        if not isinstance(value, list):
            raise ValueError("rules must be a list")
        if len(value) > MAX_RULES:
            raise ValueError(f"at most {MAX_RULES} rules")
        return [clean_rule(rule, i) for i, rule in enumerate(value)]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        if key == "language":
            value = value.strip()
            if not value:
                raise ValueError("language can't be empty")
        if len(value) > TEXT_LIMITS[key]:
            raise ValueError(f"{key} must be at most {TEXT_LIMITS[key]} characters")
        return value
    lo, hi = LIMITS[key]
    if isinstance(default, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be a whole number")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a number")
        value = float(value)
    if not lo <= value <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return value


def load_settings() -> dict:
    try:
        data = json.loads(settings_path().read_text("utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {key: json.loads(json.dumps(default)) for key, default in DEFAULTS.items()}
    for key, value in data.items():
        if key in DEFAULTS:
            try:
                out[key] = validate_setting(key, value)
            except ValueError:
                pass                    # a hand-edited bad value: keep the default
    return out


def apply_settings(changes: dict, store: "Store | None" = None) -> tuple[dict, list[str]]:
    """Validate and save ``changes``; returns (all settings, the content keys that really changed).

    ``settings_version`` goes up by one when a content setting changed. The file is written first and the version
    bumped after, and readers read the version first: the worst a race can do is call a fresh result "outdated".
    """
    if not isinstance(changes, dict):
        raise ValueError("changes must be an object")
    clean = {key: validate_setting(key, value) for key, value in changes.items()}      # all or nothing
    with _SETTINGS_LOCK:
        settings = load_settings()
        changed = [key for key, value in clean.items() if key in CONTENT and settings[key] != value]
        settings.update(clean)
        tmp = settings_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=1, ensure_ascii=False), "utf-8")
        tmp.replace(settings_path())
        if store is not None:
            if changed:
                store.bump_settings_version()
            if settings["write_tags"]:
                store.set_meta("native_tags", "1")      # from now on the AI/ tags are kept in step
    return settings, changed


def save_settings(changes: dict, store: "Store | None" = None) -> dict:
    return apply_settings(changes, store)[0]


def suggest_mode(changed: list[str]) -> str:
    """The cheapest reprocess mode that brings old results in line with the changed settings."""
    for mode in ("full", "describe", "retag"):
        if any(key in REPROCESS[mode] for key in changed):
            return mode
    return "none"


# ---------------------------------------------------------------- tags: scores -> tags -> rules

def _combine(values: list[float]) -> float:
    """One score for a tag over all captures of an asset: half the median, half the best."""
    return 0.5 * statistics.median(values) + 0.5 * max(values)


def _per_tag(series: list[dict]) -> dict[str, float]:
    names: set = set()
    for cap in series:
        names.update(cap)
    return {tag: _combine([float(cap.get(tag, 0.0)) for cap in series]) for tag in names}


def _mean_scores(series: list[dict]) -> dict[str, float]:
    """Per name, the mean over the captures (a capture that does not list a name counts as 0)."""
    names: set = set()
    for cap in series:
        names.update(cap)
    return {name: sum(float(cap.get(name, 0.0)) for cap in series) / len(series) for name in names}


def _merged(data: dict, categories: tuple[str, ...]) -> dict[str, float]:
    """One capture's scores for these categories as one dict (a tag in two categories keeps the higher score)."""
    out: dict[str, float] = {}
    for category in categories:
        for tag, score in (data.get(category) or {}).items():
            out[tag] = max(out.get(tag, 0.0), score)
    return out


def has_pixai(raw: dict | None) -> bool:
    """Whether stored tagger scores include PixAI's. Scores made by the v1 service (RAM++) do not."""
    return any((cap or {}).get("pixai") is not None for cap in (raw or {}).get("scores") or [])


def needs_vlm(settings: dict) -> bool:
    """The describer only has tags to go on, so it is used only when a tagger is on."""
    return bool(settings["describe"] and (settings["use_wd"] or settings["use_pixai"]))


def detect(raw: dict, settings: dict) -> dict:
    """Steps 3-4: stored tagger scores -> tags with scores (before the VLM and the rules)."""
    vocab = Vocabulary(settings["vocabulary"])
    blocked = set(settings["blocked"])
    candidates: list[tuple[str, float, str]] = []
    display: dict = {"wd": [], "pixai": [], "rating": {}}

    def show(model: str, scores: dict, strictness: float) -> None:
        shown = {}
        for tag, score in scores.items():
            name = norm_tag(tag)
            if not name:
                continue
            name = vocab.rename(name)
            kept = score >= strictness - EPS
            if kept:
                candidates.append((tag, score, model))
            if kept or score >= DISPLAY_FLOOR:
                if name not in shown or score > shown[name]["score"]:
                    shown[name] = {"tag": name, "score": round(score, 3), "kept": kept}
        display[model] = sorted(shown.values(), key=lambda t: (-t["score"], t["tag"]))[:80]

    caps = raw.get("scores") or []
    # `character_tags` gates the names of characters (WD and PixAI) and PixAI's series ("copyright") tags
    wanted = {"wd": ("general", "character") if settings["character_tags"] else ("general",),
              "pixai": ("general", "character", "copyright") if settings["character_tags"] else ("general",)}
    for model in ("wd", "pixai"):
        if not settings["use_" + model]:
            continue
        series = [_merged(cap[model], wanted[model]) for cap in caps if cap and cap.get(model) is not None]
        if series:
            show(model, _per_tag(series), settings[model + "_strictness"])
    # The rating: each model's probabilities averaged over the captures, then the mean of the models that are on
    # (one model alone if only one is), then the best. The tag's source is the model surest of the winner.
    ratings = [r for r in raw.get("ratings") or [] if isinstance(r, dict)]
    parts: dict[str, dict[str, float]] = {}
    for model in ("wd", "pixai"):
        if settings["use_" + model]:
            per_capture = [r[model] for r in ratings if isinstance(r.get(model), dict) and r[model]]
            if per_capture:
                parts[model] = _mean_scores(per_capture)
    if parts:
        names = set().union(*parts.values())
        mean = {name: sum(p.get(name, 0.0) for p in parts.values()) / len(parts) for name in names}
        display["rating"] = {name: round(p, 3) for name, p in mean.items()}
        if settings["rating_tag"]:
            best = max(sorted(mean), key=lambda name: mean[name])
            source = max(parts, key=lambda model: parts[model].get(best, 0.0))       # a tie goes to WD
            candidates.append((RATING_PREFIX + best, mean[best], source))
    tags: dict[str, tuple[float, str]] = {}
    for tag, score, source in candidates:
        original = norm_tag(tag)
        if not original:
            continue
        name = vocab.rename(original)
        if original in blocked or name in blocked:
            continue
        if name not in tags or score > tags[name][0]:
            tags[name] = (score, source)
    return {"tags": tags, "display": display, "terms": vocab.terms}


def rule_matches(have: set, rule: dict) -> bool:
    return (all(t in have for t in rule["if_all"])
            and (not rule["if_any"] or any(t in have for t in rule["if_any"]))
            and not any(t in have for t in rule["unless"]))


def apply_rules(tags: dict, rules: list[dict]) -> tuple[dict, list[dict]]:
    """Run the rules in order, again and again until nothing changes (at most ``MAX_RULE_PASSES`` passes)."""
    tags = dict(tags)
    fired: dict[int, dict] = {}
    for _ in range(MAX_RULE_PASSES):
        changed = False
        for index, rule in enumerate(rules):
            if not rule_matches(set(tags), rule):
                continue
            note = fired.setdefault(index, {"rule": index, "added": [], "removed": []})
            for tag in rule["add"]:
                if tag not in tags:
                    tags[tag] = (1.0, "rule")
                    changed = True
                    if tag not in note["added"]:
                        note["added"].append(tag)
            for tag in rule["remove"]:
                if tag in tags:
                    del tags[tag]
                    changed = True
                    if tag not in note["removed"]:
                        note["removed"].append(tag)
        if not changed:
            break
    return tags, [fired[i] for i in sorted(fired) if fired[i]["added"] or fired[i]["removed"]]


def finalize(tags: dict, vlm: dict | None, settings: dict) -> tuple[list[dict], list[dict]]:
    """Steps 5-6: what the VLM added / removed, the rules, ``blocked`` and the ``max_tags`` cap."""
    vocab = Vocabulary(settings["vocabulary"])
    blocked = set(settings["blocked"])
    tags = dict(tags)
    if settings["describe"] and vlm:
        # Measured on the library: the VLM sometimes drops tags the taggers are certain of, and sometimes lists
        # the same tag under both add and remove. So a tag named both ways is ignored, a sure tagger tag (and the
        # rating) can't be removed by it, and a tag only it saw ranks below the taggers' confident ones.
        added = {vocab.rename(norm_tag(t)) for t in vlm.get("add_tags") or []} - {""}
        removed = {norm_tag(t) for t in vlm.get("remove_tags") or []} - {""}
        removed |= {vocab.rename(t) for t in removed}
        contested = added & removed
        for name in added - contested:
            if name not in blocked:
                old = tags.get(name)
                tags[name] = (max(old[0], VLM_ADD_SCORE), old[1]) if old else (VLM_ADD_SCORE, "vlm")
        for name in removed - contested:
            if name in tags and tags[name][0] < VLM_PROTECT and not name.startswith(RATING_PREFIX):
                del tags[name]
    tags = {t: v for t, v in tags.items() if t not in blocked}
    tags, trace = apply_rules(tags, settings["rules"])
    tags = {t: v for t, v in tags.items() if t not in blocked}
    rating = [t for t in tags if t.startswith(RATING_PREFIX)]
    others = sorted((t for t in tags if t not in rating), key=lambda t: (-tags[t][0], t))
    keep = others[:max(settings["max_tags"] - len(rating), 0)] + sorted(rating)
    return [{"tag": t, "score": round(tags[t][0], 3), "source": tags[t][1]} for t in keep], trace


def build(raw: dict, vlm: dict | None, settings: dict) -> dict:
    """Everything the pipeline decides for one asset from the stored scores and the VLM's answer."""
    found = detect(raw, settings)
    tags, trace = finalize(found["tags"], vlm, settings)
    description = (vlm or {}).get("description", "") if settings["describe"] else ""
    return {"tags": tags, "models": found["display"], "rules": trace, "description": description,
            "block": compose_block([t["tag"] for t in tags], description)}


# ---------------------------------------------------------------- the block in the description

def compose_block(tags: list[str], description: str) -> str:
    """The managed text; "" when there is nothing to say."""
    def clean(text: str) -> str:
        return " ".join(str(text).replace(OPEN, "").replace(CLOSE, "").split())

    lines = []
    if tags:
        lines.append("Tags: " + ", ".join(clean(t) for t in tags))
    description = clean(description)
    if description:
        lines.append("Description: " + description)
    return "\n".join([OPEN] + lines + [CLOSE]) if lines else ""


def block_spans(text: str) -> list[tuple[int, int]]:
    """Where the ``[AI Tagger]`` ... ``[/AI Tagger]`` blocks are. A stray marker is the owner's text."""
    spans, pos = [], 0
    while True:
        end = text.find(CLOSE, pos)
        if end < 0:
            return spans
        start = text.rfind(OPEN, pos, end)
        pos = end + len(CLOSE)
        if start >= 0:
            spans.append((start, pos))


def _cut(text: str, start: int, end: int) -> str:
    """Remove a block; with it goes the blank line the panel put in front of it, so the owner's text is as before."""
    before, after = text[:start], text[end:]
    if not after.strip():
        return before[:-2] if before.endswith("\n\n") else before
    return before + (after[1:] if after.startswith("\n") else after)


def merge_description(current: str, block: str) -> str:
    """``current`` with the managed block replaced (or added, or - when ``block`` is "" - removed).

    Everything outside the block is the owner's and stays character for character. A new block goes after the
    text, separated by one blank line; an existing block is replaced where it stands.
    """
    current = current or ""
    spans = block_spans(current)
    if not spans:
        if not block:
            return current
        return block if current == "" else current + "\n\n" + block
    out = current
    for start, end in reversed(spans[1:]):              # a second copy of the block is leftover: drop it
        out = _cut(out, start, end)
    start, end = spans[0]
    return out[:start] + block + out[end:] if block else _cut(out, start, end)


def strip_block(current: str) -> str:
    return merge_description(current, "")


def same_text(a: str, b: str) -> bool:
    norm = lambda s: (s or "").replace("\r\n", "\n").strip()  # noqa: E731 - Immich may trim the ends
    return norm(a) == norm(b)


# ---------------------------------------------------------------- captures

def segment_positions(n: int) -> list[float]:
    """Where in a video (fractions of its length) the captures are taken.

    The video is cut into 8 equal segments; the middles of segments 2-7 are the candidates (the first and the last
    segment are skipped), and ``n`` of them are picked evenly: 2 gives segments 3 and 6. More than 6 means all 6.
    """
    candidates = [(k - 0.5) / SEGMENTS for k in range(2, SEGMENTS)]
    n = max(1, min(int(n), len(candidates)))
    return [candidates[int((i + 0.5) * len(candidates) / n)] for i in range(n)]


def prepare_captures(item: dict, video_n: int) -> tuple[list[bytes], str]:
    """The pictures that stand for one asset: its preview, or frames cut from a video / animated image."""
    from .media import ANIMATED_EXT, animation_frames, shrink_image

    original = item.get("original") or ""
    positions = segment_positions(video_n)
    if item["type"] == "VIDEO" and original and os.path.exists(original):
        frames = searchplus.video_frames(original, item.get("duration_ms") or 0, len(positions), CAPTURE_SIDE,
                                         positions=positions)
        if frames:
            return frames, "video"
    if item["type"] == "IMAGE" and original.lower().endswith(ANIMATED_EXT) and os.path.exists(original):
        frames = animation_frames(original, len(positions), positions=positions)
        if len(frames) > 1:
            return [shrink_image(f, CAPTURE_SIDE) for f in frames], "animation"
    preview = item.get("preview")
    if preview and os.path.exists(preview):
        with open(preview, "rb") as fh:
            return [shrink_image(fh.read(), CAPTURE_SIDE)], "video" if item["type"] == "VIDEO" else "image"
    if not original or not os.path.exists(original):
        raise FileNotFoundError("the file is gone and Immich has no preview of it")
    if item["type"] == "IMAGE":
        data, problem = searchplus._picture(original, CAPTURE_SIDE)       # noqa: SLF001
        if data:
            return [data], "image"
        if problem == "truncated":
            raise ValueError("damaged picture: the file is cut short (an incomplete copy), and Immich has no preview")
        raise ValueError("not a picture: Immich has no preview and the file can't be read as an image "
                         f"({searchplus._what_is(original)})")                  # noqa: SLF001
    raise ValueError("not a playable video: ffmpeg can't open it and Immich has no preview "
                     f"({searchplus._what_is(original)})")                      # noqa: SLF001


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


# ---------------------------------------------------------------- the two model servers

def _is_oom(text: str) -> bool:
    return "out of memory" in (text or "").lower()


class Tagger:
    """HTTP client of the tagger container (WD + PixAI)."""

    def __init__(self, url: str = TAGGER_URL, timeout: float = 600):
        self.url, self.timeout = url.rstrip("/"), timeout

    def health(self, timeout: float = 3) -> dict | None:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=timeout) as resp:
                return json.loads(resp.read())
        except (OSError, ValueError):
            return None

    def tag(self, images: list[bytes], floor: float = FLOOR) -> tuple[list, list]:
        """(one result per picture or None, one error per picture or None). Raises GpuOOM / ServiceDown."""
        if len(images) > MAX_IMAGES_PER_REQUEST:
            raise ValueError(f"at most {MAX_IMAGES_PER_REQUEST} pictures per request")
        body = {"images": [base64.b64encode(b).decode() for b in images], "floor": floor}
        req = urllib.request.Request(self.url + "/tag", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if _is_oom(detail):
                raise GpuOOM(detail) from exc
            if exc.code >= 500:                 # 503 = still loading; other 5xx = the server fell over
                raise ServiceDown(detail or f"HTTP {exc.code}") from exc
            raise RuntimeError(f"AI Tagger model server error {exc.code}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            raise ServiceDown(str(exc)) from exc
        except ValueError as exc:
            raise ServiceDown(f"the tagger sent something unreadable: {exc}") from exc
        results = data.get("results") or []
        errors = data.get("errors") or [None] * len(results)
        if len(results) != len(images):
            raise RuntimeError("the tagger answered for a different number of pictures")
        if any(_is_oom(e) for e in errors if e):
            raise GpuOOM(next(e for e in errors if e and _is_oom(e)))
        return results, errors


VLM_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["description", "add_tags", "remove_tags"],
              "properties": {"description": {"type": "string"},
                             "add_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                             "remove_tags": {"type": "array", "items": {"type": "string"}, "maxItems": 12}}}
VLM_SYSTEM = (
    "You are the cataloguer of a private, personal photo archive that only its owner can see. You never see the "
    "pictures: you are given the tags that image taggers found in one picture, each with a confidence, and you turn "
    "them into a short description. Treat every tag neutrally and factually, including nudity, sexual or violent "
    "content, in plain language, without judging, warning or moralising. You never refuse: if the tags say little, "
    "say little. Never invent details the tags do not support. Answer only with the JSON object that is asked for.")
_REFUSAL = re.compile(r"^\s*(i['’]?m sorry|i am sorry|sorry[,.]|i can(?:'|’)?t|i cannot|i['’]?m (?:not able|unable)|"
                      r"i am (?:not able|unable)|i won['’]?t|as an ai|unable to (?:describe|assist|help))", re.I)


class VLMRejected(Exception):
    """The language model refused, or did not answer in the asked-for format."""


def vlm_prompt(tags: list[dict], rating: dict, settings: dict, terms: list[str], kind: str = "IMAGE") -> str:
    """What the text-only describer is told: the final tags with their scores, the rating, and the owner's
    instructions, preferred terms and language. There is no picture."""
    what = "video" if kind == "VIDEO" else "picture"
    shown = ", ".join(f"{t['tag']} {t['score']:.2f}" for t in tags[:60]) or "(none)"
    lines = [f"Tags found by two image taggers in one {what}"
             + (" (several frames of it, combined)" if kind == "VIDEO" else "")
             + f", with a 0-1 confidence: {shown}"]
    if rating:
        lines.append("Content rating estimate: " + ", ".join(f"{k} {v:.2f}" for k, v in sorted(rating.items())))
    if settings["instructions"].strip():
        lines.append("Instructions from the archive's owner:\n" + settings["instructions"].strip())
    if terms:
        lines.append("Preferred terms (use these words when they fit): " + "; ".join(terms[:80]))
    lines.append(
        f"Write the description in {settings['language']}: 1-2 sentences saying what the {what} shows, as far as the "
        "tags imply it, following the instructions. Use only what the tags say: do not add a place, setting, "
        "lighting, time of day, weather, mood or story unless a tag names it. "
        "add_tags and remove_tags may hold at most 8 entries each and normally stay empty: add a tag only when the "
        "owner's instructions ask for it, and remove one only when it directly contradicts other tags. "
        "add_tags are short lowercase English tags; remove_tags are tags from the list above, spelled exactly the same. "
        'Answer with a JSON object {"description": "...", "add_tags": [...], "remove_tags": [...]}.')
    return "\n".join(lines)


def parse_vlm_answer(content) -> dict:
    """The VLM's JSON answer as {"description", "add_tags", "remove_tags"}, or VLMRejected."""
    if not isinstance(content, str) or not content.strip():
        raise VLMRejected("empty answer")
    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise VLMRejected(f"not valid JSON ({exc})") from None
    if not isinstance(data, dict) or not isinstance(data.get("description"), str):
        raise VLMRejected("the JSON has no description")
    out = {"description": " ".join(data["description"].split())[:1500]}
    for key in ("add_tags", "remove_tags"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
            raise VLMRejected(f"{key} is not a list of text")
        out[key] = value[:60]
    if out["description"] and _REFUSAL.match(out["description"]):
        raise VLMRejected("the model refused")
    return out


class VLM:
    """HTTP client of the language model container (vLLM, OpenAI-compatible). It is sent text only, never a picture."""

    def __init__(self, url: str = VLM_URL, model: str = VLM_MODEL, timeout: float = 300):
        self.url, self.model, self.timeout = url.rstrip("/"), model, timeout

    def health(self, timeout: float = 3) -> bool:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=timeout) as resp:
                return resp.status == 200
        except OSError:
            return False

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self.url + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code >= 500 or exc.code == 429:
                raise ServiceDown(f"the language model answered {exc.code}: {detail}") from exc
            raise VLMRejected(f"the language model answered {exc.code}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            raise ServiceDown(str(exc)) from exc
        except ValueError as exc:
            raise ServiceDown(f"the language model sent something unreadable: {exc}") from exc

    def ask(self, prompt: str) -> str:
        """One round trip; the answer's text. Raises ServiceDown / VLMRejected."""
        data = self._post({
            "model": self.model, "temperature": 0.2, "max_tokens": 400, "presence_penalty": 1.0,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "tagger_answer", "strict": True, "schema": VLM_SCHEMA}},
            "messages": [{"role": "system", "content": VLM_SYSTEM}, {"role": "user", "content": prompt}]})
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            raise VLMRejected("no answer in the response") from None
        if message.get("refusal"):
            raise VLMRejected("the model refused")
        if choice.get("finish_reason") == "length":
            raise VLMRejected("the answer was cut short")
        return message.get("content")

    def describe(self, tags: list[dict], rating: dict, settings: dict, kind: str = "IMAGE",
                 terms: list[str] | None = None) -> dict:
        """{"description", "add_tags", "remove_tags", "note"}. A refusal or a bad answer is tried once more, then
        stored as an empty description with a note - that is not the asset's failure."""
        prompt = vlm_prompt(tags, rating, settings, terms or [], kind)
        problem = ""
        for _ in range(2):
            try:
                answer = parse_vlm_answer(self.ask(prompt))
            except VLMRejected as exc:
                problem = str(exc)
                continue
            return {**answer, "note": ""}
        return {"description": "", "add_tags": [], "remove_tags": [], "note": f"no description: {problem}"}


# ---------------------------------------------------------------- the model containers

class _Done:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run_command(cmd: list[str], env: dict | None = None, timeout: float = 60):
    """Run a command (docker, nvidia-smi); never raises. Tests replace this runner, so docker is never touched."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **env} if env else None)
    except (OSError, subprocess.SubprocessError) as exc:
        return _Done(127, "", str(exc))


def _nvidia_smi() -> str:
    """nvidia-smi's path. Under WSL it lives in /usr/lib/wsl/lib, which systemd services don't have on PATH."""
    import shutil
    return shutil.which("nvidia-smi") or next(
        (p for p in ("/usr/lib/wsl/lib/nvidia-smi",) if os.path.exists(p)), "nvidia-smi")


class ComposeService:
    """One container of the docker compose project, started and stopped through ``runner``."""

    def __init__(self, container: str, service: str, runner=run_command, *, project: str = PROJECT,
                 compose: Path = COMPOSE, clock=time.monotonic, ttl: float = STATE_TTL):
        self.container, self.service, self.runner = container, service, runner
        self.project, self.compose, self.clock, self.ttl = project, compose, clock, ttl
        self._state: str | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._state = None

    def container_state(self, fresh: bool = False) -> str:
        """running / stopped / missing / unknown; remembered for a few seconds unless ``fresh``."""
        with self._lock:
            now = self.clock()
            if not fresh and self._state is not None and now - self._at < self.ttl:
                return self._state
        out = self.runner(["docker", "inspect", "-f", "{{.State.Running}}", self.container], timeout=15)
        if out.returncode == 127:
            state = "unknown"
        elif out.returncode != 0:
            state = "missing" if (not out.stderr.strip() or "no such" in out.stderr.lower()) else "unknown"
        else:
            state = "running" if out.stdout.strip() == "true" else "stopped"
        with self._lock:
            self._state, self._at = state, self.clock()
        return state

    def up(self, env: dict, recreate: bool = False) -> None:
        cmd = ["docker", "compose", "-p", self.project, "-f", str(self.compose), "up", "-d"]
        if recreate:
            cmd.append("--force-recreate")
        out = self.runner(cmd + [self.service], env=env, timeout=1800)
        self.invalidate()
        if out.returncode != 0:
            raise RuntimeError(f"could not start {self.container}: {(out.stderr or out.stdout).strip()[-300:]}")

    def stop(self) -> bool:
        if self.container_state(fresh=True) != "running":
            return False
        self.runner(["docker", "stop", "-t", "20", self.container], timeout=90)
        self.invalidate()
        return True


class Services:
    """The two model containers and the HTTP clients that talk to them (what the Indexer and the routes use)."""

    def __init__(self, tagger: Tagger | None = None, vlm: VLM | None = None, *, runner=run_command,
                 store: "Store | None" = None, settings_fn=None, clock=time.monotonic, sleep=time.sleep,
                 search_stop=None):
        self.tagger, self.vlm = tagger or Tagger(), vlm or VLM()
        self.runner, self.store, self.clock, self.sleep = runner, store, clock, sleep
        self.settings_fn = settings_fn or load_settings
        self.tagger_box = ComposeService(TAGGER_CONTAINER, TAGGER_SERVICE, runner, clock=clock)
        self.vlm_box = ComposeService(VLM_CONTAINER, VLM_SERVICE, runner, clock=clock)
        self.search_box = ComposeService(searchplus.CONTAINER, "", runner, clock=clock)
        self.search_stop = search_stop or (lambda: searchplus.Service().stop())
        self.last_used = clock()
        self._lock = threading.RLock()
        self._memory: dict[str, str] = {}
        self._gpu: tuple[float, dict] | None = None
        self._total_gb: float | None = None

    # ---- the clients (every call counts as "in use")
    def touch(self) -> None:
        self.last_used = self.clock()

    def tag(self, images: list[bytes]) -> tuple[list, list]:
        self.touch()
        try:
            return self.tagger.tag(images)
        finally:
            self.touch()

    def describe(self, tags: list[dict], rating: dict, settings: dict, kind: str = "IMAGE",
                 terms: list[str] | None = None) -> dict:
        self.touch()
        try:
            return self.vlm.describe(tags, rating, settings, kind, terms)
        finally:
            self.touch()

    # ---- the graphics card
    def gpu(self) -> dict:
        """{"totalGb", "usedGb"} from nvidia-smi (remembered for a few seconds)."""
        now = self.clock()
        with self._lock:
            if self._gpu and now - self._gpu[0] < STATE_TTL:
                return self._gpu[1]
        out = self.runner([_nvidia_smi(), "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                          timeout=10)
        total = used = None
        if out.returncode == 0 and out.stdout.strip():
            try:
                first = out.stdout.strip().splitlines()[0].split(",")
                total, used = float(first[0]) / 1024, float(first[1]) / 1024
            except (ValueError, IndexError):
                total = used = None
        if total:
            self._total_gb = total
        info = {"totalGb": round(total or self._total_gb or DEFAULT_GPU_GB, 1),
                "usedGb": round(used, 1) if used is not None else None}
        with self._lock:
            self._gpu = (now, info)
        return info

    def env(self, settings: dict | None = None) -> dict:
        """What the compose file needs: ``vram_gb`` is the taggers' cap, the describer's share is the constant
        ``VLM_UTIL`` (not derived from ``vram_gb``), and ``vlm_parallel`` is its number of concurrent requests."""
        settings = settings or self.settings_fn()
        return {"AITAGGER_VRAM_GB": str(int(settings["vram_gb"])), "AITAGGER_VLM_UTIL": str(VLM_UTIL),
                "AITAGGER_VLM_SEQS": str(int(settings["vlm_parallel"]))}

    # ---- starting and stopping
    def _remembered(self) -> dict:
        raw = self.store.meta("services_env") if self.store else self._memory.get("env", "")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def _remember(self, data: dict) -> None:
        if self.store:
            self.store.set_meta("services_env", _dumps(data))
        else:
            self._memory["env"] = _dumps(data)

    def load(self, need_tagger: bool = True, need_vlm: bool = True) -> list[str]:
        """Start the containers that are not running (Search+ is stopped first, while ``searchplus.AITAGGER_EXCLUSIVE``
        says they may not share the card).
        Returns the names it started. A container whose derived env changed since its last start is recreated."""
        with self._lock:
            self.touch()
            wanted = [(box, keys) for box, keys, needed in (
                (self.tagger_box, ("AITAGGER_VRAM_GB",), need_tagger),
                (self.vlm_box, ("AITAGGER_VLM_UTIL", "AITAGGER_VLM_SEQS"), need_vlm)) if needed]
            todo = [(box, keys, box.container_state(fresh=True)) for box, keys in wanted]
            todo = [t for t in todo if t[2] != "running"]
            if not todo:
                return []
            if searchplus.AITAGGER_EXCLUSIVE and self.search_box.container_state(fresh=True) == "running":
                try:
                    self.search_stop()
                except Exception:  # noqa: BLE001 - not stopping Search+ must not hide the real problem
                    pass
                self.search_box.invalidate()
            env = self.env()
            remembered = self._remembered()
            started = []
            for box, keys, state in todo:
                wanted_env = {k: env[k] for k in keys}
                recreate = state != "missing" and remembered.get(box.container) != wanted_env
                try:
                    box.up(env, recreate=recreate)
                except RuntimeError as exc:
                    raise ServiceDown(str(exc)) from exc
                remembered[box.container] = wanted_env
                self._remember(remembered)
                started.append(box.container)
            return started

    def _ready(self, need_tagger: bool, need_vlm: bool) -> bool:
        if need_tagger:
            health = self.tagger.health()
            if not (health and health.get("status") == "ok"):
                return False
        return not need_vlm or bool(self.vlm.health())

    def ensure_ready(self, need_tagger: bool = True, need_vlm: bool = True, wait: float = 3600, progress=None,
                     stop: threading.Event | None = None) -> None:
        """Start what is needed and wait until it answers. ServiceDown when it is still loading after ``wait``."""
        self.touch()
        if self._ready(need_tagger, need_vlm):
            return
        self.load(need_tagger, need_vlm)
        started = self.clock()
        deadline = started + wait
        while True:
            if stop is not None and stop.is_set():
                raise ServiceDown("stopped")
            health = self.tagger.health() if need_tagger else None
            if health and health.get("status") == "error":
                raise RuntimeError(f"the AI Tagger model failed to load: {health.get('error')}")
            if self._ready(need_tagger, need_vlm):
                self.touch()
                return
            self.touch()                        # waiting for the models is using them
            if progress:
                waiting = [name for name, needed, ok in (
                    ("tagger", need_tagger, bool(health and health.get("status") == "ok")),
                    ("language model", need_vlm, bool(need_vlm and self.vlm.health()))) if needed and not ok]
                progress("loading the " + " and the ".join(waiting) + " into the graphics card "
                         "(the very first time, the models are downloaded first)")
            if self.clock() >= deadline:
                raise ServiceDown("the AI Tagger models are still loading")
            if self.clock() - started > 30:
                for box, needed in ((self.tagger_box, need_tagger), (self.vlm_box, need_vlm)):
                    if needed and box.container_state(fresh=True) == "stopped":
                        raise RuntimeError(f"{box.container} stopped while loading; see `docker logs {box.container}`")
            self.sleep(2)

    def unload(self) -> list[str]:
        """Stop both containers (frees the card)."""
        with self._lock:
            return [box.container for box in (self.vlm_box, self.tagger_box) if box.stop()]

    def idle_check(self, now: float | None = None) -> bool:
        """Stop the VLM container when nothing has used the models for ``IDLE_EXIT_MINUTES`` (three times as long
        while it is still loading: the very first start downloads the model)."""
        now = self.clock() if now is None else now
        idle = now - self.last_used
        if idle < IDLE_EXIT_MINUTES * 60:
            return False
        with self._lock:
            if self.vlm_box.container_state(fresh=True) != "running":
                return False
            if idle < 3 * IDLE_EXIT_MINUTES * 60 and not self.vlm.health(timeout=1.5):
                return False
            return self.vlm_box.stop()

    def status(self) -> dict:
        tagger_state, vlm_state = self.tagger_box.container_state(), self.vlm_box.container_state()
        th = self.tagger.health(timeout=1.5) if tagger_state == "running" else None
        vh = self.vlm.health(timeout=1.5) if vlm_state == "running" else False
        tagger = {"container": tagger_state, "status": "down", "error": None}
        if th:
            tagger.update(status=th.get("status") or "loading", error=th.get("error"))
        elif tagger_state == "running":
            tagger["status"] = "loading"
        vlm = {"container": vlm_state, "status": "ok" if vh else ("loading" if vlm_state == "running" else "down"),
               "error": None}
        return {"tagger": tagger, "vlm": vlm, "gpu": self.gpu(),
                "searchplusRunning": self.search_box.container_state() == "running"}


# ---------------------------------------------------------------- the store

SCHEMA = """
create table if not exists assets (
  id text primary key, type text, taken text, name text, preview text, original text,
  duration_ms integer default 0, gone integer default 0
);
create index if not exists assets_taken on assets(taken desc, id);
create table if not exists raw (
  id text primary key, captures integer, scores_json text, rating_json text, tagged_at text, models text
);
create table if not exists results (
  id text primary key, tags_json text, vlm_json text, description text, block text, settings_version integer,
  processed_at text, written_at text, note text
);
create index if not exists results_version on results(settings_version);
create table if not exists history (id text, at text, old_description text, new_description text);
create index if not exists history_id on history(id);
create table if not exists failed (id text primary key, error text, attempts integer, at text);
create table if not exists queue (id text primary key, mode text, at text);
create table if not exists excluded (id text primary key, at text);
create table if not exists meta (key text primary key, value text);
create table if not exists asset_tags (id text, tag text, primary key (id, tag)) without rowid;
create index if not exists asset_tags_tag on asset_tags(tag);
"""
RAW_FORMAT = "2"        # meta.raw_format; 1 (no entry) = stored tagger scores with RAM++, 2 = with PixAI
_ITEM = "a.id, a.type, a.preview, a.original, a.duration_ms, a.taken, a.name"
_ITEM_KEYS = ("id", "type", "preview", "original", "duration_ms", "taken", "name")
_NOT_EXCLUDED = " and not exists (select 1 from excluded e where e.id=a.id)"
_NOT_FAILED = " and not exists (select 1 from failed f where f.id=a.id and (f.attempts >= ? or f.at > ?))"
_WRITTEN = "r.written_at is not null"


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Store:
    def __init__(self, folder: Path | None = None):
        self.folder = folder or home()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.folder / "tagger.sqlite"), timeout=30, check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.executescript(SCHEMA)
        self._meta = dict(self.conn.execute("select key, value from meta").fetchall())
        self._changes = 0
        self._tags_cache: tuple[int, list] | None = None
        self._migrate()

    def _migrate(self) -> None:
        """Stored scores made by the v1 service (RAM++, no PixAI entry) cannot give a v2 result. The first time a
        store holding such scores is opened, ``settings_version`` goes up once, so every result made from them counts
        as "outdated"; the Indexer redoes them as ``full`` whenever a reprocess (of any mode) reaches them."""
        if self.meta("raw_format") == RAW_FORMAT:
            return
        if self.conn.execute("select 1 from raw where models not like '%pixai%' limit 1").fetchone():
            self.bump_settings_version()
        self.set_meta("raw_format", RAW_FORMAT)

    # ---- meta (kept in memory too: read from many threads, written rarely)
    def meta(self, key: str, default: str = "") -> str:
        return self._meta.get(key, default)

    def set_meta(self, key: str, value) -> None:
        with self.lock, self.conn:
            self.conn.execute("insert or replace into meta values (?,?)", (key, str(value)))
            self._meta[key] = str(value)

    @property
    def settings_version(self) -> int:
        return int(self.meta("settings_version", "1") or 1)

    def bump_settings_version(self) -> int:
        with self.lock:
            version = self.settings_version + 1
            self.set_meta("settings_version", version)
            return version

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
        return {"assets": len(rows)}

    def asset(self, asset_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(f"select {_ITEM} from assets a where a.id=? and a.gone=0", (asset_id,)).fetchone()
        return dict(zip(_ITEM_KEYS, row)) if row else None

    def random_asset(self, kind: str = "") -> dict | None:
        with self.lock:
            row = self.conn.execute(
                f"select {_ITEM} from assets a where a.gone=0" + (" and a.type=?" if kind else "")
                + " order by random() limit 1", (kind,) if kind else ()).fetchone()
        return dict(zip(_ITEM_KEYS, row)) if row else None

    # ---- what to do next
    def work(self, n: int, skip=()) -> list[dict]:
        """Up to ``n`` assets to process: the queue first (oldest request first), then the assets that have no
        written result yet, newest first. Each item has a ``mode``. ``skip`` are ids that are being worked on."""
        skip = set(skip)
        out: list[dict] = []
        seen = set(skip)
        limit = n + len(skip)
        guard = (MAX_ATTEMPTS, _ago(RETRY_AFTER))
        with self.lock:
            rows = self.conn.execute(
                f"select {_ITEM}, q.mode from queue q join assets a on a.id=q.id where a.gone=0{_NOT_EXCLUDED}"
                f"{_NOT_FAILED} order by q.at, q.rowid limit ?", (*guard, limit)).fetchall()
            for row in rows:
                if row[0] not in seen and len(out) < n:
                    seen.add(row[0])
                    out.append({**dict(zip(_ITEM_KEYS, row[:7])), "mode": row[7]})
            if len(out) < n:
                rows = self.conn.execute(
                    f"select {_ITEM}, case when r.id is null then 'full' else 'retag' end from assets a"
                    f" left join results r on r.id=a.id where a.gone=0 and (r.id is null or r.written_at is null)"
                    f"{_NOT_EXCLUDED}{_NOT_FAILED} order by a.taken desc, a.id limit ?",
                    (*guard, limit + len(out))).fetchall()
                for row in rows:
                    if row[0] not in seen and len(out) < n:
                        seen.add(row[0])
                        out.append({**dict(zip(_ITEM_KEYS, row[:7])), "mode": row[7]})
        return out

    # ---- raw tagger scores
    def save_raw(self, asset_id: str, raw: dict) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                "insert or replace into raw values (?,?,?,?,?,?)",
                (asset_id, int(raw["captures"]), _dumps(raw["scores"]), _dumps(raw["ratings"]), _now(),
                 ",".join(raw.get("models") or [])))

    def raw(self, asset_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("select captures, scores_json, rating_json, models from raw where id=?",
                                    (asset_id,)).fetchone()
        if not row:
            return None
        return {"captures": row[0], "scores": json.loads(row[1]), "ratings": json.loads(row[2]),
                "models": [m for m in (row[3] or "").split(",") if m]}

    # ---- results
    def save_result(self, asset_id: str, *, tags: list[dict], vlm: dict | None, description: str, block: str,
                    version: int, note: str = "", written: bool = False) -> None:
        now = _now()
        with self.lock, self.conn:
            self.conn.execute(
                "insert or replace into results values (?,?,?,?,?,?,?,?,?)",
                (asset_id, _dumps(tags), _dumps(vlm) if vlm is not None else None, description, block, version, now,
                 now if written else None, note))
            self.conn.execute("delete from asset_tags where id=?", (asset_id,))
            self.conn.executemany("insert or ignore into asset_tags values (?,?)", [(asset_id, t["tag"]) for t in tags])
            self._changes += 1

    def result(self, asset_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "select tags_json, vlm_json, description, block, settings_version, processed_at, written_at, note"
                " from results where id=?", (asset_id,)).fetchone()
        if not row:
            return None
        return {"tags": json.loads(row[0]), "vlm": json.loads(row[1]) if row[1] else None, "description": row[2],
                "block": row[3], "settings_version": row[4], "processed_at": row[5], "written_at": row[6],
                "note": row[7] or ""}

    def mark_written(self, asset_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("update results set written_at=? where id=?", (_now(), asset_id))
            self.conn.execute("delete from failed where id=?", (asset_id,))
            self._changes += 1

    def add_history(self, asset_id: str, old: str, new: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("insert into history values (?,?,?,?)", (asset_id, _now(), old, new))
            self.conn.execute(
                "delete from history where id=? and rowid not in"
                " (select rowid from history where id=? order by rowid desc limit ?)", (asset_id, asset_id, HISTORY_KEEP))

    def history(self, asset_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("select at, old_description, new_description from history where id=?"
                                     " order by rowid", (asset_id,)).fetchall()
        return [{"at": r[0], "old": r[1], "new": r[2]} for r in rows]

    def forget(self, asset_id: str) -> None:
        """Drop everything stored about an asset's tagging (the history stays)."""
        with self.lock, self.conn:
            for table in ("results", "raw", "asset_tags", "failed", "queue"):
                self.conn.execute(f"delete from {table} where id=?", (asset_id,))
            self._changes += 1

    # ---- failures
    def fail(self, asset_id: str, error: str, final: bool = False) -> None:
        """Note a failure; it is tried again later unless ``final`` (e.g. the file isn't media at all)."""
        first = MAX_ATTEMPTS if final else 1
        with self.lock, self.conn:
            self.conn.execute(
                "insert into failed values (?,?,?,?) on conflict(id) do update set error=excluded.error,"
                " attempts=max(attempts+1, excluded.attempts), at=excluded.at", (asset_id, error[:300], first, _now()))
            self.conn.execute("delete from queue where id=? and (select attempts from failed where id=?) >= ?",
                              (asset_id, asset_id, MAX_ATTEMPTS))

    def retry_failed(self) -> int:
        with self.lock, self.conn:
            return self.conn.execute("delete from failed").rowcount

    def clear_failed(self) -> int:
        with self.lock, self.conn:
            return self.conn.execute("update failed set attempts=? where attempts < ?", (CLEARED, CLEARED)).rowcount

    def failures(self, limit: int = 8) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                f"select f.id, a.name, f.error, f.attempts, f.at from failed f join assets a on a.id=f.id"
                f" where a.gone=0 and f.attempts < {CLEARED} and not exists"
                f" (select 1 from results r where r.id=f.id and {_WRITTEN}) order by f.at desc limit ?",
                (limit,)).fetchall()
        return [{"id": r[0], "name": r[1], "error": r[2], "attempts": r[3], "at": r[4]} for r in rows]

    # ---- the queue
    def enqueue(self, ids: list[str], mode: str) -> int:
        """Ask for ``mode`` on these assets; a stronger mode replaces a weaker one already waiting.
        Asking is explicit, so earlier failures and an "exclude" are forgotten. Returns how many ids."""
        if mode not in RANK:
            raise ValueError("mode must be retag, describe or full")
        ids = list(dict.fromkeys(ids))
        if not ids:
            return 0
        now = _now()
        with self.lock, self.conn:
            self.conn.executemany(
                "insert into queue (id, mode, at) values (?,?,?) on conflict(id) do update set mode=excluded.mode,"
                " at=excluded.at where (case excluded.mode when 'full' then 3 when 'describe' then 2 else 1 end)"
                " > (case queue.mode when 'full' then 3 when 'describe' then 2 else 1 end)",
                [(i, mode, now) for i in ids])
            self.conn.executemany("delete from failed where id=?", [(i,) for i in ids])
            self.conn.executemany("delete from excluded where id=?", [(i,) for i in ids])
        return len(ids)

    def queue(self) -> list[dict]:
        with self.lock:
            return [{"id": r[0], "mode": r[1], "at": r[2]} for r in
                    self.conn.execute("select id, mode, at from queue order by at, rowid")]

    def dequeue(self, asset_id: str, done_mode: str) -> None:
        """Take an asset off the queue when what was just done is at least what was asked for."""
        with self.lock, self.conn:
            row = self.conn.execute("select mode from queue where id=?", (asset_id,)).fetchone()
            if row and RANK[row[0]] <= RANK[done_mode]:
                self.conn.execute("delete from queue where id=?", (asset_id,))

    def scope_ids(self, scope: str, ids=None, tag: str = "") -> list[str]:
        """The assets a reprocess applies to."""
        with self.lock:
            if scope == "ids":
                return list(dict.fromkeys(ids or []))
            base = f"select a.id from assets a join results r on r.id=a.id where a.gone=0 and {_WRITTEN}"
            if scope == "all":
                rows = self.conn.execute(base)
            elif scope == "outdated":
                rows = self.conn.execute(base + " and r.settings_version < ?", (self.settings_version,))
            elif scope == "tag":
                rows = self.conn.execute(base + " and exists (select 1 from asset_tags t where t.id=a.id and t.tag=?)",
                                         (norm_tag(tag),))
            else:
                raise ValueError("scope must be ids, tag, outdated or all")
            return [r[0] for r in rows]

    def exclude(self, ids: list[str]) -> int:
        now = _now()
        with self.lock, self.conn:
            self.conn.executemany("insert or ignore into excluded values (?,?)", [(i, now) for i in ids])
            self.conn.executemany("delete from queue where id=?", [(i,) for i in ids])
        return len(ids)

    # ---- counts and lists
    def counts(self) -> dict:
        with self.lock:
            return self._counts()

    def _counts(self) -> dict:
        q = lambda sql, *args: self.conn.execute(sql, args).fetchone()[0] or 0  # noqa: E731
        live = "a.gone=0"
        done = f"exists (select 1 from results r where r.id=a.id and {_WRITTEN})"
        total = q(f"select count(*) from assets a where {live}")
        failed_rows = f"from failed f join assets a on a.id=f.id where {live} and not {done}"
        return {
            "assets": total,
            "images": q(f"select count(*) from assets a where {live} and a.type='IMAGE'"),
            "videos": q(f"select count(*) from assets a where {live} and a.type='VIDEO'"),
            "processed": q(f"select count(*) from assets a where {live} and {done}"),
            "pending": q(f"select count(*) from assets a where {live} and not {done}{_NOT_EXCLUDED}"
                         " and not exists (select 1 from failed f where f.id=a.id)"),
            "queued": q(f"select count(*) from queue qq join assets a on a.id=qq.id where {live}{_NOT_EXCLUDED}"),
            "outdated": q(f"select count(*) from results r join assets a on a.id=r.id where {live} and {_WRITTEN}"
                          " and r.settings_version < ?", self.settings_version),
            "failed": q(f"select count(*) {failed_rows} and f.attempts < {CLEARED}"),
            "retrying": q(f"select count(*) {failed_rows} and f.attempts < {MAX_ATTEMPTS}"),
            "cleared": q(f"select count(*) {failed_rows} and f.attempts >= {CLEARED}"),
            "excluded": q(f"select count(*) from excluded e join assets a on a.id=e.id where {live}"),
            "catalogAt": self.meta("catalog_at") or None,
        }

    def top_tags(self, limit: int = 200) -> list[dict]:
        with self.lock:
            if self._tags_cache and self._tags_cache[0] == self._changes:
                return self._tags_cache[1]
            rows = self.conn.execute(
                "select t.tag, count(*) c from asset_tags t join assets a on a.id=t.id where a.gone=0"
                " group by t.tag order by c desc, t.tag limit ?", (limit,)).fetchall()
            tags = [{"tag": r[0], "count": r[1]} for r in rows]
            self._tags_cache = (self._changes, tags)
            return tags

    def list_assets(self, *, tag: str = "", q: str = "", outdated: bool = False, page: int = 1, size: int = 60) -> dict:
        where, args = [f"a.gone=0 and {_WRITTEN}"], []
        if tag:
            where.append("exists (select 1 from asset_tags t where t.id=a.id and t.tag=?)")
            args.append(norm_tag(tag))
        if q:
            like = _like(q.lower())
            where.append("(lower(a.name) like ? escape '\\' or lower(r.description) like ? escape '\\' or exists"
                         " (select 1 from asset_tags t where t.id=a.id and t.tag like ? escape '\\'))")
            args += [like, like, like]
        if outdated:
            where.append("r.settings_version < ?")
            args.append(self.settings_version)
        clause = " and ".join(where)
        join = "from assets a join results r on r.id=a.id"
        with self.lock:
            total = self.conn.execute(f"select count(*) {join} where {clause}", args).fetchone()[0]
            rows = self.conn.execute(
                f"select a.id, a.name, a.type, a.taken, r.tags_json, r.description, r.settings_version, r.processed_at"
                f" {join} where {clause} order by a.taken desc, a.id limit ? offset ?",
                (*args, size, (page - 1) * size)).fetchall()
        items = [{"id": r[0], "name": r[1], "type": r[2], "taken": r[3], "tags": [t["tag"] for t in json.loads(r[4])],
                  "description": r[5] or "", "settingsVersion": r[6], "processedAt": r[7]} for r in rows]
        return {"items": items, "total": total, "page": page, "tags": self.top_tags()}


# ---------------------------------------------------------------- one asset, start to finish

def synthetic_raw(captures: int) -> dict:
    """Stand-in for tagger scores when neither tagger is used (then there are no tags, so nothing to describe either)."""
    return {"captures": captures, "scores": [{"wd": None, "pixai": None}] * captures, "ratings": [None] * captures,
            "models": []}


def raw_from_results(results: list, errors: list) -> dict | str:
    """The tagger's answers for one asset's captures as a stored ``raw`` payload, or the error text.

    ``scores[i]`` holds capture i's calibrated tag scores per model (WD: general, character; PixAI: general,
    character, copyright), ``ratings[i]`` its raw rating probabilities as ``{"wd": {...}, "pixai": {...}}``.
    """
    good = [r for r in results if r]
    if not good:
        return next((e for e in errors if e), "the tagger could not read this picture")
    scores, ratings, models = [], [], set()
    for r in good:
        wd, pixai = r.get("wd"), r.get("pixai")
        if wd is not None:
            models.add("wd")
        if pixai is not None:
            models.add("pixai")
        scores.append({
            "wd": None if wd is None else {"general": wd.get("general") or {}, "character": wd.get("character") or {}},
            "pixai": None if pixai is None else {"general": pixai.get("general") or {},
                                                 "character": pixai.get("character") or {},
                                                 "copyright": pixai.get("copyright") or {}}})
        rating = {"wd": (wd or {}).get("rating") or None, "pixai": (pixai or {}).get("rating") or None}
        ratings.append(rating if any(rating.values()) else None)
    return {"captures": len(good), "scores": scores, "ratings": ratings, "models": sorted(models)}


class Pipeline:
    """What is done to an asset: captures, tagging, VLM, tags, write-back. Used by the Indexer and the Test card."""

    def __init__(self, store: Store, services, client_fn, frames=prepare_captures):
        self.store, self.services, self.client_fn, self.frames = store, services, client_fn, frames
        self._tag_ids: dict[str, str] = {}
        self._tag_lock = threading.Lock()

    @property
    def client(self):
        client = self.client_fn()
        if client is None:
            raise RuntimeError("the AI Tagger is not connected to Immich")
        return client

    def snapshot(self) -> tuple[int, dict]:
        """(settings version, settings). The version is read first, see ``apply_settings``."""
        version = self.store.settings_version
        return version, load_settings()

    # ---- tagging
    def tag_batch(self, batch: list[tuple[dict, list[bytes]]]) -> dict:
        """{asset id: stored-raw payload, or an error text} for assets whose captures go in one request."""
        results, errors = self.services.tag([f for _, frames in batch for f in frames])
        out, i = {}, 0
        for item, frames in batch:
            out[item["id"]] = raw_from_results(results[i:i + len(frames)], errors[i:i + len(frames)])
            i += len(frames)
        return out

    # ---- the VLM (text only: it is given the tags, never a picture)
    def run_vlm(self, item: dict, found: dict, settings: dict) -> dict:
        tags = sorted(({"tag": t, "score": round(v[0], 3)} for t, v in found["tags"].items()),
                      key=lambda t: (-t["score"], t["tag"]))
        if not tags:                # nothing to describe from: a text model would only make something up
            return {"description": "", "add_tags": [], "remove_tags": [],
                    "note": "no description: the taggers found no tags to describe from"}
        rating = found["display"].get("rating") or {}
        answer = self.services.describe(tags, rating, settings, item["type"], found["terms"])
        return {"description": answer.get("description", ""), "add_tags": list(answer.get("add_tags") or []),
                "remove_tags": list(answer.get("remove_tags") or []), "note": answer.get("note", "")}

    def decide(self, item: dict, mode: str, raw: dict, settings: dict,
               stored_vlm: dict | None = None) -> tuple[dict, dict | None]:
        """(the decision, the VLM answer): the VLM runs unless ``mode`` is retag (then the stored answer is used)."""
        vlm = None
        if settings["describe"]:
            if mode == "retag":
                vlm = stored_vlm
            else:
                vlm = self.run_vlm(item, detect(raw, settings), settings)
        return build(raw, vlm, settings), vlm

    # ---- Immich
    @staticmethod
    def _immich(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except AuthError:
            raise
        except NotFoundError as exc:
            raise AssetGone("Immich does not have this asset any more") from exc
        except ImmichError as exc:
            if exc.status is None or exc.status >= 500 or exc.status == 429:
                raise ImmichDown(str(exc)) from exc
            raise

    def current(self, asset_id: str) -> tuple[str, dict]:
        """(the asset's description in Immich, the whole asset)."""
        asset = self._immich(self.client.get_asset, asset_id)
        return (asset.get("exifInfo") or {}).get("description") or "", asset

    def _ids_for(self, names: list[str]) -> dict[str, str]:
        """Tag ids for these full tag names, creating the tags that do not exist."""
        with self._tag_lock:
            missing = [n for n in names if n not in self._tag_ids]
            if missing:
                for tag in self._immich(self.client.upsert_tags, missing):
                    self._tag_ids[tag.get("value") or tag.get("name")] = tag["id"]
            absent = [n for n in names if n not in self._tag_ids]
            if absent:
                raise RuntimeError(f"Immich did not make the tag {absent[0]!r}")
            return {n: self._tag_ids[n] for n in names}

    def sync_native(self, asset_id: str, asset: dict, wanted: list[str]) -> None:
        """Keep the asset's ``AI/`` tags equal to ``wanted``: attach what is missing, detach what is stale."""
        want = {NATIVE_PREFIX + t for t in wanted}
        have = {(t.get("value") or t.get("name") or ""): t["id"] for t in asset.get("tags") or []
                if (t.get("value") or t.get("name") or "").startswith(NATIVE_PREFIX)}
        add = sorted(want - set(have))
        if add:
            try:
                self._immich(self.client.tag_assets, list(self._ids_for(add).values()), [asset_id])
            except ImmichError:
                with self._tag_lock:
                    self._tag_ids.clear()           # a tag was deleted behind our back: look them up again
                self._immich(self.client.tag_assets, list(self._ids_for(add).values()), [asset_id])
        for name, tag_id in have.items():
            if name not in want:
                self._immich(self.client.untag_assets, tag_id, [asset_id])

    def write_back(self, asset_id: str, block: str, tags: list[str], settings: dict) -> dict:
        """Put ``block`` into the asset's description (the owner's text untouched), read it back, keep the old
        description in history, and keep the ``AI/`` tags in step. Returns {"old", "new", "changed"}."""
        old, asset = self.current(asset_id)
        new = merge_description(old, block)
        if new != old:
            self._immich(self.client.update_asset, asset_id, description=new)
            back = (self._immich(self.client.get_asset, asset_id).get("exifInfo") or {}).get("description") or ""
            if not same_text(back, new):
                raise WriteMismatch("Immich did not keep the new description as written")
            self.store.add_history(asset_id, old, new)
        if settings["write_tags"] or self.store.meta("native_tags") == "1":
            self.sync_native(asset_id, asset, tags if settings["write_tags"] else [])
        return {"old": old, "new": new, "changed": new != old}

    def commit(self, item: dict, mode: str, decision: dict, vlm: dict | None, version: int, settings: dict) -> dict:
        """Store the result, write it to Immich (and read it back), mark it written."""
        self.store.save_result(item["id"], tags=decision["tags"], vlm=vlm, description=decision["description"],
                               block=decision["block"], version=version, note=(vlm or {}).get("note", ""))
        written = self.write_back(item["id"], decision["block"], [t["tag"] for t in decision["tags"]], settings)
        self.store.mark_written(item["id"])
        self.store.dequeue(item["id"], mode)
        return written

    # ---- the whole thing for one asset (Indexer)
    def process(self, item: dict, mode: str, raw: dict, settings: dict, version: int,
                covers: str | None = None) -> dict:
        """Do ``mode`` for one asset. ``covers`` is what the work counts as when taking it off the queue."""
        previous = self.store.result(item["id"])
        decision, vlm = self.decide(item, mode, raw, settings, (previous or {}).get("vlm"))
        written = self.commit(item, covers or mode, decision, vlm, version, settings)
        return {"decision": decision, "vlm": vlm, "write": written}

    def lookup(self, asset_id: str, refresh=None) -> dict:
        item = self.store.asset(asset_id)
        if item is None and refresh is not None and not self.store.meta("catalog_at"):
            refresh()
            item = self.store.asset(asset_id)
        if item is None:
            raise NotFound("That photo is not in the AI Tagger's library list (yet).")
        return item

    def test(self, asset_id: str, *, write: bool = False, refresh=None) -> dict:
        """The Test card: run the whole pipeline on one asset; store and write only when ``write``."""
        item = self.lookup(asset_id, refresh)
        version, settings = self.snapshot()
        tagger_used = settings["use_wd"] or settings["use_pixai"]
        self.services.ensure_ready(need_tagger=tagger_used, need_vlm=needs_vlm(settings), wait=PREVIEW_WAIT)
        try:
            frames, _kind = self.frames(item, settings["video_frames"])
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not read this {item['type'].lower()}: {exc}") from exc
        if tagger_used:
            raw = self.tag_batch([(item, frames)])[item["id"]]
            if isinstance(raw, str):
                raise ValueError(f"The tagger could not read this picture: {raw}")
        else:
            raw = synthetic_raw(len(frames))
        decision, vlm = self.decide(item, "full", raw, settings)
        current, _asset = self.current(asset_id)
        out = {"id": item["id"], "name": item["name"], "type": item["type"], "captures": len(frames),
               "frames": [data_url(_shrunk(f, PREVIEW_SIDE)) for f in frames],
               "models": decision["models"],
               "vlm": {"description": (vlm or {}).get("description", ""), "add_tags": (vlm or {}).get("add_tags", []),
                       "remove_tags": (vlm or {}).get("remove_tags", []), "note": (vlm or {}).get("note", "")},
               "rules": decision["rules"], "tags": decision["tags"], "description": decision["description"],
               "block": decision["block"], "currentDescription": current,
               "newDescription": merge_description(current, decision["block"]), "written": False}
        if write:
            if tagger_used:
                self.store.save_raw(item["id"], raw)
            self.commit(item, "full", decision, vlm, version, settings)
            out["written"] = True
        return out

    def remove(self, ids: list[str], exclude: bool) -> dict:
        """Strip the block (and the ``AI/`` tags) from these assets and forget their results."""
        removed, failed = 0, []
        native = self.store.meta("native_tags") == "1"
        for asset_id in ids:
            try:
                old, asset = self.current(asset_id)
                new = strip_block(old)
                if new != old:
                    self._immich(self.client.update_asset, asset_id, description=new)
                    self.store.add_history(asset_id, old, new)
                if native:
                    self.sync_native(asset_id, asset, [])
            except AssetGone:
                pass                            # not in Immich any more: nothing to strip
            except (ImmichError, ServiceDown) as exc:
                failed.append({"id": asset_id, "error": str(exc)})
                continue
            self.store.forget(asset_id)
            removed += 1
        excluded = self.store.exclude(ids) if exclude else 0
        return {"removed": removed, "excluded": excluded, "failed": failed}


def _shrunk(jpeg: bytes, side: int) -> bytes:
    from .media import shrink_image
    return shrink_image(jpeg, side)


def reprocess(store: Store, scope: str, mode: str, ids=None, tag: str = "") -> int:
    """Queue assets for a reprocess; returns how many."""
    if mode not in RANK:
        raise ValueError("mode must be retag, describe or full")
    if scope == "ids":
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ValueError("ids must be a list of asset ids")
    elif scope == "tag":
        if not isinstance(tag, str) or not norm_tag(tag):
            raise ValueError("Give the tag to reprocess.")
    elif scope not in ("outdated", "all"):
        raise ValueError("scope must be ids, tag, outdated or all")
    return store.enqueue(store.scope_ids(scope, ids, tag), mode)


# ---------------------------------------------------------------- the background worker

class Indexer:
    """Background thread that tags and describes the assets; stop / start at will, it resumes where it was.

    Work order: the queue (asked-for reprocessing) first, then the assets with no written result, newest first.
    Pictures are prepared by ``WORKERS`` threads, at most ``TAG_REQUESTS`` tagger requests are in flight, and
    ``vlm_parallel`` assets are in the VLM / write-back stage at once.
    """

    CATALOG_EVERY = 600         # re-read the library list this often (and look for new photos)
    WORKERS = 6                 # threads reading previews / cutting video frames
    TAG_REQUESTS = 2            # tagger requests in flight
    IDLE_POLL = 60              # seconds between looks at the idle clock while there is nothing to do
    DROP_WAIT = 10              # seconds to wait (times the number of drops in a row) after a server went away

    def __init__(self, store: Store, services, *, client=None, catalog=fetch_catalog, frames=prepare_captures,
                 clock=time.monotonic):
        self.store, self.services, self.catalog, self.clock, self.client = store, services, catalog, clock, client
        self.pipe = Pipeline(store, services, lambda: self.client, frames)
        self.state, self.detail, self.error = "stopped", "", ""
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.last_sync = None
        self.done_times: collections.deque = collections.deque(maxlen=2000)
        self.batch_cap: int | None = None       # halved by a CUDA out-of-memory answer, for this session
        self._cap_for = None
        self._round_batch = DEFAULTS["batch_size"]
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._flight_lock = threading.Lock()
        self._wake = threading.Event()          # cuts the "up to date" wait short (Start, Try again, reprocess)
        self._inflight: set[str] = set()
        self._pending: set = set()
        self._done = 0                          # assets finished in this process (progress, for the drop counter)
        self._vlm_limit = DEFAULTS["vlm_parallel"]
        self._prep = self._tagpool = self._vlmpool = None

    @property
    def frames(self):
        return self.pipe.frames

    @frames.setter
    def frames(self, fn) -> None:
        self.pipe.frames = fn

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self.running():
                self._wake.set()                # already waiting for work: look again now
                return
            self.stop_event = threading.Event()
            self.error = ""
            self.state, self.detail = "starting", "starting"
            self.thread = threading.Thread(target=self._run, args=(self.stop_event,), name="aitagger-indexer",
                                           daemon=True)
            self.thread.start()

    def stop(self, wait_s: float = 0) -> None:
        self.stop_event.set()
        self._wake.set()
        if wait_s and self.thread:
            self.thread.join(wait_s)

    def poke(self) -> None:
        """New work was queued: wake an indexer that is waiting for it."""
        self._wake.set()

    def refresh_catalog(self) -> None:
        with self._sync_lock:
            self.store.sync_catalog(self.catalog())
            self.last_sync = self.clock()

    def test(self, asset_id: str, write: bool = False) -> dict:
        return self.pipe.test(asset_id, write=write, refresh=self.refresh_catalog)

    def remove(self, ids: list[str], exclude: bool) -> dict:
        return self.pipe.remove(ids, exclude)

    def batch_size(self, settings: dict) -> int:
        want = int(settings["batch_size"])
        if self.batch_cap is not None and self._cap_for == want:
            return max(1, min(want, self.batch_cap))
        return want

    def rate_per_min(self) -> float:
        """Assets finished per minute over the last five minutes."""
        now = self.clock()
        recent = [d for d in self.done_times if now - d[0] < 300]
        if len(recent) < 3:
            return 0.0
        return round(len(recent) * 60 / max(now - recent[0][0], 1.0), 1)

    # ---- the loop
    def _run(self, stop: threading.Event) -> None:
        drops, last_done = 0, self._done
        try:
            settings = load_settings()
            self._vlm_limit = max(1, int(settings["vlm_parallel"]))
            self._prep = ThreadPoolExecutor(self.WORKERS, thread_name_prefix="aitagger-prep")
            self._tagpool = ThreadPoolExecutor(self.TAG_REQUESTS, thread_name_prefix="aitagger-tag")
            self._vlmpool = ThreadPoolExecutor(self._vlm_limit, thread_name_prefix="aitagger-vlm")
            while not stop.is_set():
                try:
                    outcome = self._step(stop)
                    if outcome == "over":
                        return
                    if outcome == "idle" or self._done != last_done:
                        drops = 0               # the server is answering again: something got done
                    last_done = self._done
                except ServiceDown as exc:
                    self._drain()
                    drops += 1                  # a model server went away (crash, restart): start it again
                    if stop.is_set() or drops >= 3:
                        raise
                    self.detail = f"{exc}; trying again"
                    stop.wait(self.DROP_WAIT * drops)
            self.state, self.detail = "stopped", "paused"
        except ServiceDown as exc:
            if stop.is_set():
                self.state, self.detail = "stopped", "paused"
            else:
                self.state, self.detail, self.error = "error", f"stopped: {exc}", str(exc)
        except Exception as exc:  # noqa: BLE001
            self.state, self.detail, self.error = "error", f"{type(exc).__name__}: {exc}", f"{type(exc).__name__}: {exc}"
        finally:
            self._drain()
            for pool in (self._tagpool, self._vlmpool):
                if pool:
                    pool.shutdown(wait=True, cancel_futures=True)
            if self._prep:                      # one slow video (a huge file) must not hold up pausing
                self._prep.shutdown(wait=False, cancel_futures=True)
            self._prep = self._tagpool = self._vlmpool = None

    def _step(self, stop: threading.Event) -> str:
        """One look for work and one round of it: "over" when the run is finished, "idle" when there was nothing
        to do, else "busy"."""
        version, settings = self.pipe.snapshot()
        if self.last_sync is None or self.clock() - self.last_sync >= self.CATALOG_EVERY:
            self.state, self.detail = "running", "reading the library list"
            self.refresh_catalog()
        items = self.store.work(self.batch_size(settings), skip=self._flying())
        if not items:
            if self._pending:
                self._reap(0, stop)             # earlier assets are still being described / written
                return "busy"
            if not settings["keep_updated"]:
                self.state, self.detail = "done", "everything is tagged"
                return "over"
            self.state, self.detail = "done", "everything is tagged; looks for new photos every 10 minutes"
            self.services.idle_check()
            self._wake.wait(min(self.IDLE_POLL, max(self.CATALOG_EVERY - (self.clock() - self.last_sync), 1)))
            self._wake.clear()
            return "idle"
        self.state, self.detail = "running", "tagging"
        self._round(items, settings, version, stop)
        self._reap(self._vlm_limit, stop)       # never more than a pool-full waiting for the VLM
        return "busy"

    # ---- who is being worked on
    def _flying(self) -> set:
        with self._flight_lock:
            return set(self._inflight)

    def _claim(self, asset_id: str) -> None:
        with self._flight_lock:
            self._inflight.add(asset_id)

    def _release(self, asset_id: str) -> None:
        with self._flight_lock:
            self._inflight.discard(asset_id)

    def _fail(self, item: dict, exc: Exception) -> None:
        # ValueError = we looked at the file and it isn't usable: no point retrying
        self.store.fail(item["id"], f"{type(exc).__name__}: {exc}", final=isinstance(exc, ValueError))
        item["_handed"] = True
        self._release(item["id"])

    # ---- one round
    def _round(self, items: list[dict], settings: dict, version: int, stop: threading.Event) -> None:
        self._round_batch = int(settings["batch_size"])
        for item in items:
            self._claim(item["id"])
        try:
            local, captured = [], []
            for item in items:
                item["asked"] = item["mode"]
                mode, raw = item["mode"], None
                if mode == "describe" and not settings["describe"]:
                    mode = "retag"              # nothing to ask the VLM: the stored scores are all there is
                if mode in ("retag", "describe"):
                    raw = self.store.raw(item["id"])
                    if raw is None or (settings["use_pixai"] and not has_pixai(raw)):
                        mode = "full"           # nothing stored to work from, or only the v1 service's scores (no PixAI)
                item["mode"], item["raw"] = mode, raw
                (captured if mode == "full" else local).append(item)
            # No pictures are needed for these: the stored scores and, for "retag", the stored answer. A "describe"
            # asks the (text-only) language model again, so only that one has to be up.
            if needs_vlm(settings) and any(i["mode"] == "describe" for i in local):
                self.state, self.detail = "starting", "waiting for the language model"
                self.services.ensure_ready(need_tagger=False, need_vlm=True, wait=3600, stop=stop,
                                           progress=lambda d: setattr(self, "detail", d))
                self.state, self.detail = "running", "describing"
            for item in local:
                self._submit_finish(item, settings, version)
            if captured:
                self._capture_and_tag(captured, settings, version, stop)
        finally:
            for item in items:
                if not item.get("_handed"):
                    self._release(item["id"])

    def _capture_and_tag(self, items: list[dict], settings: dict, version: int, stop: threading.Event) -> None:
        need_tagger = settings["use_wd"] or settings["use_pixai"]
        self.state, self.detail = "starting", "waiting for the models"
        self.services.ensure_ready(need_tagger=need_tagger, need_vlm=needs_vlm(settings), wait=3600, stop=stop,
                                   progress=lambda d: setattr(self, "detail", d))
        self.state, self.detail = "running", "tagging"
        futures = {self._prep.submit(self.pipe.frames, item, settings["video_frames"]): item for item in items}
        batch: list[tuple[dict, list[bytes]]] = []
        images, tag_futs = 0, set()
        try:
            for fut in as_completed(futures):
                if stop.is_set():
                    break
                item = futures[fut]
                try:
                    frames, _kind = fut.result()
                except Exception as exc:  # noqa: BLE001 - unreadable file: note it and go on
                    self._fail(item, exc)
                    continue
                if not need_tagger:             # no tagger is on: the describer has no tags to go on
                    item["raw"] = synthetic_raw(len(frames))
                    self._submit_finish(item, settings, version)
                    continue
                if batch and images + len(frames) > MAX_IMAGES_PER_REQUEST:
                    tag_futs = self._send(batch, tag_futs, settings, version)
                    batch, images = [], 0
                batch.append((item, frames))
                images += len(frames)
                item["_handed"] = True
                if len(batch) >= self.batch_size(settings):
                    tag_futs = self._send(batch, tag_futs, settings, version)
                    batch, images = [], 0
            if batch and not stop.is_set():
                tag_futs = self._send(batch, tag_futs, settings, version)
                batch = []
            for fut in list(tag_futs):
                fut.result()                    # re-raises ServiceDown
        finally:
            for fut in futures:
                fut.cancel()
            for item, _ in batch:               # a batch never sent (stopped): back to the pool of work
                self._release(item["id"])
            wait(list(tag_futs))

    def _send(self, batch: list, tag_futs: set, settings: dict, version: int) -> set:
        while len(tag_futs) >= self.TAG_REQUESTS:       # one on the GPU, one being prepared
            done, tag_futs = wait(tag_futs, return_when=FIRST_COMPLETED)
            for fut in done:
                fut.result()
        tag_futs = set(tag_futs)
        tag_futs.add(self._tagpool.submit(self._tag_stage, batch, settings, version))
        return tag_futs

    def _tag_stage(self, batch: list, settings: dict, version: int) -> None:
        try:
            results = self._tag_halving(batch)
        except BaseException:
            for item, _ in batch:
                self._release(item["id"])
            raise
        for item, _frames in batch:
            res = results[item["id"]]
            if isinstance(res, str):
                self.store.fail(item["id"], res)
                self._release(item["id"])
                continue
            self.store.save_raw(item["id"], res)
            item["raw"] = res
            self._submit_finish(item, settings, version)

    def _tag_halving(self, batch: list) -> dict:
        """Tag a batch; when the card is out of memory, halve the batch size (for this session) and retry."""
        try:
            return self.pipe.tag_batch(batch)
        except GpuOOM as exc:
            half = max(1, len(batch) // 2)
            self.batch_cap, self._cap_for = half if len(batch) > 1 else 1, self._round_batch
            if len(batch) == 1:
                return {batch[0][0]["id"]: f"out of graphics memory, even for this one picture ({exc})"[:300]}
            self.detail = f"the graphics card is full: now {half} assets at a time"
            out = self._tag_halving(batch[:half])
            out.update(self._tag_halving(batch[half:]))
            return out

    # ---- the VLM / write-back stage
    def _submit_finish(self, item: dict, settings: dict, version: int) -> None:
        item["_handed"] = True
        fut = self._vlmpool.submit(self._finish, item, settings, version)
        with self._flight_lock:
            self._pending.add(fut)

    def _finish(self, item: dict, settings: dict, version: int) -> None:
        try:
            # "describe" with describe switched off was done as a retag: that is all there is to do, so it is done
            covers = item["asked"] if item["asked"] == "describe" and item["mode"] == "retag" else item["mode"]
            self.pipe.process(item, item["mode"], item["raw"], settings, version, covers)
            self.done_times.append((self.clock(), 1))
            with self._flight_lock:
                self._done += 1
        except (ServiceDown, AuthError):
            raise                               # not the asset's fault: it stays untagged for the next look
        except Exception as exc:  # noqa: BLE001
            self.store.fail(item["id"], f"{type(exc).__name__}: {exc}", final=isinstance(exc, ValueError))
        finally:
            self._release(item["id"])

    def _reap(self, limit: int, stop: threading.Event | None = None) -> None:
        """Wait until at most ``limit`` assets are in the VLM stage; re-raise the first failure that matters."""
        while True:
            with self._flight_lock:
                done = {f for f in self._pending if f.done()}
                self._pending -= done
                waiting = set(self._pending)
            for fut in done:
                if fut.exception() is not None:
                    raise fut.exception()
            if len(waiting) <= limit or (stop is not None and stop.is_set()):
                return
            wait(waiting, timeout=1, return_when=FIRST_COMPLETED)

    def _drain(self) -> None:
        """Wait for everything in the VLM stage to finish (their outcome is already recorded or doesn't matter)."""
        with self._flight_lock:
            pending = list(self._pending)
        if pending:
            wait(pending)
        with self._flight_lock:
            self._pending -= {f for f in self._pending if f.done()}

    def status(self, counts: dict | None = None) -> dict:
        counts = counts or self.store.counts()
        rate = self.rate_per_min() if self.state == "running" else 0.0
        eta = round((counts["pending"] + counts["queued"]) / rate) if rate else None
        return {"state": self.state, "detail": self.detail, "error": self.error or None, "running": self.running(),
                "ratePerMin": rate or None, "etaMinutes": eta}


# ---------------------------------------------------------------- one per panel process

_LOCK = threading.Lock()
_INSTANCE: dict = {}


def _watch(services, every: float = 60) -> None:
    """Stops the VLM container when nothing has used it for ``IDLE_EXIT_MINUTES`` (also while tagging is paused)."""
    def loop() -> None:
        while True:
            time.sleep(every)
            try:
                services.idle_check()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=loop, name="aitagger-idle", daemon=True).start()


def instance(client=None) -> tuple[Store, Services, Indexer]:
    """The panel's store / model containers / indexer (created on first use; ``client`` is the Immich client)."""
    with _LOCK:
        key = str(home())
        if key not in _INSTANCE:
            store = Store()
            services = Services(store=store)
            _INSTANCE[key] = (store, services, Indexer(store, services, client=client))
            _watch(services)
        parts = _INSTANCE[key]
        if client is not None and parts[2].client is None:
            parts[2].client = client
        return parts


def autostart(client=None) -> None:
    """Resume tagging after a panel restart if it was on (and start watching the idle clock either way)."""
    try:
        parts = instance(client)
        if load_settings().get("indexing"):
            parts[2].start()
    except Exception:  # noqa: BLE001 - the panel must start regardless
        pass
