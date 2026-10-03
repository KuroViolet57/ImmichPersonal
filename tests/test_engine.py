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
    looking_at,
    read_journal,
    undo_run,
)
from immich_organizer.rules import Refinement, Rule, parse_ruleset
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


class TestEvaluateRuleOnlyTheseAssets(EngineCase):
    """``only_ids``: the AI tag / description filter. Immich cannot rank just those, so its ranking is read page by
    page and the wanted assets are picked out, up to ``limit`` of them or ``scan_cap`` results looked at."""

    def setUp(self):
        super().setUp()
        self.fake.max_page = 5                      # so the ranking spans several pages
        self.fake.smart_results["mountain"] = self.ids[:30]       # rank 0 .. 29

    def smart_requests(self):
        return [b for kind, b in self.fake.searches if kind == "smart"]

    def run_rule(self, only, *, limit=10, cap=None, exclude=None, **rule_kw):
        rule = Rule(name="r", album="A", query="mountain", limit=limit, **rule_kw)
        stats = {}
        got = evaluate_rule(self.client, rule, exclude_ids=exclude, only_ids=set(only), scan_cap=cap, stats=stats)
        return [a["id"] for a in got], stats

    def test_the_wanted_assets_come_out_in_ranking_order_and_the_rest_are_dropped(self):
        wanted = [self.ids[i] for i in (21, 3, 8, 14)]             # a set: no order of its own
        got, stats = self.run_rule(wanted, limit=4, cap=100)
        self.assertEqual(got, [self.ids[3], self.ids[8], self.ids[14], self.ids[21]])
        self.assertEqual(stats, {"scanned": 22, "capped": False})  # read down to rank 21 (the fourth), no further
        got, stats = self.run_rule(wanted, limit=10, cap=100)      # fewer than the limit exist: the whole ranking is read
        self.assertEqual(len(got), 4)
        self.assertEqual(stats, {"scanned": 30, "capped": False})

    def test_it_stops_reading_pages_once_limit_of_them_are_found(self):
        got, stats = self.run_rule([self.ids[1], self.ids[2], self.ids[20]], limit=2, cap=100)
        self.assertEqual(got, [self.ids[1], self.ids[2]])
        self.assertEqual(len(self.smart_requests()), 1)            # the first page of five had both
        self.assertEqual(stats["scanned"], 3)

    def test_a_candidate_on_a_later_page_takes_more_pages(self):
        got, stats = self.run_rule([self.ids[12]], limit=1, cap=100)
        self.assertEqual(got, [self.ids[12]])
        self.assertEqual([b["page"] for b in self.smart_requests()], [1, 2, 3])        # five a page: ranks 0-4, 5-9, 10-14
        self.assertEqual(stats, {"scanned": 13, "capped": False})

    def test_the_scan_cap_stops_it_and_says_so(self):
        got, stats = self.run_rule([self.ids[12], self.ids[3]], cap=10)
        self.assertEqual(got, [self.ids[3]])                       # rank 12 is beyond the ten results looked at
        self.assertEqual(stats, {"scanned": 10, "capped": True})
        self.assertEqual(len(self.smart_requests()), 3)                                  # pages of 5, 5, then the 11th result

    def test_a_ranking_shorter_than_the_cap_is_not_capped(self):
        self.fake.smart_results["short"] = self.ids[:10]
        rule = Rule(name="r", album="A", query="short", limit=10)
        stats = {}
        got = evaluate_rule(self.client, rule, only_ids={self.ids[9], self.ids[30]}, scan_cap=10, stats=stats)
        self.assertEqual([a["id"] for a in got], [self.ids[9]])
        self.assertEqual(stats, {"scanned": 10, "capped": False})  # exactly ten results exist: nothing more was left

    def test_nothing_in_the_ranking_matches(self):
        got, stats = self.run_rule([self.ids[35]], cap=100)
        self.assertEqual((got, stats), ([], {"scanned": 30, "capped": False}))

    def test_excluded_assets_count_as_looked_at_but_not_towards_the_limit(self):
        wanted = [self.ids[i] for i in (2, 4, 6, 8)]
        got, stats = self.run_rule(wanted, limit=2, cap=100, exclude={self.ids[2], self.ids[4]})
        self.assertEqual(got, [self.ids[6], self.ids[8]])
        self.assertEqual(stats["scanned"], 9)                       # ranks 0 .. 8 all went by

    def test_refine_words_are_applied_to_the_candidates(self):
        self.fake.smart_results["snow"] = [self.ids[5], self.ids[9], self.ids[11]]
        got, _ = self.run_rule([self.ids[1], self.ids[9], self.ids[11], self.ids[20]], cap=100,
                               refine=Refinement(all_of=["snow"]))
        self.assertEqual(got, [self.ids[9], self.ids[11]])

    def test_without_a_query_the_library_is_read_newest_first_and_the_candidates_picked_out(self):
        rule = Rule(name="r", album="A", limit=10, filters={"type": "IMAGE"})        # no text, no photo, no people
        stats = {}
        wanted = {self.ids[i] for i in (0, 3, 9, 15, 20)}           # ids[9] is a video: the type filter drops it
        got = evaluate_rule(self.client, rule, only_ids=wanted, scan_cap=None, stats=stats)
        self.assertEqual({a["id"] for a in got}, wanted - {self.ids[9]})
        self.assertTrue(all(kind == "metadata" for kind, _ in self.fake.searches))   # smart search was never asked
        self.assertEqual(stats["capped"], False)
        self.assertGreaterEqual(stats["scanned"], 21)

    def test_people_any_with_a_ranking_scans_it_to_the_cap(self):
        anna = self.fake.add_person("Anna", [self.ids[2], self.ids[7]])
        ben = self.fake.add_person("Ben", [self.ids[12], self.ids[25]])
        rule = Rule(name="r", album="A", query="mountain", limit=10, filters={"personIds": [anna, ben]},
                    people_match="any")
        stats = {}
        got = evaluate_rule(self.client, rule, only_ids={self.ids[7], self.ids[12], self.ids[25], self.ids[4]},
                            scan_cap=15, stats=stats)
        self.assertEqual([a["id"] for a in got], [self.ids[7], self.ids[12]])         # 25 is past the cap; 4 is nobody's
        self.assertEqual(stats, {"scanned": 15, "capped": True})

    def test_no_only_ids_means_nothing_changes(self):
        rule = Rule(name="r", album="A", query="mountain", limit=3)
        stats = {}
        self.assertEqual([a["id"] for a in evaluate_rule(self.client, rule, stats=stats)], self.ids[:3])
        self.assertEqual(stats, {"scanned": 0, "capped": False})    # (stats is reset but nothing is counted without a filter)
        self.assertEqual(self.smart_requests()[0]["size"], 3)       # and the ranking is read only as deep as the limit


class TestLookingAt(unittest.TestCase):
    def test_counts_and_caps(self):
        stats = {"scanned": 0, "capped": False}
        self.assertEqual(list(looking_at(iter(range(5)), stats, 3)), [0, 1, 2])
        self.assertEqual(stats, {"scanned": 3, "capped": True})
        stats = {"scanned": 0, "capped": False}
        self.assertEqual(list(looking_at(iter(range(3)), stats, 3)), [0, 1, 2])
        self.assertEqual(stats, {"scanned": 3, "capped": False})        # exactly the cap, nothing left: not capped
        self.assertEqual(list(looking_at(iter(range(4)), None, 2)), [0, 1, 2, 3])   # no stats: a plain pass-through


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
