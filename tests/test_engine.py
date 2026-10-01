import os
import tempfile
import unittest
from unittest import mock

from immich_organizer.client import ImmichClient, ImmichError
from immich_organizer.engine import (
    album_asset_ids,
    apply_plan,
    build_plan,
    evaluate_rule,
    find_album,
    read_journal,
    undo_run,
)
from immich_organizer.rules import Rule, parse_ruleset
from tests.fake_immich import API_KEY, FakeImmich


class TestFindAlbum(unittest.TestCase):
    def test_exact_match_wins(self):
        albums = [{"id": "1", "albumName": "Cats"}, {"id": "2", "albumName": "cats"}]
        self.assertEqual(find_album(albums, "Cats")["id"], "1")

    def test_case_insensitive_fallback(self):
        albums = [{"id": "2", "albumName": "CATS"}]
        self.assertEqual(find_album(albums, "cats")["id"], "2")

    def test_missing_album_returns_none(self):
        self.assertIsNone(find_album([{"id": "1", "albumName": "Dogs"}], "Cats"))

    def test_ambiguous_exact_duplicates_raise(self):
        albums = [{"id": "1", "albumName": "Cats"}, {"id": "2", "albumName": "Cats"}]
        with self.assertRaises(ImmichError):
            find_album(albums, "Cats")

    def test_ambiguous_case_only_duplicates_raise(self):
        albums = [{"id": "1", "albumName": "CATS"}, {"id": "2", "albumName": "cats"}]
        with self.assertRaises(ImmichError):
            find_album(albums, "Cats")


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich().start()
        self.addCleanup(self.fake.stop)
        self.client = ImmichClient(self.fake.url, API_KEY, retries=0)
        self.ids = [a["id"] for a in self.fake.assets]

        # Keeps journal writes inside a temp dir instead of the real home.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"IMMICH_ORGANIZER_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)


class TestEvaluateRule(EngineCase):
    def test_limit_truncates_the_ranking(self):
        self.fake.smart_results["mountain"] = self.ids
        rule = Rule(name="r", album="A", query="mountain", limit=5)
        self.assertEqual([a["id"] for a in evaluate_rule(self.client, rule)], self.ids[:5])

    def test_all_of_intersects_and_preserves_base_order(self):
        self.fake.smart_results["mountain"] = self.ids[:10]
        self.fake.smart_results["snow"] = [self.ids[7], self.ids[2], self.ids[5]]
        rule = Rule(name="r", album="A", query="mountain", limit=10)
        rule.refine.all_of = ["snow"]
        got = [a["id"] for a in evaluate_rule(self.client, rule)]
        self.assertEqual(got, [self.ids[2], self.ids[5], self.ids[7]])

    def test_none_of_subtracts(self):
        self.fake.smart_results["beach"] = self.ids[:6]
        self.fake.smart_results["swimming pool"] = [self.ids[1], self.ids[4]]
        rule = Rule(name="r", album="A", query="beach", limit=10)
        rule.refine.none_of = ["swimming pool"]
        got = [a["id"] for a in evaluate_rule(self.client, rule)]
        self.assertEqual(got, [self.ids[0], self.ids[2], self.ids[3], self.ids[5]])

    def test_all_of_with_several_terms_requires_every_one(self):
        self.fake.smart_results["base"] = self.ids[:8]
        self.fake.smart_results["a"] = [self.ids[1], self.ids[2], self.ids[3]]
        self.fake.smart_results["b"] = [self.ids[2], self.ids[3], self.ids[9]]
        rule = Rule(name="r", album="A", query="base", limit=10)
        rule.refine.all_of = ["a", "b"]
        got = [a["id"] for a in evaluate_rule(self.client, rule)]
        self.assertEqual(got, [self.ids[2], self.ids[3]])

    def test_refined_results_still_honour_the_limit(self):
        self.fake.smart_results["base"] = self.ids
        self.fake.smart_results["a"] = self.ids
        rule = Rule(name="r", album="A", query="base", limit=3)
        rule.refine.all_of = ["a"]
        self.assertEqual(len(evaluate_rule(self.client, rule)), 3)

    def test_similar_to_asset(self):
        ref = self.ids[0]
        self.fake.similar_results[ref] = self.ids[1:5]
        rule = Rule(name="r", album="A", like_asset=ref, limit=10)
        self.assertEqual([a["id"] for a in evaluate_rule(self.client, rule)], self.ids[1:5])

    def test_filters_are_passed_through_to_the_server(self):
        self.fake.smart_results["stuff"] = self.ids
        rule = Rule(name="r", album="A", query="stuff", limit=50, filters={"type": "VIDEO"})
        got = evaluate_rule(self.client, rule)
        self.assertTrue(got)
        self.assertTrue(all(a["type"] == "VIDEO" for a in got))


