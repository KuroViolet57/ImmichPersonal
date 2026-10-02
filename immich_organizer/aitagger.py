"""AI Tagger: tags every photo and video, and writes the result into the asset's Immich description.

Image taggers (WD EVA02 and PixAI v1.0, both Danbooru-style: illustration / anime, people, clothing, characters, series;
and RAM++, plain-English photo tags) say what is in the picture. The registry ``TAGGERS`` lists them; everything that
used to be hard-wired to two taggers loops over it. The result is only tags: a managed ``[AI Tagger]`` block inside
the Immich description (``Tags: a, b, c``); the owner's own text is never changed. Contract: ``docs/AI-TAGGER.md`` (its
v3 section is binding).

Pieces:
* the tagger registry, and the settings generated from it, and the SQLite ``Store`` (catalogue, raw tagger scores,
  results, history, queue);
* pure functions: ``detect``/``finalize`` (scores -> tags), ``apply_rules``, ``compose_block`` and
  ``merge_description`` (the block, with the owner's text kept exactly);
* ``Tagger``: HTTP client of the tagger container; ``Services``: starts / stops that container;
* ``Pipeline``: everything done to one asset (captures, tagging, write-back, read-back, history);
* ``Indexer``: a background thread that works through the queue and the untagged assets, like Search+.
"""

from __future__ import annotations

import base64
import collections
import dataclasses
import functools
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
TAGGER_CONTAINER = searchplus.AITAGGER_CONTAINER
COMPOSE = Path(__file__).resolve().parent.parent / "deploy" / "aitagger" / "docker-compose.yml"
PROJECT = "immich-aitagger"
TAGGER_SERVICE = "tagger"      # the only compose service this module starts and stops

# ---- graphics memory. PROVISIONAL: the v3 numbers (three taggers) are still being measured; change them here only.
VRAM_GB_DEFAULT = 6           # setting `vram_gb`, its default: the memory cap of the taggers (AITAGGER_VRAM_GB)
VRAM_GB_LIMITS = (5, 8)       # what `vram_gb` may be set to (four taggers don't load under ~5 GB; 6 = full speed)
# ----

DEFAULT_GPU_GB = 24           # when nvidia-smi can't say
STATE_TTL = 5.0               # seconds a container's state is remembered (the status route is polled)
FLOOR = 0.2                   # the tagger returns calibrated scores from this up (at 0.05 RAM++ alone sent ~4,400
                              # tags per picture, all of them stored; nothing below 0.2 is ever kept or shown)
DISPLAY_FLOOR = 0.2           # the Test card lists scores from this up (the kept ones always)
CAPTURE_SIDE = 1024           # captures are at most this big
PREVIEW_SIDE = 256            # pictures in the Test card
SEGMENTS = 8                  # a video is cut into this many equal parts; the first and last are skipped
MAX_IMAGES_PER_REQUEST = 64   # what the tagger accepts in one /tag call
MAX_RULE_PASSES = 5           # the rules (the Rules card's and the Vocabulary's) repeat at most this many times
# Sexual Danbooru tags (normalised). On a picture the combined rating calls general or sensitive, one of these is kept
# only when at least two enabled taggers found it (with a single tagger on it is dropped): measured on the library, WD
# alone tagged an everyday photo of a person by a door "oral, fellatio, loli, cunnilingus" while PixAI saw "indoors,
# shirt, shorts" and both ratings said general.
EXPLICIT_TAGS = frozenset(t.strip() for t in """
    sex, vaginal, anal, oral, fellatio, irrumatio, deepthroat, cunnilingus, anilingus, paizuri, handjob, footjob,
    thighjob, masturbation, fingering, group sex, gangbang, threesome, foursome, orgy, rape, implied sex,
    implied fellatio, after sex, after vaginal, after anal, sex from behind, doggystyle, missionary, cowgirl position,
    reverse cowgirl position, girl on top, mating press, 69, penis, large penis, small penis, veiny penis, dark penis,
    penis on head, penis on face, erection, testicles, foreskin, glans, pussy, spread pussy, clitoris, labia, anus,
    nipples, areolae, nude, completely nude, bottomless, pubic hair, female pubic hair, male pubic hair, cum,
    cum in pussy, cum in mouth, cum on body, cum on breasts, cum on hair, facial, bukkake, ejaculation, cumdrip,
    creampie, pussy juice, precum, sex toy, dildo, vibrator, condom, used condom, surrounded by penises,
    clothed female nude male, clothed male nude female, hetero, loli, shota, toddlercon, uncensored, censored,
    mosaic censoring, bar censor, cameltoe, exhibitionism, public indecency, prostitution, bdsm, lactation,
    breast sucking, groping, molestation, sex machine, tentacle sex, bestiality
""".replace("\n", " ").split(",") if t.strip())
MAX_ATTEMPTS = searchplus.MAX_ATTEMPTS
CLEARED = searchplus.CLEARED
RETRY_AFTER = searchplus.RETRY_AFTER
PREVIEW_WAIT = 90             # seconds Test / Write this wait for the models before saying "try again"
HISTORY_KEEP = 20             # old descriptions kept per asset
EPS = 1e-9

OPEN, CLOSE = "[AI Tagger]", "[/AI Tagger]"
RATING_PREFIX = "rating: "
NATIVE_PREFIX = "AI/"
MODES = ("retag", "full")
RANK = {"retag": 1, "full": 2}
LEGACY_MODES = {"describe": "retag"}     # the v2 mode: a stored or requested "describe" is handled as "retag"


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


class GpuBroken(ServiceDown):
    """The tagger's connection to the graphics card died (e.g. "CUDA failure 999: unknown error"). Every picture then
    fails instantly while /health still says ok: on 2026-10-02 that marked 9,408 assets as failed in minutes. It is
    the tagger's failure, not the pictures': the container is stopped so the next round starts a fresh one."""


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


# The Vocabulary text: one entry per line; blank lines and lines starting with "#" are ignored.
#   old -> new               rename a tag (a single tag on both sides, no sign)
#   a + b -> c               when the asset has a AND b, add c
#   a | b -> c               when it has a OR b, add c
#   a + !b -> c              a and NOT b (! on any tag of a + line; not with |)
#   a + b -> c, -d           several targets, comma separated: "-tag" removes, "+tag" or a bare tag adds
#   a -> +b                  add b and keep a (one condition with an explicit sign is a rule, not a rename)
# A line with + or | (or with a sign / several targets) becomes a rule in the shape of the Rules card
# ({if_all, if_any, unless, add, remove}). Those rules run after the card's, in the order written, in the same
# repeat-until-stable engine (``apply_rules``).
ARROWS = ("->", "→")
_OPERATOR = re.compile(r"(\s+[+|]\s+)")                   # + and | are operators only with spaces around them ...
_STRAY_OPERATOR = re.compile(r"(?:^|\s)[+|](?:\s|$)|\w\s*[+|]\s*\w")      # ... so "a+b" is caught; "c++" and "+_+" are tags
MAX_PROBLEMS_SHOWN = 5


