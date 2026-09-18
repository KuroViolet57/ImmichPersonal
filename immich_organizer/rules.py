"""Parsing and validation of the rules file.

A rules file describes *what to find* and *which album it belongs in*. It is
deliberately declarative so the same file can be previewed, applied, and later
re-run on a schedule to catch newly uploaded photos.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Immich rejects a page size above 1000; a single rule pulling more than this
# is almost always a mistake rather than an intent.
MAX_LIMIT = 5000
DEFAULT_LIMIT = 200
# How deep each refinement search goes when narrowing a rule's matches.
DEFAULT_REFINE_POOL = 600

ASSET_TYPES = {"IMAGE", "VIDEO", "AUDIO", "OTHER"}
VISIBILITIES = {"timeline", "archive", "hidden", "locked"}

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_RELATIVE_RE = re.compile(r"^-(\d+)([dwmy])$", re.I)
_RELATIVE_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


class RuleError(ValueError):
    """A rules file is malformed. The message names the offending field."""


@dataclass
class Refinement:
    """Extra smart searches used to sharpen a rule's result set.

    Immich returns no similarity score, so precision comes from intersecting
    several independent searches rather than from a confidence threshold.
    """

    all_of: list[str] = field(default_factory=list)
    none_of: list[str] = field(default_factory=list)
    pool: int = 600


@dataclass
class Actions:
    archive: bool = False
    favorite: bool = False


@dataclass
class Rule:
    name: str
    album: str
    query: str | None = None
    like_asset: str | None = None
    limit: int = DEFAULT_LIMIT
    filters: dict[str, Any] = field(default_factory=dict)
    refine: Refinement = field(default_factory=Refinement)
    actions: Actions = field(default_factory=Actions)
    create_album: bool = True
    enabled: bool = True

    def describe_match(self) -> str:
        if self.query:
            return f'text "{self.query}"'
        return f"similar to asset {self.like_asset}"

    def search_payload(self) -> dict[str, Any]:
        """Build the ``SmartSearchDto`` body for this rule."""
        payload: dict[str, Any] = dict(self.filters)
        if self.query:
            payload["query"] = self.query
        if self.like_asset:
            payload["queryAssetId"] = self.like_asset
        return payload


@dataclass
class RuleSet:
    rules: list[Rule]
    source: Path | None = None

    def enabled_rules(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]


# --------------------------------------------------------------------- helpers


def _fail(where: str, message: str) -> None:
    raise RuleError(f"{where}: {message}")


def normalise_date(value: Any, where: str) -> str:
    """Accept ``YYYY-MM-DD``, a full ISO timestamp, a YAML date, or ``-30d``.

    Relative offsets make recurring rules ("anything from the last 6 months")
    possible without rewriting the file each run.
    """
    if isinstance(value, dt.datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date):
        return dt.datetime(
            value.year, value.month, value.day, tzinfo=dt.timezone.utc
        ).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str):
        _fail(where, f"expected a date, got {type(value).__name__}")

    text = value.strip()
    rel = _RELATIVE_RE.match(text)
    if rel:
        amount = int(rel.group(1)) * _RELATIVE_DAYS[rel.group(2).lower()]
        moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=amount)
        return moment.isoformat().replace("+00:00", "Z")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail(
            where,
            f"{text!r} is not a date. Use YYYY-MM-DD, a full ISO timestamp, "
            "or a relative offset like -30d / -6m / -1y.",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _as_id_list(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        _fail(where, "expected a list of UUIDs")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not _UUID_RE.match(item.strip()):
            _fail(f"{where}[{i}]", f"{item!r} is not a UUID")
        out.append(item.strip())
    return out


def _as_str_list(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        _fail(where, "expected a string or a list of strings")
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _fail(f"{where}[{i}]", "expected a non-empty string")
        out.append(item.strip())
    return out


# Maps friendly rule keys onto the SmartSearchDto field names Immich expects.
_FILTER_MAP = {
    "taken_after": "takenAfter",
    "taken_before": "takenBefore",
    "created_after": "createdAfter",
    "created_before": "createdBefore",
    "only_unfiled": "isNotInAlbum",
    "favorite": "isFavorite",
    "person_ids": "personIds",
    "album_ids": "albumIds",
    "tag_ids": "tagIds",
    "library_id": "libraryId",
    "city": "city",
    "state": "state",
    "country": "country",
    "make": "make",
    "model": "model",
    "lens_model": "lensModel",
    "rating": "rating",
    "type": "type",
    "visibility": "visibility",
    "language": "language",
}
_DATE_KEYS = {"taken_after", "taken_before", "created_after", "created_before"}
_BOOL_KEYS = {"only_unfiled", "favorite"}
_ID_LIST_KEYS = {"person_ids", "album_ids", "tag_ids"}


def parse_filters(raw: Any, where: str) -> dict[str, Any]:
    """Translate the friendly filter block into SmartSearchDto fields."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail(where, "expected a mapping of filters")

    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _FILTER_MAP:
            known = ", ".join(sorted(_FILTER_MAP))
            _fail(f"{where}.{key}", f"unknown filter. Known filters: {known}")
        if value is None:
            continue
        api_key = _FILTER_MAP[key]
        field_where = f"{where}.{key}"

        if key in _DATE_KEYS:
            out[api_key] = normalise_date(value, field_where)
        elif key in _BOOL_KEYS:
            if not isinstance(value, bool):
                _fail(field_where, "expected true or false")
            out[api_key] = value
        elif key in _ID_LIST_KEYS:
            out[api_key] = _as_id_list(value, field_where)
        elif key == "library_id":
            out[api_key] = _as_id_list(value, field_where)[0]
        elif key == "type":
            text = str(value).upper()
            if text not in ASSET_TYPES:
                _fail(field_where, f"expected one of {sorted(ASSET_TYPES)}")
            out[api_key] = text
        elif key == "visibility":
            text = str(value).lower()
            if text not in VISIBILITIES:
                _fail(field_where, f"expected one of {sorted(VISIBILITIES)}")
            out[api_key] = text
        elif key == "rating":
            if not isinstance(value, int) or isinstance(value, bool) or not -1 <= value <= 5:
                _fail(field_where, "expected an integer between -1 and 5")
            out[api_key] = value
        else:
            if not isinstance(value, str):
                _fail(field_where, "expected a string")
            out[api_key] = value
    return out