class TestPlanAndApply(EngineCase):
    def _ruleset(self, **rule):
        base = {"album": "Mountains", "query": "mountain", "limit": 5}
        base.update(rule)
        return parse_ruleset({"version": 1, "rules": [base]})

    def test_plan_changes_nothing(self):
        self.fake.smart_results["mountain"] = self.ids
        plan = build_plan(self.client, self._ruleset())
        self.assertEqual(plan.total_to_add, 5)
        self.assertEqual(plan.albums_to_create(), ["Mountains"])
        self.assertEqual(self.fake.albums, {})

    def test_apply_creates_the_album_and_files_assets(self):
        self.fake.smart_results["mountain"] = self.ids
        plan = build_plan(self.client, self._ruleset())
        result = apply_plan(self.client, plan)

        self.assertEqual(result.total_added, 5)
        self.assertEqual(result.created_albums, ["Mountains"])
        album = self.fake.album_by_name("Mountains")
        self.assertEqual(self.fake.album_members[album["id"]], self.ids[:5])

    def test_second_run_is_idempotent(self):
        self.fake.smart_results["mountain"] = self.ids
        apply_plan(self.client, build_plan(self.client, self._ruleset()))

        plan = build_plan(self.client, self._ruleset())
        self.assertEqual(plan.total_to_add, 0)
        self.assertEqual(len(plan.entries[0].already_in_album), 5)
        result = apply_plan(self.client, plan)
        self.assertEqual(result.total_added, 0)

    def test_new_photos_get_picked_up_on_a_later_run(self):
        self.fake.smart_results["mountain"] = self.ids[:3]
        apply_plan(self.client, build_plan(self.client, self._ruleset()))
        # A later upload also matches the query.
        self.fake.smart_results["mountain"] = self.ids[:5]
        plan = build_plan(self.client, self._ruleset())
        self.assertEqual(plan.total_to_add, 2)

    def test_existing_album_membership_is_detected(self):
        album_id = self.fake.add_album("Mountains", members=self.ids[:2])
        self.fake.smart_results["mountain"] = self.ids
        plan = build_plan(self.client, self._ruleset())
        entry = plan.entries[0]
        self.assertTrue(entry.album_exists)
        self.assertEqual(entry.album_id, album_id)
        self.assertEqual(len(entry.already_in_album), 2)
        self.assertEqual(len(entry.to_add), 3)

    def test_create_album_false_reports_a_rule_error(self):
        self.fake.smart_results["mountain"] = self.ids
        plan = build_plan(self.client, self._ruleset(create_album=False))
        self.assertEqual(len(plan.errors), 1)
        self.assertIn("does not exist", plan.entries[0].error)
        # A failed rule contributes nothing to apply.
        self.assertEqual(apply_plan(self.client, plan).total_added, 0)

    def test_actions_apply_only_to_newly_added_assets(self):
        self.fake.smart_results["mountain"] = self.ids
        rs = self._ruleset(actions={"archive": True, "favorite": True})
        apply_plan(self.client, build_plan(self.client, rs))
        kinds = [set(u) - {"ids"} for u in self.fake.updates]
        self.assertIn({"isFavorite"}, kinds)
        self.assertIn({"visibility"}, kinds)

        self.fake.updates.clear()
        apply_plan(self.client, build_plan(self.client, rs))
        self.assertEqual(self.fake.updates, [])

    def test_only_filter_selects_a_single_rule(self):
        self.fake.smart_results["a"] = self.ids[:3]
        self.fake.smart_results["b"] = self.ids[3:6]
        rs = parse_ruleset({"version": 1, "rules": [
            {"name": "ra", "album": "A", "query": "a"},
            {"name": "rb", "album": "B", "query": "b"},
        ]})
        plan = build_plan(self.client, rs, only=["rb"])
        self.assertEqual([e.rule.name for e in plan.entries], ["rb"])

    def test_unknown_rule_name_is_rejected(self):
        rs = self._ruleset()
        with self.assertRaises(ImmichError):
            build_plan(self.client, rs, only=["nope"])

    def test_disabled_rules_are_skipped(self):
        self.fake.smart_results["mountain"] = self.ids
        rs = self._ruleset(enabled=False)
        with self.assertRaises(ImmichError):
            build_plan(self.client, rs)


class TestJournalAndUndo(EngineCase):
    def test_undo_removes_exactly_what_was_added(self):
        # An asset already in the album must survive the undo.
        album_id = self.fake.add_album("Mountains", members=[self.ids[0]])
        self.fake.smart_results["mountain"] = self.ids
        rs = parse_ruleset({"version": 1, "rules": [
            {"album": "Mountains", "query": "mountain", "limit": 4}
        ]})
        result = apply_plan(self.client, build_plan(self.client, rs))
        self.assertEqual(result.total_added, 3)
        self.assertEqual(len(self.fake.album_members[album_id]), 4)

        record = read_journal()[-1]
        removed, failures = undo_run(self.client, record)
        self.assertEqual(removed, 3)
        self.assertEqual(failures, [])
        self.assertEqual(self.fake.album_members[album_id], [self.ids[0]])

    def test_journal_records_the_run(self):
        self.fake.smart_results["mountain"] = self.ids
        rs = parse_ruleset({"version": 1, "rules": [{"album": "M", "query": "mountain", "limit": 2}]})
        result = apply_plan(self.client, build_plan(self.client, rs))
        record = read_journal()[-1]
        self.assertEqual(record["run_id"], result.run_id)
        self.assertEqual(record["added"]["M"], self.ids[:2])
        self.assertIn("M", record["album_ids"])

    def test_nothing_is_journalled_when_nothing_changed(self):
        self.fake.add_album("M", members=self.ids[:2])
        self.fake.smart_results["mountain"] = self.ids[:2]
        rs = parse_ruleset({"version": 1, "rules": [{"album": "M", "query": "mountain", "limit": 2}]})
        apply_plan(self.client, build_plan(self.client, rs))
        self.assertEqual(read_journal(), [])


class TestAlbumMembership(EngineCase):
    def test_membership_paginates_past_one_page(self):
        album_id = self.fake.add_album("Big", members=self.ids)
        got = album_asset_ids(self.client, album_id, page_size=7)
        self.assertEqual(got, set(self.ids))


if __name__ == "__main__":
    unittest.main()