def _unix(text) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def parse_vocabulary_line(line: str):
    """One entry of the vocabulary: ``("rename", (old, new))``, ``("rule", rule)`` or ``("skip", None)`` (a rename to
    itself). Raises ValueError, without a line number, when the line can't be used."""
    arrows = sum(line.count(a) for a in ARROWS)
    if not arrows:
        raise ValueError('write it as "tags -> result", for example a + b -> c')
    if arrows > 1:
        raise ValueError("only one -> per line")
    arrow = next(a for a in ARROWS if a in line)
    left, right = (part.strip() for part in line.split(arrow, 1))
    if not left:
        raise ValueError("there is nothing before ->")
    pieces = _OPERATOR.split(left)
    operators = {piece.strip() for piece in pieces[1::2]}
    if len(operators) > 1:
        raise ValueError("use + or |, not both")
    operator = next(iter(operators), "")
    required: list[str] = []
    excluded: list[str] = []
    for token in pieces[0::2]:
        token = token.strip()
        negated = token.startswith("!")
        text = token[1:] if negated else token
        if _STRAY_OPERATOR.search(text):
            raise ValueError("put a tag on each side of + or |, with spaces around it (a + b)")
        tag = norm_tag(text)
        if not tag:
            raise ValueError("a tag is missing before or after + / | / !")
        group = excluded if negated else required
        if tag not in group:
            group.append(tag)
    if operator == "|" and excluded:
        raise ValueError("! can't be used with | (a + !b means 'a and not b')")
    if not required:
        raise ValueError("it needs at least one tag that must be present, not only !tags")
    if set(required) & set(excluded):
        raise ValueError("a tag can't be both required and excluded")
    targets: list[tuple[str, str]] = []                   # (sign, tag); the sign is "" for a bare tag
    for segment in right.split(","):
        segment = segment.strip()
        if not segment:
            continue
        sign = segment[0] if segment[0] in "+-" else ""
        text = segment[1:] if sign else segment
        if not sign and text.startswith("!"):
            raise ValueError("! only works on the left of -> (a + !b -> c)")
        tag = norm_tag(text)
        if not tag:
            raise ValueError(f'"{sign}" needs a tag after it' if sign else "a tag is missing after ->")
        targets.append((sign, tag))
    if not targets:
        raise ValueError("there is nothing after ->")
    if not operator and len(required) == 1 and not excluded and len(targets) == 1 and not targets[0][0]:
        old, new = required[0], targets[0][1]
        return ("skip", None) if old == new else ("rename", (old, new))
    add = list(dict.fromkeys(tag for sign, tag in targets if sign != "-"))
    remove = list(dict.fromkeys(tag for sign, tag in targets if sign == "-"))
    if set(add) & set(remove):
        raise ValueError("a tag can't be both added and removed")
    for where, tags in (("before", [*required, *excluded]), ("after", [*add, *remove])):
        if len(tags) > MAX_RULE_TAGS:
            raise ValueError(f"at most {MAX_RULE_TAGS} tags {where} ->")
    return "rule", {"if_all": [] if operator == "|" else required, "if_any": required if operator == "|" else [],
                    "unless": excluded, "add": add, "remove": remove}


class Vocabulary:
    """The Vocabulary setting. ``renames`` (old -> new) are applied to the tags before anything else sees them;
    ``rules`` are the combination lines (``a + b -> c``) in the shape of the Rules card, each with a ``label``
    (``"line 3"``) that the preview's trace shows; ``errors`` are ``(line number, message)`` for the lines that can't
    be used. Reading is forgiving: such a line is skipped (the v2 vocabulary had preferred terms on lines without an
    arrow). Saving is not: ``validate_setting`` refuses a vocabulary that has errors."""

    def __init__(self, text: str = ""):
        self.renames: dict[str, str] = {}
        self.rules: list[dict] = []
        self.errors: list[tuple[int, str]] = []
        for number, line in enumerate(_unix(text).split("\n"), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                kind, value = parse_vocabulary_line(line)
            except ValueError as exc:
                self.errors.append((number, str(exc)))
                continue
            if kind == "rename":
                self.renames[value[0]] = value[1]
            elif kind == "rule":
                self.rules.append({**value, "label": f"line {number}"})

    def rename(self, tag: str) -> str:
        return self.renames.get(tag, tag)

    def problems(self) -> str:
        """The errors as one message for the person who typed the text; "" when there are none."""
        shown = [f"Line {n}: {message}" for n, message in self.errors[:MAX_PROBLEMS_SHOWN]]
        if len(self.errors) > MAX_PROBLEMS_SHOWN:
            shown.append(f"{len(self.errors) - MAX_PROBLEMS_SHOWN} more lines have problems")
        return "; ".join(shown)


@functools.lru_cache(maxsize=16)
def _vocabulary(text: str) -> Vocabulary:
    """The same parse for every asset of a run. Read-only: callers must not change it."""
    return Vocabulary(text)


# ---------------------------------------------------------------- the taggers (the registry)

@dataclasses.dataclass(frozen=True)
class TaggerKind:
    """One image tagger of the tagger container. Its ``key`` is the key of its answer in ``/tag``, its name in the
    request's ``models`` list, and the stem of its two settings ``use_<key>`` and ``<key>_strictness``."""

    key: str                                     # lowercase letters and digits: "wd", "pixai"
    label: str                                   # the model's name, shown in the status and the apps
    categories: tuple[str, ...]                  # the tag categories it answers with (the rating is separate)
    character_categories: tuple[str, ...] = ()   # those of them that the ``character_tags`` setting switches off
    has_rating: bool = True                      # whether it answers with a ``rating`` (probability per rating name)
    default_on: bool = True                      # the default of ``use_<key>``
    noise: frozenset = frozenset()               # its tags that say nothing on their own (normalised); never kept

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9]*", self.key):
            raise ValueError(f"tagger key {self.key!r}: lowercase letters and digits only")
        if not self.categories or not set(self.character_categories) <= set(self.categories):
            raise ValueError(f"tagger {self.key}: character_categories must be among its categories")