def _parse_limit(value: Any, where: str, fallback: int) -> int:
    if value is None:
        return fallback
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(where, "expected a whole number")
    if value < 1:
        _fail(where, "must be at least 1")
    if value > MAX_LIMIT:
        _fail(
            where,
            f"{value} exceeds the {MAX_LIMIT} cap. Smart search always returns "
            "results whether or not they match, so a very large limit files junk.",
        )
    return value


def parse_rule(raw: Any, index: int, defaults: dict[str, Any]) -> Rule:
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        _fail(where, "expected a mapping")

    allowed = {
        "name", "album", "query", "like_asset", "limit", "filters",
        "refine", "actions", "create_album", "enabled",
    }
    unknown = set(raw) - allowed
    if unknown:
        _fail(where, f"unknown key(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(sorted(allowed))}")

    album = raw.get("album")
    if not isinstance(album, str) or not album.strip():
        _fail(f"{where}.album", "every rule needs a target album name")
    name = raw.get("name") or album
    if not isinstance(name, str) or not name.strip():
        _fail(f"{where}.name", "expected a non-empty string")

    query = raw.get("query")
    like_asset = raw.get("like_asset")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        _fail(f"{where}.query", "expected a non-empty search phrase")
    if like_asset is not None:
        if not isinstance(like_asset, str) or not _UUID_RE.match(like_asset.strip()):
            _fail(
                f"{where}.like_asset",
                "expected an asset UUID. Open the photo in Immich and copy the "
                "id from the URL, or use `immich-organizer search` to find one.",
            )
        like_asset = like_asset.strip()
    if bool(query) == bool(like_asset):
        _fail(
            where,
            "set exactly one of `query` (natural language) or `like_asset` "
            "(an asset UUID to match against)",
        )

    filters = dict(defaults.get("filters") or {})
    filters.update(parse_filters(raw.get("filters"), f"{where}.filters"))

    refine_raw = raw.get("refine") or {}
    if not isinstance(refine_raw, dict):
        _fail(f"{where}.refine", "expected a mapping")
    unknown_refine = set(refine_raw) - {"all_of", "none_of", "pool"}
    if unknown_refine:
        _fail(f"{where}.refine", f"unknown key(s): {', '.join(sorted(unknown_refine))}")
    refine = Refinement(
        all_of=_as_str_list(refine_raw.get("all_of", []), f"{where}.refine.all_of"),
        none_of=_as_str_list(refine_raw.get("none_of", []), f"{where}.refine.none_of"),
        pool=_parse_limit(
            refine_raw.get("pool"),
            f"{where}.refine.pool",
            int(defaults.get("refine_pool", DEFAULT_REFINE_POOL)),
        ),
    )

    actions_raw = raw.get("actions") or {}
    if not isinstance(actions_raw, dict):
        _fail(f"{where}.actions", "expected a mapping")
    unknown_actions = set(actions_raw) - {"archive", "favorite"}
    if unknown_actions:
        _fail(f"{where}.actions", f"unknown key(s): {', '.join(sorted(unknown_actions))}")
    for key in ("archive", "favorite"):
        if key in actions_raw and not isinstance(actions_raw[key], bool):
            _fail(f"{where}.actions.{key}", "expected true or false")
    actions = Actions(
        archive=bool(actions_raw.get("archive", defaults.get("archive", False))),
        favorite=bool(actions_raw.get("favorite", defaults.get("favorite", False))),
    )

    create_album = raw.get("create_album", defaults.get("create_album", True))
    if not isinstance(create_album, bool):
        _fail(f"{where}.create_album", "expected true or false")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        _fail(f"{where}.enabled", "expected true or false")

    return Rule(
        name=name.strip(),
        album=album.strip(),
        query=query.strip() if isinstance(query, str) else None,
        like_asset=like_asset,
        limit=_parse_limit(raw.get("limit"), f"{where}.limit", int(defaults.get("limit", DEFAULT_LIMIT))),
        filters=filters,
        refine=refine,
        actions=actions,
        create_album=create_album,
        enabled=enabled,
    )


