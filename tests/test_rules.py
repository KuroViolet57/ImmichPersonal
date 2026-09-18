import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from immich_organizer.rules import (
    RuleError,
    load_rules,
    normalise_date,
    parse_filters,
    parse_ruleset,
)

UUID_A = "00000000-0000-4000-8000-000000000001"


def ruleset(**overrides):
    base = {
        "version": 1,
        "rules": [{"name": "Mountains", "album": "Mountains", "query": "a mountain"}],
    }
    base.update(overrides)
    return base


class TestDates(unittest.TestCase):
    def test_plain_date(self):
        self.assertEqual(normalise_date("2021-05-04", "x"), "2021-05-04T00:00:00Z")

    def test_yaml_date_object(self):
        self.assertEqual(normalise_date(dt.date(2021, 5, 4), "x"), "2021-05-04T00:00:00Z")

    def test_iso_timestamp_keeps_time(self):
        self.assertEqual(normalise_date("2021-05-04T08:30:00Z", "x"), "2021-05-04T08:30:00Z")

    def test_relative_offset(self):
        got = dt.datetime.fromisoformat(normalise_date("-30d", "x").replace("Z", "+00:00"))
        delta = dt.datetime.now(dt.timezone.utc) - got
        self.assertAlmostEqual(delta.total_seconds(), 30 * 86400, delta=120)

    def test_bad_date_names_the_field(self):
        with self.assertRaises(RuleError) as ctx:
            normalise_date("last tuesday", "rules[0].filters.taken_after")
        self.assertIn("rules[0].filters.taken_after", str(ctx.exception))


class TestFilters(unittest.TestCase):
    def test_friendly_names_map_to_api_fields(self):
        got = parse_filters(
            {"taken_after": "2020-01-01", "only_unfiled": True, "type": "image"}, "f"
        )
        self.assertEqual(
            got, {"takenAfter": "2020-01-01T00:00:00Z", "isNotInAlbum": True, "type": "IMAGE"}
        )

    def test_unknown_filter_is_rejected_with_suggestions(self):
        with self.assertRaises(RuleError) as ctx:
            parse_filters({"taken_at": "2020-01-01"}, "f")
        self.assertIn("unknown filter", str(ctx.exception))
        self.assertIn("taken_after", str(ctx.exception))

    def test_rating_range_is_enforced(self):
        with self.assertRaises(RuleError):
            parse_filters({"rating": 9}, "f")

    def test_person_ids_must_be_uuids(self):
        with self.assertRaises(RuleError):
            parse_filters({"person_ids": ["not-a-uuid"]}, "f")
        self.assertEqual(parse_filters({"person_ids": UUID_A}, "f"), {"personIds": [UUID_A]})


class TestRuleParsing(unittest.TestCase):
    def test_minimal_rule(self):
        rules = parse_ruleset(ruleset()).rules
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].album, "Mountains")
        self.assertEqual(rules[0].search_payload(), {"query": "a mountain"})

    def test_like_asset_rule(self):
        data = ruleset(rules=[{"album": "Cats", "like_asset": UUID_A}])
        rule = parse_ruleset(data).rules[0]
        self.assertEqual(rule.search_payload(), {"queryAssetId": UUID_A})
        self.assertEqual(rule.name, "Cats")  # falls back to the album name

    def test_query_and_like_asset_are_mutually_exclusive(self):
        data = ruleset(rules=[{"album": "X", "query": "a", "like_asset": UUID_A}])
        with self.assertRaises(RuleError) as ctx:
            parse_ruleset(data)
        self.assertIn("exactly one", str(ctx.exception))

    def test_a_rule_needs_one_of_them(self):
        with self.assertRaises(RuleError):
            parse_ruleset(ruleset(rules=[{"album": "X"}]))

    def test_album_is_required(self):
        with self.assertRaises(RuleError) as ctx:
            parse_ruleset(ruleset(rules=[{"query": "a"}]))
        self.assertIn("album", str(ctx.exception))

    def test_defaults_merge_into_rules_but_rules_win(self):
        data = ruleset(
            defaults={"limit": 10, "filters": {"type": "IMAGE", "only_unfiled": True}},
            rules=[
                {"album": "A", "query": "a"},
                {"album": "B", "query": "b", "limit": 99, "filters": {"type": "VIDEO"}},
            ],
        )
        rules = parse_ruleset(data).rules
        self.assertEqual(rules[0].limit, 10)
        self.assertEqual(rules[0].filters["type"], "IMAGE")
        self.assertEqual(rules[1].limit, 99)
        self.assertEqual(rules[1].filters["type"], "VIDEO")
        # A default the rule did not override survives.
        self.assertTrue(rules[1].filters["isNotInAlbum"])

    def test_limit_cap_is_explained(self):
        with self.assertRaises(RuleError) as ctx:
            parse_ruleset(ruleset(rules=[{"album": "A", "query": "a", "limit": 999999}]))
        self.assertIn("cap", str(ctx.exception))

    def test_duplicate_rule_names_rejected(self):
        data = ruleset(rules=[
            {"name": "same", "album": "A", "query": "a"},
            {"name": "same", "album": "B", "query": "b"},
        ])
        with self.assertRaises(RuleError) as ctx:
            parse_ruleset(data)
        self.assertIn("duplicate rule name", str(ctx.exception))

    def test_typo_in_key_is_caught(self):
        with self.assertRaises(RuleError) as ctx:
            parse_ruleset(ruleset(rules=[{"album": "A", "querty": "a"}]))
        self.assertIn("querty", str(ctx.exception))

    def test_refine_and_actions(self):
        data = ruleset(rules=[{
            "album": "Beach", "query": "beach",
            "refine": {"all_of": ["sand"], "none_of": "swimming pool", "pool": 300},
            "actions": {"archive": True},
        }])
        rule = parse_ruleset(data).rules[0]
        self.assertEqual(rule.refine.all_of, ["sand"])
        self.assertEqual(rule.refine.none_of, ["swimming pool"])
        self.assertEqual(rule.refine.pool, 300)
        self.assertTrue(rule.actions.archive)
        self.assertFalse(rule.actions.favorite)

    def test_refine_pool_default_comes_from_defaults(self):
        data = ruleset(
            defaults={"refine_pool": 111},
            rules=[{"album": "A", "query": "a", "refine": {"all_of": ["b"]}}],
        )
        self.assertEqual(parse_ruleset(data).rules[0].refine.pool, 111)

    def test_unsupported_version(self):
        with self.assertRaises(RuleError):
            parse_ruleset(ruleset(version=2))

    def test_enabled_rules_filter(self):
        data = ruleset(rules=[
            {"name": "on", "album": "A", "query": "a"},
            {"name": "off", "album": "B", "query": "b", "enabled": False},
        ])
        rs = parse_ruleset(data)
        self.assertEqual([r.name for r in rs.enabled_rules()], ["on"])


class TestLoading(unittest.TestCase):
    def test_json_rules_load_without_pyyaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(ruleset()))
            self.assertEqual(len(load_rules(path).rules), 1)

    def test_missing_file(self):
        with self.assertRaises(RuleError):
            load_rules("/nonexistent/rules.yaml")


if __name__ == "__main__":
    unittest.main()