# The ordered registry. Everything that used to be hard-wired to two taggers loops over it: detection and merging
# (the highest score wins, ``source`` is the tagger's key), the rating (the mean of the enabled taggers that report
# one), the explicit-tag check, the settings (generated below), the status labels, the preview and the ``models`` the
# panel asks the tagger service for. Tie-breaks go to the earlier entry.
TAGGERS: list[TaggerKind] = [
    TaggerKind("wd", "wd-eva02-large-tagger-v3", ("general", "character"), ("character",)),
    TaggerKind("pixai", "pixai-tagger-v1.0", ("general", "character", "copyright"), ("character", "copyright")),
    # RAM++: plain-English photo tags (objects, food, places, screenshots); no rating, no characters. Chosen in v3
    # after testing on 53 library pictures: it adds the most correct tags on real photos (docs/AI-TAGGER.md).
    TaggerKind("ram", "RAM++ (swin-large)", ("general",), has_rating=False,
               noise=frozenset({"image", "catch", "peak", "miss", "take", "wear", "label"})),
    # Hydra 3.5 (RedRocket, e621 vocabulary: anthro, feral, human on anthro, species...). Fourth since v3: the owner's
    # library has a lot of furry art the Danbooru taggers only call "furry". It votes in the explicit-tag check (on
    # 190 everyday pictures it raised fewer sexual tags than WD or PixAI). "mammal" lands on every human: noise.
    TaggerKind("e621", "Hydra 3.5 (e621)", ("general", "species", "character", "copyright"), ("character", "copyright"),
               has_rating=False, noise=frozenset({"mammal"})),
    # (its key must be the key the tagger service answers /tag with; see docs/AI-TAGGER.md, "Adding a tagger")
]


def enabled_kinds(settings: dict) -> list[TaggerKind]:
    """The taggers switched on, in registry order."""
    return [kind for kind in TAGGERS if settings["use_" + kind.key]]


def model_labels() -> dict[str, str]:
    """``{key: label}`` for the status (every registered tagger, in registry order)."""
    return {kind.key: kind.label for kind in TAGGERS}


# ---------------------------------------------------------------- settings

# What is not a tagger. The per-tagger settings ``use_<key>`` / ``<key>_strictness`` are generated from ``TAGGERS``
# by ``configure_taggers`` (below), together with the tables that mention them.
FIXED_DEFAULTS = {
    "indexing": False, "keep_updated": True, "video_frames": 6, "batch_size": 8, "vram_gb": VRAM_GB_DEFAULT,
    "character_tags": True, "rating_tag": True, "max_tags": 30, "vocabulary": "", "blocked": [], "rules": [],
    "write_tags": False,
}
FIXED_LIMITS = {"video_frames": (1, 8), "batch_size": (1, 64), "vram_gb": VRAM_GB_LIMITS, "max_tags": (5, 100)}
STRICTNESS = (0.2, 0.95)            # the limits of every ``<key>_strictness`` (calibrated: 0.5 = the model's own threshold)
TEXT_LIMITS = {"vocabulary": 20000}
MAX_BLOCKED, MAX_RULES, MAX_RULE_TAGS = 500, 100, 50
RULE_KEYS = ("if_all", "if_any", "unless", "add", "remove")

# Generated from the registry (in place, so a reference to them stays valid when the registry changes).
DEFAULTS: dict = {}
LIMITS: dict = {}
# settings that change what an asset's result looks like (a change makes older results "outdated")
CONTENT: tuple = ()
# the cheapest way to bring results up to date after a change (the strongest of the changed keys wins)
REPROCESS: dict = {}
_SETTINGS_LOCK = threading.Lock()


def configure_taggers(kinds=None) -> None:
    """Make ``kinds`` the registry (or, with None, just rebuild the tables from ``TAGGERS``) and generate from it:
    the ``use_<key>`` / ``<key>_strictness`` settings with ``DEFAULTS``, ``LIMITS``, ``CONTENT`` and ``REPROCESS``
    (``use_*`` need a ``full`` reprocess, ``*_strictness`` a ``retag``). The panel calls this once, at import; tests use
    it to add (and remove) a fake fourth tagger."""
    global CONTENT
    if kinds is not None:
        kinds = list(kinds)
        if len({k.key for k in kinds}) != len(kinds):
            raise ValueError("two taggers with the same key")
        TAGGERS[:] = kinds
    use = [f"use_{kind.key}" for kind in TAGGERS]
    strict = [f"{kind.key}_strictness" for kind in TAGGERS]
    fixed = FIXED_DEFAULTS
    DEFAULTS.clear()
    DEFAULTS.update({k: fixed[k] for k in ("indexing", "keep_updated", "video_frames", "batch_size", "vram_gb")})
    DEFAULTS.update({f"use_{kind.key}": kind.default_on for kind in TAGGERS})
    DEFAULTS.update({key: 0.5 for key in strict})
    DEFAULTS.update({k: fixed[k] for k in ("character_tags", "rating_tag", "max_tags", "vocabulary", "blocked", "rules",
                                           "write_tags")})
    LIMITS.clear()
    LIMITS.update(FIXED_LIMITS)
    LIMITS.update({key: STRICTNESS for key in strict})
    CONTENT = ("video_frames", *use, *strict, "character_tags", "rating_tag", "max_tags", "vocabulary", "blocked",
               "rules", "write_tags")
    REPROCESS.clear()
    REPROCESS.update({"full": ("video_frames", *use),
                      "retag": (*strict, "character_tags", "rating_tag", "max_tags", "vocabulary", "blocked", "rules",
                                "write_tags")})


configure_taggers()


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


def validate_setting(key: str, value, strict: bool = True):
    """The canonical value of a setting, or ValueError. Types are checked explicitly (``"false"`` is not False).
    ``strict=False`` (reading the saved file) lets a vocabulary keep lines this version can't use: they are skipped
    when the tags are made, and the next save names them."""
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
        value = _unix(value)
        if len(value) > TEXT_LIMITS[key]:
            raise ValueError(f"{key} must be at most {TEXT_LIMITS[key]} characters")
        if key == "vocabulary" and strict:
            problems = Vocabulary(value).problems()
            if problems:
                raise ValueError(problems)
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
    """The saved settings. A key this version does not know (the v2 describer's ``describe``, ``instructions``,
    ``language`` and ``vlm_parallel``, v1's ``use_ram``) is ignored, a bad value falls back to the default."""
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
                out[key] = validate_setting(key, value, strict=False)
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
    for mode in ("full", "retag"):
        if any(key in REPROCESS[mode] for key in changed):
            return mode
    return "none"


def normalize_mode(mode) -> str:
    """"retag" or "full". The v2 mode "describe" (a stored queue row, or a request from an old app) counts as
    "retag". ValueError for anything else."""
    mode = LEGACY_MODES.get(mode, mode) if isinstance(mode, str) else mode
    if not isinstance(mode, str) or mode not in RANK:
        raise ValueError("mode must be retag or full")
    return mode


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


def _by_category(kind: TaggerKind, data) -> dict:
    """One capture's stored scores of ``kind`` as ``{category: {tag: score}}``. The v1 service stored RAM++ flat
    (``{tag: score}``, no category level); a tagger with one category answered like that is read as that category,
    so such a row can't be taken for "no tags" (or, with a tag called like a category, crash)."""
    if not isinstance(data, dict):
        return {}
    if len(kind.categories) == 1 and any(isinstance(v, (int, float)) for v in data.values()):
        return {kind.categories[0]: data}
    return data