def parse_ruleset(data: Any, source: Path | None = None) -> RuleSet:
    if not isinstance(data, dict):
        raise RuleError("the rules file must be a mapping with a `rules:` list at the top level")

    unknown = set(data) - {"version", "defaults", "rules"}
    if unknown:
        raise RuleError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")

    version = data.get("version", 1)
    if version != 1:
        raise RuleError(f"unsupported rules version {version!r}; this build understands version 1")

    defaults_raw = data.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise RuleError("defaults: expected a mapping")
    allowed_defaults = {"limit", "filters", "create_album", "archive", "favorite", "refine_pool"}
    unknown_defaults = set(defaults_raw) - allowed_defaults
    if unknown_defaults:
        raise RuleError(
            f"defaults: unknown key(s): {', '.join(sorted(unknown_defaults))}. "
            f"Allowed: {', '.join(sorted(allowed_defaults))}"
        )
    defaults = dict(defaults_raw)
    defaults["limit"] = _parse_limit(defaults_raw.get("limit"), "defaults.limit", DEFAULT_LIMIT)
    defaults["filters"] = parse_filters(defaults_raw.get("filters"), "defaults.filters")
    defaults["refine_pool"] = _parse_limit(
        defaults_raw.get("refine_pool"), "defaults.refine_pool", DEFAULT_REFINE_POOL
    )

    rules_raw = data.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise RuleError("rules: expected a non-empty list of rules")

    rules = [parse_rule(raw, i, defaults) for i, raw in enumerate(rules_raw)]

    seen: dict[str, int] = {}
    for i, rule in enumerate(rules):
        if rule.name in seen:
            raise RuleError(
                f"rules[{i}].name: duplicate rule name {rule.name!r} "
                f"(already used by rules[{seen[rule.name]}]); names must be unique"
            )
        seen[rule.name] = i
    return RuleSet(rules=rules, source=source)


def load_rules(path: str | Path) -> RuleSet:
    """Load a ``.yaml``/``.yml``/``.json`` rules file."""
    path = Path(path).expanduser()
    if not path.exists():
        raise RuleError(f"rules file not found: {path}")
    text = path.read_text("utf-8")

    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise RuleError(
                "Reading YAML rules needs PyYAML. Install it with "
                "`pip install pyyaml`, or write the rules as .json instead."
            ) from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RuleError(f"{path} is not valid YAML: {exc}") from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuleError(f"{path} is not valid JSON: {exc}") from exc

    return parse_ruleset(data, source=path)