def _rating_by_kind(entry: dict) -> dict:
    """One capture's stored rating as ``{key: {rating: probability}}``. The v1 service stored WD's alone, flat
    (``{rating: probability}``)."""
    if any(isinstance(v, (int, float)) for v in entry.values()):
        return {"wd": entry}
    return entry


def _merged(data: dict, categories: tuple[str, ...]) -> dict[str, float]:
    """One capture's scores for these categories as one dict (a tag in two categories keeps the higher score)."""
    out: dict[str, float] = {}
    for category in categories:
        for tag, score in (data.get(category) or {}).items():
            out[tag] = max(out.get(tag, 0.0), score)
    return out


def has_kind(raw: dict | None, key: str) -> bool:
    """Whether stored tagger scores include this tagger's. Scores made while it was off, or by an older service
    (v1: RAM++ instead of PixAI), do not."""
    return any((cap or {}).get(key) is not None for cap in (raw or {}).get("scores") or [])


def missing_kinds(raw: dict | None, settings: dict) -> list[str]:
    """The enabled taggers whose scores the stored ``raw`` lacks. Any of them means the asset needs a ``full``
    reprocess: its tags can't be made from what is stored."""
    return [kind.key for kind in enabled_kinds(settings) if not has_kind(raw, kind.key)]


def detect(raw: dict, settings: dict) -> dict:
    """Steps 3-4: stored tagger scores -> tags with scores (before the rules)."""
    vocab = _vocabulary(settings["vocabulary"])
    blocked = set(settings["blocked"])
    candidates: list[tuple[str, float, str]] = []
    display: dict = {kind.key: [] for kind in TAGGERS}
    display["rating"] = {}

    def show(model: str, scores: dict, strictness: float, noise: frozenset = frozenset()) -> None:
        shown = {}
        for tag, score in scores.items():
            name = norm_tag(tag)
            if not name or name in noise:
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
    for kind in enabled_kinds(settings):
        # `character_tags` gates what a tagger names as characters and series (its character categories)
        wanted = tuple(c for c in kind.categories if settings["character_tags"] or c not in kind.character_categories)
        series = [_merged(_by_category(kind, cap[kind.key]), wanted)
                  for cap in caps if cap and cap.get(kind.key) is not None]
        if series:
            show(kind.key, _per_tag(series), settings[kind.key + "_strictness"], kind.noise)
    # The rating: each tagger's probabilities averaged over the captures, then the mean of the enabled taggers that
    # report one (one alone if only one does), then the best. The tag's source is the tagger surest of the winner.
    ratings = [_rating_by_kind(r) for r in raw.get("ratings") or [] if isinstance(r, dict)]
    parts: dict[str, dict[str, float]] = {}
    for kind in enabled_kinds(settings):
        if kind.has_rating:
            per_capture = [r[kind.key] for r in ratings if isinstance(r.get(kind.key), dict) and r[kind.key]]
            if per_capture:
                parts[kind.key] = _mean_scores(per_capture)
    best = None
    if parts:
        names = set().union(*parts.values())
        mean = {name: sum(p.get(name, 0.0) for p in parts.values()) / len(parts) for name in names}
        display["rating"] = {name: round(p, 3) for name, p in mean.items()}
        best = max(sorted(mean), key=lambda name: mean[name])
        if settings["rating_tag"]:
            source = max(parts, key=lambda model: parts[model].get(best, 0.0))       # a tie goes to the earlier tagger
            candidates.append((RATING_PREFIX + best, mean[best], source))
    tags: dict[str, tuple[float, str]] = {}
    kept_by: dict[str, set] = {}
    explicit: set[str] = set()
    for tag, score, source in candidates:
        original = norm_tag(tag)
        if not original:
            continue
        name = vocab.rename(original)
        if original in blocked or name in blocked:
            continue
        kept_by.setdefault(name, set()).add(source)
        if original in EXPLICIT_TAGS or name in EXPLICIT_TAGS:
            explicit.add(name)
        if name not in tags or score > tags[name][0]:
            tags[name] = (score, source)
    # A sexual tag on a picture the combined rating calls general/sensitive must be confirmed by at least two of the
    # enabled taggers (with one tagger on, none can be).
    if best in ("general", "sensitive"):
        doubtful = sorted(n for n in explicit if n in tags and len(kept_by.get(n, ())) < 2)
        for name in doubtful:
            del tags[name]
        if doubtful:
            display["dropped"] = doubtful
    return {"tags": tags, "display": display}


def rule_matches(have: set, rule: dict) -> bool:
    return (all(t in have for t in rule["if_all"])
            and (not rule["if_any"] or any(t in have for t in rule["if_any"]))
            and not any(t in have for t in rule["unless"]))


def apply_rules(tags: dict, rules: list[dict]) -> tuple[dict, list[dict]]:
    """Run the rules in order, again and again until nothing changes (at most ``MAX_RULE_PASSES`` passes). The trace
    names a rule by its position in ``rules`` (the Rules card's), or by its ``label`` when it has one (the
    Vocabulary's combination lines: ``"line 3"``)."""
    tags = dict(tags)
    fired: dict[int, dict] = {}
    for _ in range(MAX_RULE_PASSES):
        changed = False
        for index, rule in enumerate(rules):
            if not rule_matches(set(tags), rule):
                continue
            note = fired.setdefault(index, {"rule": rule.get("label", index), "added": [], "removed": []})
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


def finalize(tags: dict, settings: dict) -> tuple[list[dict], list[dict]]:
    """Step 5: the rules, ``blocked`` and the ``max_tags`` cap. The rules are the Rules card's, then the Vocabulary's
    combination lines in the order written, all in one repeat-until-stable run."""
    blocked = set(settings["blocked"])
    tags = {t: v for t, v in tags.items() if t not in blocked}
    tags, trace = apply_rules(tags, [*settings["rules"], *_vocabulary(settings["vocabulary"]).rules])
    tags = {t: v for t, v in tags.items() if t not in blocked}
    rating = [t for t in tags if t.startswith(RATING_PREFIX)]
    others = sorted((t for t in tags if t not in rating), key=lambda t: (-tags[t][0], t))
    keep = others[:max(settings["max_tags"] - len(rating), 0)] + sorted(rating)
    return [{"tag": t, "score": round(tags[t][0], 3), "source": tags[t][1]} for t in keep], trace


def build(raw: dict, settings: dict) -> dict:
    """Everything the pipeline decides for one asset from the stored scores."""
    found = detect(raw, settings)
    tags, trace = finalize(found["tags"], settings)
    return {"tags": tags, "models": found["display"], "rules": trace, "block": compose_block([t["tag"] for t in tags])}


# ---------------------------------------------------------------- the block in the description

def compose_block(tags: list[str]) -> str:
    """The managed text, ``[AI Tagger]``, ``Tags: a, b, c``, ``[/AI Tagger]`` on three lines; "" when there are no tags."""
    def clean(text: str) -> str:
        return " ".join(str(text).replace(OPEN, "").replace(CLOSE, "").split())

    return "\n".join([OPEN, "Tags: " + ", ".join(clean(t) for t in tags), CLOSE]) if tags else ""


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


# ---------------------------------------------------------------- the model server

def _is_oom(text: str) -> bool:
    return "out of memory" in (text or "").lower()


_GPU_BROKEN = re.compile(r"cuda (failure|error)|cudaerror|cudnn_status|cublas_status|illegal memory access|"
                         r"device-side assert|unspecified launch failure|no cuda-capable device|cuda driver", re.I)


def _is_gpu_broken(text: str) -> bool:
    """A graphics-card runtime failure (not out of memory, which halving handles)."""
    return bool(text) and not _is_oom(text) and bool(_GPU_BROKEN.search(text))


class Tagger:
    """HTTP client of the tagger container (every registered tagger runs in it)."""

    def __init__(self, url: str = TAGGER_URL, timeout: float = 600):
        self.url, self.timeout = url.rstrip("/"), timeout

    def health(self, timeout: float = 3) -> dict | None:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=timeout) as resp:
                return json.loads(resp.read())
        except (OSError, ValueError):
            return None

    def tag(self, images: list[bytes], floor: float = FLOOR, models: list[str] | None = None) -> tuple[list, list]:
        """(one result per picture or None, one error per picture or None). ``models`` are the tagger keys to run (the
        enabled ones); the answer then has only those. Raises GpuOOM / ServiceDown."""
        if len(images) > MAX_IMAGES_PER_REQUEST:
            raise ValueError(f"at most {MAX_IMAGES_PER_REQUEST} pictures per request")
        body = {"images": [base64.b64encode(b).decode() for b in images], "floor": floor}
        if models is not None:
            body["models"] = list(models)
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
        broken = next((e for e in errors if _is_gpu_broken(e)), None)
        if broken:
            raise GpuBroken(f"the tagger lost the graphics card: {broken[:200]}")
        return results, errors


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


TAGGER_ENV_KEYS = ("AITAGGER_VRAM_GB",)      # what a start of the tagger container depends on (a change recreates it)


class Services:
    """The tagger container and the HTTP client that talks to it (what the Indexer and the routes use). Only the
    ``tagger`` compose service is ever started or stopped here. The container stops itself after a quiet spell."""

    def __init__(self, tagger: Tagger | None = None, *, runner=run_command, store: "Store | None" = None,
                 settings_fn=None, clock=time.monotonic, sleep=time.sleep, search_stop=None):
        self.tagger = tagger or Tagger()
        self.runner, self.store, self.clock, self.sleep = runner, store, clock, sleep
        self.settings_fn = settings_fn or load_settings
        self.tagger_box = ComposeService(TAGGER_CONTAINER, TAGGER_SERVICE, runner, clock=clock)
        self.search_box = ComposeService(searchplus.CONTAINER, "", runner, clock=clock)
        self.search_stop = search_stop or (lambda: searchplus.Service().stop())
        self._lock = threading.RLock()
        self._memory: dict[str, str] = {}
        self._gpu: tuple[float, dict] | None = None
        self._total_gb: float | None = None

    # ---- the client
    def tag(self, images: list[bytes], models: list[str] | None = None) -> tuple[list, list]:
        try:
            return self.tagger.tag(images, models=models)
        except GpuBroken:
            self.tagger_box.stop()              # its CUDA context is gone for good: the next round starts a fresh one
            raise

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
        """What the compose file needs: ``vram_gb`` is the taggers' memory cap."""
        settings = settings or self.settings_fn()
        return {"AITAGGER_VRAM_GB": str(int(settings["vram_gb"]))}

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

    def load(self) -> list[str]:
        """Start the tagger container if it is not running (Search+ is stopped first, while
        ``searchplus.AITAGGER_EXCLUSIVE`` says they may not share the card). Returns the names it started. A container
        whose derived env changed since its last start is recreated."""
        with self._lock:
            box = self.tagger_box
            state = box.container_state(fresh=True)
            if state == "running":
                return []
            if searchplus.AITAGGER_EXCLUSIVE and self.search_box.container_state(fresh=True) == "running":
                try:
                    self.search_stop()
                except Exception:  # noqa: BLE001 - not stopping Search+ must not hide the real problem
                    pass
                self.search_box.invalidate()
            env = self.env()
            remembered = self._remembered()
            wanted_env = {k: env[k] for k in TAGGER_ENV_KEYS}
            recreate = state != "missing" and remembered.get(box.container) != wanted_env
            try:
                box.up(env, recreate=recreate)
            except RuntimeError as exc:
                raise ServiceDown(str(exc)) from exc
            remembered[box.container] = wanted_env
            self._remember(remembered)
            return [box.container]

    def ensure_ready(self, need_tagger: bool = True, wait: float = 3600, progress=None,
                     stop: threading.Event | None = None) -> None:
        """Start the tagger if needed and wait until it answers. ServiceDown when it is still loading after ``wait``.
        ``need_tagger`` False (no tagger is switched on) needs nothing."""
        if not need_tagger:
            return
        health = self.tagger.health()
        if health and health.get("status") == "ok":
            return
        self.load()
        started = self.clock()
        deadline = started + wait
        while True:
            if stop is not None and stop.is_set():
                raise ServiceDown("stopped")
            health = self.tagger.health()
            if health and health.get("status") == "error":
                raise RuntimeError(f"the AI Tagger model failed to load: {health.get('error')}")
            if health and health.get("status") == "ok":
                return
            if progress:
                progress("loading the tagger into the graphics card "
                         "(the very first time, the models are downloaded first)")
            if self.clock() >= deadline:
                raise ServiceDown("the AI Tagger models are still loading")
            if self.clock() - started > 30 and self.tagger_box.container_state(fresh=True) == "stopped":
                raise RuntimeError(f"{self.tagger_box.container} stopped while loading; "
                                   f"see `docker logs {self.tagger_box.container}`")
            self.sleep(2)

    def unload(self) -> list[str]:
        """Stop the tagger container (frees the card)."""
        with self._lock:
            return [box.container for box in (self.tagger_box,) if box.stop()]

    def status(self) -> dict:
        state = self.tagger_box.container_state()
        health = self.tagger.health(timeout=1.5) if state == "running" else None
        tagger = {"container": state, "status": "down", "error": None}
        if health:
            tagger.update(status=health.get("status") or "loading", error=health.get("error"))
        elif state == "running":
            tagger["status"] = "loading"
        return {"tagger": tagger, "gpu": self.gpu(),
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
  id text primary key, tags_json text, block text, settings_version integer, processed_at text, written_at text
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
BLOCK_FORMAT = "3"      # meta.block_format; before v3 (no entry) a block could hold a "Description:" line
LEGACY_TAGGERS = ("wd", "pixai")        # the registry as it was before it was remembered (meta.taggers)
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
        """One-time changes when a store made by an older version is opened. Three things make every stored result
        "outdated" (``settings_version`` goes up once, for all of them together, and the fact is remembered in
        ``meta`` so it never happens twice):
        * stored scores made by the v1 service (RAM++, no PixAI entry) cannot give a v2 result (``raw_format``);
        * a stored block that still has v2's ``Description:`` line: a retag rewrites it without (``block_format``);
        * a tagger that is on by default was added to the registry since the store was last opened
          (``meta.taggers``): the results lack its tags.
        The Indexer redoes the assets whose stored scores lack an enabled tagger as ``full``. A queued v2 "describe"
        becomes "retag"."""
        bump = False
        if self.meta("raw_format") != RAW_FORMAT:
            if self.conn.execute("select 1 from raw where models not like '%pixai%' limit 1").fetchone():
                bump = True
            self.set_meta("raw_format", RAW_FORMAT)
        if self.meta("block_format") != BLOCK_FORMAT:
            if self.conn.execute("select 1 from results where instr(block, ?) > 0 limit 1",
                                 ("\nDescription: ",)).fetchone():
                bump = True
            self.set_meta("block_format", BLOCK_FORMAT)
        seen = [k for k in self.meta("taggers").split(",") if k] or list(LEGACY_TAGGERS)
        registered = ",".join(kind.key for kind in TAGGERS)
        if any(kind.default_on and kind.key not in seen for kind in TAGGERS) \
                and self.conn.execute("select 1 from results limit 1").fetchone():
            bump = True
        if self.meta("taggers") != registered:
            self.set_meta("taggers", registered)
        if bump:
            self.bump_settings_version()
        with self.lock, self.conn:
            self.conn.execute("update queue set mode='retag' where mode='describe'")

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
    def save_result(self, asset_id: str, *, tags: list[dict], block: str, version: int, written: bool = False) -> None:
        now = _now()
        with self.lock, self.conn:
            self.conn.execute(
                "insert or replace into results (id, tags_json, block, settings_version, processed_at, written_at)"
                " values (?,?,?,?,?,?)", (asset_id, _dumps(tags), block, version, now, now if written else None))
            self.conn.execute("delete from asset_tags where id=?", (asset_id,))
            self.conn.executemany("insert or ignore into asset_tags values (?,?)", [(asset_id, t["tag"]) for t in tags])
            self._changes += 1

    def result(self, asset_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "select tags_json, block, settings_version, processed_at, written_at from results where id=?",
                (asset_id,)).fetchone()
        if not row:
            return None
        return {"tags": json.loads(row[0]), "block": row[1], "settings_version": row[2], "processed_at": row[3],
                "written_at": row[4]}

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
        mode = normalize_mode(mode)
        ids = list(dict.fromkeys(ids))
        if not ids:
            return 0
        now = _now()
        with self.lock, self.conn:
            self.conn.executemany(
                "insert into queue (id, mode, at) values (?,?,?) on conflict(id) do update set mode=excluded.mode,"
                " at=excluded.at where (case excluded.mode when 'full' then 2 else 1 end)"
                " > (case queue.mode when 'full' then 2 else 1 end)",
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
            if row and RANK[normalize_mode(row[0])] <= RANK[normalize_mode(done_mode)]:
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
            where.append("(lower(a.name) like ? escape '\\' or exists"
                         " (select 1 from asset_tags t where t.id=a.id and t.tag like ? escape '\\'))")
            args += [like, like]
        if outdated:
            where.append("r.settings_version < ?")
            args.append(self.settings_version)
        clause = " and ".join(where)
        join = "from assets a join results r on r.id=a.id"
        with self.lock:
            total = self.conn.execute(f"select count(*) {join} where {clause}", args).fetchone()[0]
            rows = self.conn.execute(
                f"select a.id, a.name, a.type, a.taken, r.tags_json, r.settings_version, r.processed_at"
                f" {join} where {clause} order by a.taken desc, a.id limit ? offset ?",
                (*args, size, (page - 1) * size)).fetchall()
        items = [{"id": r[0], "name": r[1], "type": r[2], "taken": r[3], "tags": [t["tag"] for t in json.loads(r[4])],
                  "settingsVersion": r[5], "processedAt": r[6]} for r in rows]
        return {"items": items, "total": total, "page": page, "tags": self.top_tags()}


# ---------------------------------------------------------------- one asset, start to finish

def synthetic_raw(captures: int) -> dict:
    """Stand-in for tagger scores when no tagger is used (then there are no tags either)."""
    return {"captures": captures, "scores": [{kind.key: None for kind in TAGGERS}] * captures,
            "ratings": [None] * captures, "models": []}


def raw_from_results(results: list, errors: list) -> dict | str:
    """The tagger's answers for one asset's captures as a stored ``raw`` payload, or the error text.

    ``scores[i]`` holds capture i's calibrated tag scores per tagger (``{key: {category: {tag: score}} | None}``, the
    categories of its registry entry; ``None`` when it did not answer), ``ratings[i]`` its raw rating probabilities as
    ``{key: {rating: probability} | None}`` (``None`` for a tagger without a rating).
    """
    good = [r for r in results if r]
    if not good:
        return next((e for e in errors if e), "the tagger could not read this picture")
    scores, ratings, models = [], [], set()
    for r in good:
        entry, rating = {}, {}
        for kind in TAGGERS:
            data = r.get(kind.key)
            if data is not None:
                models.add(kind.key)
            by_category = _by_category(kind, data)
            entry[kind.key] = None if data is None else {c: by_category.get(c) or {} for c in kind.categories}
            rating[kind.key] = ((data or {}).get("rating") or None) if kind.has_rating else None
        scores.append(entry)
        ratings.append(rating if any(rating.values()) else None)
    return {"captures": len(good), "scores": scores, "ratings": ratings, "models": sorted(models)}


class Pipeline:
    """What is done to an asset: captures, tagging, tags, write-back. Used by the Indexer and the Test card."""

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
    def tag_batch(self, batch: list[tuple[dict, list[bytes]]], models: list[str] | None = None) -> dict:
        """{asset id: stored-raw payload, or an error text} for assets whose captures go in one request. ``models``
        are the keys of the taggers to run (the enabled ones)."""
        results, errors = self.services.tag([f for _, frames in batch for f in frames], models=models)
        out, i = {}, 0
        for item, frames in batch:
            out[item["id"]] = raw_from_results(results[i:i + len(frames)], errors[i:i + len(frames)])
            i += len(frames)
        return out

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

    def commit(self, item: dict, mode: str, decision: dict, version: int, settings: dict) -> dict:
        """Store the result, write it to Immich (and read it back), mark it written."""
        self.store.save_result(item["id"], tags=decision["tags"], block=decision["block"], version=version)
        written = self.write_back(item["id"], decision["block"], [t["tag"] for t in decision["tags"]], settings)
        self.store.mark_written(item["id"])
        self.store.dequeue(item["id"], mode)
        return written

    # ---- the whole thing for one asset (Indexer)
    def process(self, item: dict, mode: str, raw: dict, settings: dict, version: int) -> dict:
        """Do ``mode`` for one asset (``raw`` is the stored scores, or what the taggers just said). ``mode`` is also
        what the work counts as when taking it off the queue."""
        decision = build(raw, settings)
        written = self.commit(item, mode, decision, version, settings)
        return {"decision": decision, "write": written}

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
        wanted = [kind.key for kind in enabled_kinds(settings)]
        tagger_used = bool(wanted)
        self.services.ensure_ready(need_tagger=tagger_used, wait=PREVIEW_WAIT)
        try:
            frames, _kind = self.frames(item, settings["video_frames"])
        except (OSError, ValueError) as exc:
            raise ValueError(f"Could not read this {item['type'].lower()}: {exc}") from exc
        if tagger_used:
            raw = self.tag_batch([(item, frames)], wanted)[item["id"]]
            if isinstance(raw, str):
                raise ValueError(f"The tagger could not read this picture: {raw}")
        else:
            raw = synthetic_raw(len(frames))
        decision = build(raw, settings)
        current, _asset = self.current(asset_id)
        out = {"id": item["id"], "name": item["name"], "type": item["type"], "captures": len(frames),
               "frames": [data_url(_shrunk(f, PREVIEW_SIDE)) for f in frames],
               "models": decision["models"], "rules": decision["rules"], "tags": decision["tags"],
               "block": decision["block"], "currentDescription": current,
               "newDescription": merge_description(current, decision["block"]), "written": False}
        if write:
            if tagger_used:
                self.store.save_raw(item["id"], raw)
            self.commit(item, "full", decision, version, settings)
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
    """Queue assets for a reprocess; returns how many. The v2 mode "describe" counts as "retag"."""
    mode = normalize_mode(mode)
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
    """Background thread that tags the assets; stop / start at will, it resumes where it was.

    Work order: the queue (asked-for reprocessing) first, then the assets with no written result, newest first.
    Pictures are prepared by ``WORKERS`` threads, at most ``TAG_REQUESTS`` tagger requests are in flight, and
    ``WRITERS`` assets are in the write-back stage (Immich) at once.

    The stages overlap *across rounds*: a round (``batch_size`` assets) hands its batches to the tagger and returns
    without waiting for the answers, so the next round's pictures are read and cut while the GPU is still busy with the
    earlier ones. The requests in flight are state of the indexer (``_tag_futs``), not of the round; a round only
    waits when ``TAG_REQUESTS`` requests are already out. So the GPU has the next batch waiting at every round
    boundary, and at most ``TAG_REQUESTS + 1`` batches of pictures are in memory (the ones on the GPU and the round
    being prepared).
    """

    CATALOG_EVERY = 600         # re-read the library list this often (and look for new photos)
    WORKERS = 6                 # threads reading previews / cutting video frames
    TAG_REQUESTS = 2            # tagger requests in flight
    WRITERS = 8                 # assets being written to Immich (and read back) at once
    IDLE_POLL = 60              # seconds between looks for new work while there is nothing to do
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
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._flight_lock = threading.Lock()
        self._wake = threading.Event()          # cuts the "up to date" wait short (Start, Try again, reprocess)
        self._inflight: set[str] = set()        # claimed assets: being prepared, tagged or written
        self._pending: set = set()              # write-back futures (a failure that matters is raised by _reap)
        self._tag_futs: set = set()             # tagger requests in flight, across rounds (a failure is raised by _reap_tags)
        self._done = 0                          # assets finished in this process (progress, for the drop counter)
        self._prep = self._tagpool = self._writepool = None

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
            self._prep = ThreadPoolExecutor(self.WORKERS, thread_name_prefix="aitagger-prep")
            self._tagpool = ThreadPoolExecutor(self.TAG_REQUESTS, thread_name_prefix="aitagger-tag")
            self._writepool = ThreadPoolExecutor(self.WRITERS, thread_name_prefix="aitagger-write")
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
            for pool in (self._tagpool, self._writepool):
                if pool:
                    pool.shutdown(wait=True, cancel_futures=True)
            if self._prep:                      # one slow video (a huge file) must not hold up pausing
                self._prep.shutdown(wait=False, cancel_futures=True)
            self._prep = self._tagpool = self._writepool = None

    def _step(self, stop: threading.Event) -> str:
        """One look for work and one round of it: "over" when the run is finished, "idle" when there was nothing
        to do, else "busy"."""
        version, settings = self.pipe.snapshot()
        self._reap_tags(self.TAG_REQUESTS)      # a request that failed since the last look: raise it before more is read
        if self.last_sync is None or self.clock() - self.last_sync >= self.CATALOG_EVERY:
            self.state, self.detail = "running", "reading the library list"
            self.refresh_catalog()
        # One round is one batch: the pictures of ``TAG_REQUESTS`` requests are on the GPU side and this round's are
        # being prepared, which is as far ahead as it pays to read (and as much memory as it is worth).
        items = self.store.work(self.batch_size(settings), skip=self._flying())
        if not items:
            if self._tag_futs or self._pending:
                self._reap_tags(0, stop)        # earlier assets are still on the tagger ...
                self._reap(0, stop)             # ... or being written
                return "busy"
            if not settings["keep_updated"]:
                self.state, self.detail = "done", "everything is tagged"
                return "over"
            self.state, self.detail = "done", "everything is tagged; looks for new photos every 10 minutes"
            self._wake.wait(min(self.IDLE_POLL, max(self.CATALOG_EVERY - (self.clock() - self.last_sync), 1)))
            self._wake.clear()
            return "idle"
        self.state, self.detail = "running", "tagging"
        self._round(items, settings, version, stop)
        self._reap(self.WRITERS, stop)          # never more than a pool-full waiting to be written
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
        for item in items:
            self._claim(item["id"])
        try:
            local, captured = [], []
            for item in items:
                mode, raw = normalize_mode(item["mode"]), None
                if mode == "retag":
                    raw = self.store.raw(item["id"])
                    if raw is None or missing_kinds(raw, settings):
                        mode = "full"           # nothing stored to work from, or no scores of an enabled tagger
                item["mode"], item["raw"] = mode, raw
                (captured if mode == "full" else local).append(item)
            # No pictures and no GPU are needed for these: the stored scores are all there is.
            for item in local:
                self._submit_finish(item, settings, version)
            if captured:
                self._capture_and_tag(captured, settings, version, stop)
        finally:
            for item in items:
                if not item.get("_handed"):
                    self._release(item["id"])

    def _capture_and_tag(self, items: list[dict], settings: dict, version: int, stop: threading.Event) -> None:
        """Prepare the pictures of these assets and hand them to the tagger in batches. Returns when the last batch
        is *sent*, not when it is answered: the requests go on (``_tag_futs``) while the next round is prepared."""
        wanted = [kind.key for kind in enabled_kinds(settings)]
        need_tagger = bool(wanted)
        if not self.tag_requests():             # a request on the GPU proves the server answers: no need to ask
            self.state, self.detail = "starting", "waiting for the models"
            self.services.ensure_ready(need_tagger=need_tagger, wait=3600, stop=stop,
                                       progress=lambda d: setattr(self, "detail", d))
        self.state, self.detail = "running", "tagging"
        futures = {self._prep.submit(self.pipe.frames, item, settings["video_frames"]): item for item in items}
        batch: list[tuple[dict, list[bytes]]] = []
        images = 0
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
                if not need_tagger:             # no tagger is on: there are no tags to make
                    item["raw"] = synthetic_raw(len(frames))
                    self._submit_finish(item, settings, version)
                    continue
                if batch and images + len(frames) > MAX_IMAGES_PER_REQUEST:
                    if not self._send(batch, settings, version, stop):
                        break
                    batch, images = [], 0
                batch.append((item, frames))
                images += len(frames)
                item["_handed"] = True
                if len(batch) >= self.batch_size(settings):
                    if not self._send(batch, settings, version, stop):
                        break
                    batch, images = [], 0
            if batch and not stop.is_set():     # the round's last, partial batch: out now, it waits for nothing
                if self._send(batch, settings, version, stop):
                    batch = []
        finally:
            for fut in futures:
                fut.cancel()
            for item, _ in batch:               # a batch never sent (stopped, or a request failed): back to the pool
                self._release(item["id"])

    def tag_requests(self) -> int:
        """Tagger requests in flight: sent and not answered yet."""
        with self._flight_lock:
            return sum(1 for fut in self._tag_futs if not fut.done())

    def _send(self, batch: list, settings: dict, version: int, stop: threading.Event) -> bool:
        """Hand a batch to the tagger and return at once. It waits only while ``TAG_REQUESTS`` requests are already in
        flight (that bounds the pictures held), and raises the failure of a request that finished meanwhile
        (``ServiceDown`` ...); the batch is then not sent and the caller gives its assets back. False: paused while
        waiting, nothing was sent."""
        self._reap_tags(self.TAG_REQUESTS - 1, stop)
        if stop.is_set():
            return False
        with self._flight_lock:                 # (submitted and registered as one step: a request never runs unseen)
            self._tag_futs.add(self._tagpool.submit(self._tag_stage, batch, settings, version))
        return True

    def _reap_tags(self, limit: int, stop: threading.Event | None = None) -> None:
        """Wait until at most ``limit`` tagger requests are in flight; re-raise the first failure among the requests
        that have finished (their assets are already back in the pool of work)."""
        while True:
            with self._flight_lock:
                done = {f for f in self._tag_futs if f.done()}
                self._tag_futs -= done
                waiting = set(self._tag_futs)
            for fut in done:
                fut.result()                    # re-raises ServiceDown
            if len(waiting) <= limit or (stop is not None and stop.is_set()):
                return
            wait(waiting, timeout=0.5, return_when=FIRST_COMPLETED)

    def _tag_stage(self, batch: list, settings: dict, version: int) -> None:
        """One tagger request (runs on a tag thread): tag the batch, store the scores, hand each asset to the
        write-back stage. Whatever goes wrong, the assets that did not get that far are released."""
        settled: set[str] = set()               # failed (and noted) or handed to the write-back stage: not ours any more
        try:
            models = [kind.key for kind in enabled_kinds(settings)]
            results = self._tag_halving(batch, models, int(settings["batch_size"]))
            for item, _frames in batch:
                res = results[item["id"]]
                if isinstance(res, str):
                    self.store.fail(item["id"], res)
                    self._release(item["id"])
                else:
                    self.store.save_raw(item["id"], res)
                    item["raw"] = res
                    self._submit_finish(item, settings, version)
                settled.add(item["id"])
        except BaseException:
            for item, _ in batch:
                if item["id"] not in settled:
                    self._release(item["id"])
            raise

    def _tag_halving(self, batch: list, models: list[str], want: int) -> dict:
        """Tag a batch; when the card is out of memory, halve the batch size (for this session) and retry. ``want`` is
        the ``batch_size`` setting the batch was made under: the cap holds only while the owner keeps that value."""
        try:
            return self.pipe.tag_batch(batch, models)
        except GpuOOM as exc:
            half = max(1, len(batch) // 2)
            self.batch_cap, self._cap_for = half if len(batch) > 1 else 1, want
            if len(batch) == 1:
                return {batch[0][0]["id"]: f"out of graphics memory, even for this one picture ({exc})"[:300]}
            self.detail = f"the graphics card is full: now {half} assets at a time"
            out = self._tag_halving(batch[:half], models, want)
            out.update(self._tag_halving(batch[half:], models, want))
            return out

    # ---- the write-back stage
    def _submit_finish(self, item: dict, settings: dict, version: int) -> None:
        item["_handed"] = True
        fut = self._writepool.submit(self._finish, item, settings, version)
        with self._flight_lock:
            self._pending.add(fut)

    def _finish(self, item: dict, settings: dict, version: int) -> None:
        try:
            self.pipe.process(item, item["mode"], item["raw"], settings, version)
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
        """Wait until at most ``limit`` assets are in the write-back stage; re-raise the first failure that matters."""
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
        """Wait for everything in flight: the tagger requests (each hands its assets to the write-back stage as it
        ends), then the write-back stage. Their outcome is already recorded or doesn't matter: a request that failed
        has given its assets back, and the next round looks at them again."""
        with self._flight_lock:
            tagging = list(self._tag_futs)
        if tagging:
            wait(tagging)
        with self._flight_lock:
            self._tag_futs -= {f for f in self._tag_futs if f.done()}
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


def instance(client=None) -> tuple[Store, Services, Indexer]:
    """The panel's store / tagger container / indexer (created on first use; ``client`` is the Immich client)."""
    with _LOCK:
        key = str(home())
        if key not in _INSTANCE:
            store = Store()
            services = Services(store=store)
            _INSTANCE[key] = (store, services, Indexer(store, services, client=client))
        parts = _INSTANCE[key]
        if client is not None and parts[2].client is None:
            parts[2].client = client
        return parts


def autostart(client=None) -> None:
    """Resume tagging after a panel restart if it was on."""
    try:
        parts = instance(client)
        if load_settings().get("indexing"):
            parts[2].start()
    except Exception:  # noqa: BLE001 - the panel must start regardless
        pass
