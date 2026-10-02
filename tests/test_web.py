import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from immich_organizer.client import ImmichClient
from immich_organizer.web.server import OrganizerHandler
from tests.fake_immich import API_KEY, FakeImmich

TOKEN = "test-token"


def request(url, *, method="GET", body=None, token=TOKEN, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Organizer-Token", token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        content_type = resp.headers.get("Content-Type", "")
        payload = json.loads(raw) if content_type.startswith("application/json") else raw
        return resp.status, payload


class WebCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich().start()
        self.addCleanup(self.fake.stop)
        self.ids = [a["id"] for a in self.fake.assets]
        self.client = ImmichClient(self.fake.url, API_KEY, retries=0)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"IMMICH_ORGANIZER_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logged = []                      # the panel's journal lines, kept out of the test output
        patcher = mock.patch("immich_organizer.web.server._log", self.logged.append)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.rules_path = Path(self.tmp.name) / "rules.json"
        self.rules_path.write_text(json.dumps({
            "version": 1,
            "rules": [{"name": "Mountains", "album": "Mountains", "query": "mountain", "limit": 4}],
        }))

        handler = type("BoundHandler", (OrganizerHandler,), {
            "client": self.client, "token": TOKEN, "rules_path": self.rules_path, "verbose": False,
        })
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"


class TestAuth(WebCase):
    def test_data_routes_require_the_token(self):
        for path in ("/api/status", "/api/albums", f"/thumb/{self.ids[0]}"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                request(self.base + path, token=None)
            self.assertEqual(ctx.exception.code, 401, path)

    def test_a_wrong_token_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/status", token="nope")
        self.assertEqual(ctx.exception.code, 401)

    def test_token_may_arrive_as_a_query_parameter(self):
        # <img src> cannot set headers, so thumbnails pass the token this way.
        status, _ = request(f"{self.base}/thumb/{self.ids[0]}?t={TOKEN}", token=None)
        self.assertEqual(status, 200)

    def test_post_routes_require_the_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/search", method="POST", body={"query": "x"}, token=None)
        self.assertEqual(ctx.exception.code, 401)

    def test_the_app_shell_is_served_without_a_token(self):
        status, payload = request(self.base + "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"Immich Organizer", payload)


class TestStatic(WebCase):
    def test_assets_are_served(self):
        for path, needle in (
            ("/static/app.js", b"Organizer"),
            ("/static/styles.css", b"--accent"),
            ("/manifest.webmanifest", b"Immich Organizer"),
            ("/sw.js", b"CACHE"),
            ("/icon.svg", b"<svg"),
        ):
            status, payload = request(self.base + path, token=None)
            self.assertEqual(status, 200, path)
            self.assertIn(needle, payload, path)

    def test_path_traversal_is_blocked(self):
        for attempt in ("/static/../server.py", "/static/..%2fserver.py"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                request(self.base + attempt, token=None)
            self.assertIn(ctx.exception.code, (403, 404), attempt)


class TestApi(WebCase):
    def test_status_reports_the_connection(self):
        _, payload = request(self.base + "/api/status")
        self.assertTrue(payload["connected"])
        self.assertEqual(payload["user"], "me@example.com")

    def test_albums_are_listed(self):
        self.fake.add_album("Trip", members=self.ids[:2])
        _, payload = request(self.base + "/api/albums")
        self.assertEqual(payload, [{"id": mock.ANY, "name": "Trip", "count": 2}])

    def test_search_returns_ranked_assets(self):
        self.fake.smart_results["mountain"] = self.ids
        _, payload = request(
            self.base + "/api/search", method="POST", body={"query": "mountain", "limit": 3}
        )
        self.assertEqual(payload["count"], 3)
        self.assertEqual([a["id"] for a in payload["assets"]], self.ids[:3])

    def test_search_by_reference_asset(self):
        ref = self.ids[0]
        self.fake.similar_results[ref] = self.ids[1:4]
        _, payload = request(self.base + "/api/search", method="POST", body={"like": ref})
        self.assertEqual(payload["count"], 3)
        self.assertIn(ref, payload["match"])

    def test_search_applies_refinement(self):
        self.fake.smart_results["beach"] = self.ids[:6]
        self.fake.smart_results["pool"] = [self.ids[1]]
        _, payload = request(self.base + "/api/search", method="POST", body={
            "query": "beach", "limit": 10, "refine": {"none_of": ["pool"]},
        })
        self.assertNotIn(self.ids[1], [a["id"] for a in payload["assets"]])

    def test_search_needs_exactly_one_of_query_or_like(self):
        for body in ({}, {"query": "a", "like": self.ids[0]}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                request(self.base + "/api/search", method="POST", body=body)
            self.assertEqual(ctx.exception.code, 400)

    def test_tag_search_and_tagger_endpoints_are_gone(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/search", method="POST", body={"mode": "tags", "tags": ["beach"]})
        self.assertEqual(ctx.exception.code, 400)
        for path in ("/api/tags", "/api/describer"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                request(self.base + path)
            self.assertEqual(ctx.exception.code, 404)

    def test_sort_choices_are_remembered(self):
        self.assertEqual(request(self.base + "/api/prefs")[1],
                         {"albumsSort": "name", "albumSort": "taken_desc", "searchEngine": "immich"})
        request(self.base + "/api/prefs", method="POST", body={"changes": {"albumsSort": "updated"}})
        self.assertEqual(request(self.base + "/api/prefs")[1]["albumsSort"], "updated")
        for bad in ({"albumsSort": "nonsense"}, {"other": "x"}, {"albumSort": "relevance"}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                request(self.base + "/api/prefs", method="POST", body={"changes": bad})
            self.assertEqual(ctx.exception.code, 400)

    def test_search_rejects_a_malformed_refine_block(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/search", method="POST",
                    body={"query": "a", "refine": "not-an-object"})
        self.assertEqual(ctx.exception.code, 400)

    def test_search_rejects_an_unknown_filter(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/search", method="POST",
                    body={"query": "a", "filters": {"taken_at": "2020-01-01"}})
        self.assertEqual(ctx.exception.code, 400)

    def test_search_rejects_an_absurd_limit(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/search", method="POST", body={"query": "a", "limit": 99999})
        self.assertEqual(ctx.exception.code, 400)

    def test_filing_creates_the_album(self):
        _, payload = request(self.base + "/api/file", method="POST", body={
            "assetIds": self.ids[:3], "album": "Mountains",
        })
        self.assertEqual(payload["added"], 3)
        self.assertTrue(payload["created"])
        album = self.fake.album_by_name("Mountains")
        self.assertEqual(self.fake.album_members[album["id"]], self.ids[:3])

    def test_filing_twice_reports_duplicates(self):
        body = {"assetIds": self.ids[:3], "album": "Mountains"}
        request(self.base + "/api/file", method="POST", body=body)
        _, payload = request(self.base + "/api/file", method="POST", body=body)
        self.assertEqual(payload["added"], 0)
        self.assertEqual(payload["duplicates"], 3)

    def test_filing_validates_its_input(self):
        for body in ({"assetIds": [], "album": "A"}, {"assetIds": self.ids[:1], "album": ""}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                request(self.base + "/api/file", method="POST", body=body)
            self.assertEqual(ctx.exception.code, 400)

    def test_rules_are_listed(self):
        _, payload = request(self.base + "/api/rules")
        self.assertTrue(payload["loaded"])
        self.assertEqual(payload["rules"][0]["name"], "Mountains")

    def test_plan_previews_without_changing_anything(self):
        self.fake.smart_results["mountain"] = self.ids
        _, payload = request(self.base + "/api/plan", method="POST", body={})
        self.assertEqual(payload["totalToAdd"], 4)
        self.assertFalse(payload["applied"])
        self.assertEqual(self.fake.albums, {})

    def test_apply_files_the_assets(self):
        self.fake.smart_results["mountain"] = self.ids
        _, payload = request(self.base + "/api/apply", method="POST", body={})
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["added"], 4)
        self.assertIsNotNone(self.fake.album_by_name("Mountains"))

    def test_thumbnails_are_proxied(self):
        status, payload = request(f"{self.base}/thumb/{self.ids[0]}")
        self.assertEqual(status, 200)
        self.assertTrue(payload.startswith(b"\x89PNG"))

    def test_unknown_route(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_malformed_json_body(self):
        req = urllib.request.Request(
            self.base + "/api/search", data=b"{not json", method="POST"
        )
        req.add_header("X-Organizer-Token", TOKEN)
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)


class TestSearchBeyondOnePage(WebCase):
    def setUp(self):
        super().setUp()
        # 2500 assets, all ranked for one query: more than two Immich pages.
        from tests.fake_immich import make_assets
        big = make_assets(2500)
        self.fake.assets = big
        self.fake.by_id = {a["id"]: a for a in big}
        self.ids = [a["id"] for a in big]
        self.fake.smart_results["cat"] = self.ids

    def test_more_than_1000_results_are_paged_in(self):
        _, payload = request(self.base + "/api/search", method="POST",
                             body={"query": "cat", "limit": 2200})
        got = [a["id"] for a in payload["assets"]]
        self.assertEqual(len(got), 2200)
        self.assertEqual(len(set(got)), 2200)
        self.assertEqual(got, self.ids[:2200])


class TestSkipping(WebCase):
    def setUp(self):
        super().setUp()
        self.fake.smart_results["mountain"] = self.ids

    def search(self, **extra):
        _, payload = request(self.base + "/api/search", method="POST",
                             body={"query": "mountain", "limit": 5, **extra})
        return payload

    def test_skipped_albums_do_not_use_up_the_limit(self):
        self.fake.add_album("Done", members=self.ids[:5])
        payload = self.search(excludeAlbums=["Done"])
        self.assertEqual([a["id"] for a in payload["assets"]], self.ids[5:10])
        self.assertEqual(payload["excluded"], 5)

    def test_albums_can_be_skipped_by_id_too(self):
        album_id = self.fake.add_album("Done", members=self.ids[:2])
        payload = self.search(excludeAlbums=[album_id])
        self.assertEqual(payload["assets"][0]["id"], self.ids[2])

    def test_explicit_skip_ids(self):
        payload = self.search(skipIds=self.ids[:3])
        self.assertEqual(payload["assets"][0]["id"], self.ids[3])
        self.assertEqual(len(payload["assets"]), 5)

    def test_an_unknown_album_is_reported_not_ignored(self):
        payload = self.search(excludeAlbums=["No such album"])
        self.assertEqual(payload["unknownAlbums"], ["No such album"])
        self.assertEqual(payload["assets"][0]["id"], self.ids[0])


class TestMoveAndUndo(WebCase):
    def members(self, name):
        return self.fake.album_members[self.fake.album_by_name(name)["id"]]

    def test_add_keeps_other_albums(self):
        self.fake.add_album("Old", members=self.ids[:2])
        request(self.base + "/api/file", method="POST",
                body={"assetIds": self.ids[:2], "album": "New"})
        self.assertEqual(self.members("Old"), self.ids[:2])
        self.assertEqual(self.members("New"), self.ids[:2])

    def test_move_takes_assets_out_of_every_other_album(self):
        self.fake.add_album("A", members=self.ids[:3])
        self.fake.add_album("B", members=[self.ids[0], self.ids[9]])
        _, payload = request(self.base + "/api/file", method="POST",
                             body={"assetIds": self.ids[:2], "album": "C", "move": True})
        self.assertEqual(payload["added"], 2)
        self.assertEqual(payload["removedTotal"], 3)
        self.assertEqual(self.members("A"), [self.ids[2]])
        self.assertEqual(self.members("B"), [self.ids[9]])
        self.assertEqual(self.members("C"), self.ids[:2])

    def test_move_also_cleans_up_assets_already_in_the_target(self):
        # The "I added 1000 to test album and they are still in their old albums" case.
        self.fake.add_album("Old", members=self.ids[:4])
        self.fake.add_album("test album", members=self.ids[:4])
        _, payload = request(self.base + "/api/file", method="POST",
                             body={"assetIds": self.ids[:4], "album": "test album", "move": True})
        self.assertEqual(payload["duplicates"], 4)
        self.assertEqual(self.members("Old"), [])
        self.assertEqual(self.members("test album"), self.ids[:4])

    def test_undo_reverses_a_move(self):
        self.fake.add_album("A", members=self.ids[:3])
        _, filed = request(self.base + "/api/file", method="POST",
                           body={"assetIds": self.ids[:2], "album": "C", "move": True})
        _, history = request(self.base + "/api/history")
        run = history["runs"][0]
        self.assertEqual(run["runId"], filed["runId"])
        self.assertEqual(run["removed"], {"A": 2})

        _, undone = request(self.base + "/api/undo", method="POST", body={"runId": run["runId"]})
        self.assertEqual(undone["removed"], 2)
        self.assertEqual(undone["restored"], 2)
        self.assertEqual(sorted(self.members("A")), sorted(self.ids[:3]))
        self.assertEqual(self.members("C"), [])

        _, history = request(self.base + "/api/history")
        self.assertTrue(history["runs"][0]["undone"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/undo", method="POST", body={"runId": run["runId"]})
        self.assertEqual(ctx.exception.code, 400)


class TestRuleEditing(WebCase):
    def test_rules_carry_their_raw_form(self):
        _, payload = request(self.base + "/api/rules")
        self.assertEqual(payload["rules"][0]["raw"]["query"], "mountain")

    def test_save_adds_edits_and_deletes(self):
        _, payload = request(self.base + "/api/rules")
        raws = [r["raw"] for r in payload["rules"]]
        raws.append({"name": "Beach", "album": "Beach", "query": "beach", "limit": 50,
                     "exclude_albums": ["Mountains"], "filters": {"taken_after": "2023-01-01"}})
        _, saved = request(self.base + "/api/rules/save", method="POST", body={"rules": raws})
        self.assertEqual([r["name"] for r in saved["rules"]], ["Mountains", "Beach"])
        self.assertEqual(saved["rules"][1]["excludeAlbums"], ["Mountains"])

        _, saved = request(self.base + "/api/rules/save", method="POST", body={"rules": raws[1:]})
        self.assertEqual([r["name"] for r in saved["rules"]], ["Beach"])
        _, saved = request(self.base + "/api/rules/save", method="POST", body={"rules": []})
        self.assertEqual(saved["rules"], [])

    def test_an_invalid_rule_is_refused_and_the_file_kept(self):
        before = self.rules_path.read_text()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/rules/save", method="POST",
                    body={"rules": [{"name": "x", "album": "x"}]})  # no query/like
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(self.rules_path.read_text(), before)

    def test_yaml_rules_round_trip_with_dates(self):
        yaml_path = Path(self.tmp.name) / "rules.yaml"
        yaml_path.write_text(
            "version: 1\nrules:\n  - name: Old\n    album: Old\n    query: old\n"
            "    filters:\n      taken_before: 2020-01-01\n"
        )
        self.httpd.RequestHandlerClass.rules_path = yaml_path
        _, payload = request(self.base + "/api/rules")
        self.assertTrue(payload["loaded"], payload)
        raw = payload["rules"][0]["raw"]
        self.assertEqual(raw["filters"]["taken_before"], "2020-01-01")
        _, saved = request(self.base + "/api/rules/save", method="POST", body={"rules": [raw]})
        self.assertEqual(saved["rules"][0]["name"], "Old")
        self.assertIn("edited from the panel", yaml_path.read_text())
        self.assertTrue((Path(self.tmp.name) / "rules.yaml.bak").exists())

    def test_rule_exclude_albums_apply_in_plans(self):
        self.fake.smart_results["mountain"] = self.ids
        self.fake.add_album("Seen", members=self.ids[:4])
        _, payload = request(self.base + "/api/rules")
        raw = dict(payload["rules"][0]["raw"], exclude_albums=["Seen"])
        request(self.base + "/api/rules/save", method="POST", body={"rules": [raw]})
        _, plan = request(self.base + "/api/plan", method="POST", body={})
        self.assertEqual([a["id"] for a in plan["entries"][0]["assets"]], self.ids[4:8])



class TestPeople(WebCase):
    def setUp(self):
        super().setUp()
        self.ana = self.fake.add_person("Ana", self.ids[:6])
        self.ben = self.fake.add_person("Ben", self.ids[4:10])
        self.fake.add_person("", self.ids[:2])                 # unnamed
        self.fake.add_person("Hidden", self.ids[:1], hidden=True)

    def test_named_visible_people_are_listed(self):
        _, payload = request(self.base + "/api/people")
        self.assertEqual([p["name"] for p in payload["people"]], ["Ana", "Ben"])
        self.assertEqual(payload["unnamed"], 1)

    def test_person_thumbnails_are_proxied(self):
        status, payload = request(f"{self.base}/thumb/person/{self.ana}")
        self.assertEqual(status, 200)
        self.assertTrue(payload.startswith(b"\xff\xd8"))

    def test_people_alone_find_every_photo_of_them(self):
        _, payload = request(self.base + "/api/search", method="POST",
                             body={"people": [self.ana], "limit": 100})
        self.assertEqual(sorted(a["id"] for a in payload["assets"]), sorted(self.ids[:6]))

    def test_several_people_means_together(self):
        _, payload = request(self.base + "/api/search", method="POST",
                             body={"people": [self.ana, self.ben], "limit": 100})
        self.assertEqual(sorted(a["id"] for a in payload["assets"]), sorted(self.ids[4:6]))

    def test_people_narrow_a_description(self):
        self.fake.smart_results["beach"] = self.ids[::-1]
        _, payload = request(self.base + "/api/search", method="POST",
                             body={"query": "beach", "people": [self.ben], "limit": 3})
        self.assertEqual([a["id"] for a in payload["assets"]], [self.ids[9], self.ids[8], self.ids[7]])

    def test_people_combine_with_skipped_albums(self):
        self.fake.add_album("Done", members=self.ids[:3])
        _, payload = request(self.base + "/api/search", method="POST",
                             body={"people": [self.ana], "excludeAlbums": ["Done"], "limit": 100})
        self.assertEqual(sorted(a["id"] for a in payload["assets"]), sorted(self.ids[3:6]))

    def test_an_empty_search_is_still_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/search", method="POST", body={"people": []})
        self.assertEqual(ctx.exception.code, 400)

    def test_people_rules_show_names_and_plan(self):
        rule = {"name": "Ana", "album": "Ana", "limit": 50, "filters": {"person_ids": [self.ana]}}
        _, saved = request(self.base + "/api/rules/save", method="POST", body={"rules": [rule]})
        self.assertEqual(saved["rules"][0]["people"], ["Ana"])
        _, plan = request(self.base + "/api/plan", method="POST", body={})
        self.assertEqual(plan["entries"][0]["toAdd"], 6)


class TestPeopleAny(WebCase):
    def setUp(self):
        super().setUp()
        self.ana = self.fake.add_person("Ana", self.ids[:6])      # 0-5
        self.ben = self.fake.add_person("Ben", self.ids[4:10])    # 4-9

    def search(self, **body):
        _, payload = request(self.base + "/api/search", method="POST", body=body)
        return [a["id"] for a in payload["assets"]]

    def test_any_without_description_is_the_union_newest_first(self):
        got = self.search(people=[self.ana, self.ben], peopleMatch="any", limit=100)
        self.assertEqual(sorted(got), sorted(self.ids[:10]))
        dates = [self.fake.by_id[i]["fileCreatedAt"] for i in got]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_all_is_still_the_default(self):
        got = self.search(people=[self.ana, self.ben], limit=100)
        self.assertEqual(sorted(got), sorted(self.ids[4:6]))

    def test_any_with_description_keeps_the_smart_ranking(self):
        ranking = self.ids[::-1]          # 39, 38, ... 0
        self.fake.smart_results["beach"] = ranking
        got = self.search(query="beach", people=[self.ana, self.ben], peopleMatch="any", limit=4)
        self.assertEqual(got, [self.ids[9], self.ids[8], self.ids[7], self.ids[6]])

    def test_any_with_description_reads_past_the_first_1000(self):
        from tests.fake_immich import make_assets
        big = make_assets(2600)
        self.fake.assets = big
        self.fake.by_id = {a["id"]: a for a in big}
        ids = [a["id"] for a in big]
        rare = self.fake.add_person("Rare", ids[2400:2405])
        self.fake.smart_results["cat"] = ids
        got = self.search(query="cat", people=[self.ana, rare], peopleMatch="any", limit=50)
        self.assertEqual(got, ids[:6] + ids[2400:2405])

    def test_any_combines_with_skipped_albums(self):
        self.fake.add_album("Done", members=self.ids[:5])
        got = self.search(people=[self.ana, self.ben], peopleMatch="any", excludeAlbums=["Done"], limit=100)
        self.assertEqual(sorted(got), sorted(self.ids[5:10]))

    def test_bad_match_mode_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/search", method="POST",
                    body={"people": [self.ana], "peopleMatch": "some"})
        self.assertEqual(ctx.exception.code, 400)

    def test_rules_store_and_apply_any(self):
        rule = {"name": "A or B", "album": "AB", "limit": 100,
                "filters": {"person_ids": [self.ana, self.ben]}, "people_match": "any"}
        _, saved = request(self.base + "/api/rules/save", method="POST", body={"rules": [rule]})
        self.assertIn("any of them", saved["rules"][0]["match"])
        _, plan = request(self.base + "/api/plan", method="POST", body={})
        self.assertEqual(plan["entries"][0]["toAdd"], 10)


class TestFacesReview(WebCase):
    def setUp(self):
        super().setUp()
        self.blurry = self.fake.add_person("", self.ids[:3])
        self.sharp = self.fake.add_person("", self.ids[3:6])
        self.hidden = self.fake.add_person("", self.ids[6:8], hidden=True)
        self.named = self.fake.add_person("Ana", self.ids[8:10])
        quality = {"generated": "2026-09-30T00:00:00+00:00", "method": "test", "faces": 10, "people": {
            self.blurry: {"name": "", "faces": 3, "best": 2.0, "median": 1.0, "bestFaceId": "f1", "bestAssetId": self.ids[1]},
            self.sharp: {"name": "", "faces": 3, "best": 90.0, "median": 50.0, "bestFaceId": "f2", "bestAssetId": self.ids[4]},
            self.hidden: {"name": "", "faces": 2, "best": 5.0, "median": 5.0, "bestFaceId": "f3", "bestAssetId": self.ids[7]},
            self.named: {"name": "Ana", "faces": 2, "best": 70.0, "median": 60.0, "bestFaceId": "f4", "bestAssetId": self.ids[9]},
        }}
        self.quality_file = Path(self.tmp.name) / "state" / "face-quality.json"
        self.quality_file.parent.mkdir(parents=True, exist_ok=True)
        self.quality_file.write_text(json.dumps(quality))

    def test_unnamed_people_are_listed_blurriest_first(self):
        _, payload = request(self.base + "/api/faces")
        self.assertTrue(payload["measured"])
        self.assertEqual([p["id"] for p in payload["people"]], [self.blurry, self.hidden, self.sharp])
        self.assertTrue(payload["people"][1]["hidden"])

    def test_not_measured_yet(self):
        self.quality_file.unlink()
        _, payload = request(self.base + "/api/faces")
        self.assertFalse(payload["measured"])
        self.assertEqual(payload["unnamed"], 3)

    def test_hide_and_unhide(self):
        _, res = request(self.base + "/api/faces/visibility", method="POST",
                         body={"ids": [self.blurry], "hidden": True})
        self.assertEqual(res["changed"], 1)
        self.assertTrue(self.fake.people[self.blurry]["isHidden"])
        request(self.base + "/api/faces/visibility", method="POST", body={"ids": [self.hidden], "hidden": False})
        self.assertFalse(self.fake.people[self.hidden]["isHidden"])

    def test_visibility_validates_input(self):
        for body in ({"ids": [], "hidden": True}, {"ids": [self.blurry], "hidden": "yes"}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                request(self.base + "/api/faces/visibility", method="POST", body=body)
            self.assertEqual(ctx.exception.code, 400)

    def test_covers_use_the_sharpest_face_for_unnamed_only(self):
        _, res = request(self.base + "/api/faces/covers", method="POST", body={})
        self.assertEqual(res["changed"], 3)
        self.assertEqual(self.fake.people[self.sharp]["featureFaceAssetId"], self.ids[4])
        self.assertNotIn("featureFaceAssetId", self.fake.people[self.named])


class TestAlbums(WebCase):
    def setUp(self):
        super().setUp()
        self.a = self.fake.add_album("Trip A", members=self.ids[:5])
        self.b = self.fake.add_album("Trip B", members=self.ids[5:7])
        self.c = self.fake.add_album("Tiny", members=[self.ids[9]])
        self.other = self.fake.add_album("Other", members=self.ids[:2])

    def post(self, action, body):
        return request(self.base + f"/api/albums/{action}", method="POST", body=body)[1]

    def members(self, album_id):
        return sorted(self.fake.album_members.get(album_id, []))

    def undo(self, run_id):
        return request(self.base + "/api/undo", method="POST", body={"runId": run_id})[1]

    def test_list(self):
        _, payload = request(self.base + "/api/albums/list")
        by_name = {a["name"]: a for a in payload["albums"]}
        self.assertEqual(by_name["Trip A"]["count"], 5)
        self.assertEqual(by_name["Tiny"]["count"], 1)

    def test_items_sorted_by_date_taken(self):
        _, payload = request(self.base + f"/api/albums/{self.a}/items?sort=taken_asc")
        taken = [i["taken"] for i in payload["items"]]
        self.assertEqual(taken, sorted(taken))
        self.assertEqual(len(taken), 5)

    def test_move_only_leaves_the_source_album(self):
        res = self.post("transfer", {"sourceId": self.a, "assetIds": self.ids[:2], "target": "Trip B", "move": True})
        self.assertEqual(res["added"], 2)
        self.assertEqual(res["removedFromSource"], 2)
        self.assertEqual(self.members(self.a), sorted(self.ids[2:5]))
        self.assertEqual(self.members(self.b), sorted(self.ids[:2] + self.ids[5:7]))
        self.assertEqual(self.members(self.other), sorted(self.ids[:2]))   # untouched

    def test_copy_keeps_the_source_and_can_create_the_target(self):
        res = self.post("transfer", {"sourceId": self.a, "assetIds": self.ids[:3], "target": "Brand new", "move": False})
        self.assertTrue(res["created"])
        self.assertEqual(self.members(self.a), sorted(self.ids[:5]))
        self.assertEqual(self.members(res["targetId"]), sorted(self.ids[:3]))

    def test_move_is_undoable(self):
        res = self.post("transfer", {"sourceId": self.a, "assetIds": self.ids[:2], "target": "Trip B", "move": True})
        self.undo(res["runId"])
        self.assertEqual(self.members(self.a), sorted(self.ids[:5]))
        self.assertEqual(self.members(self.b), sorted(self.ids[5:7]))

    def test_remove_and_undo(self):
        res = self.post("remove", {"albumId": self.a, "assetIds": self.ids[:3]})
        self.assertEqual(res["removed"], 3)
        self.assertEqual(self.members(self.a), sorted(self.ids[3:5]))
        self.undo(res["runId"])
        self.assertEqual(self.members(self.a), sorted(self.ids[:5]))

    def test_merge_small_albums_and_delete_them(self):
        res = self.post("merge", {"sourceIds": [self.b, self.c], "target": "Trip A", "deleteSources": True})
        self.assertEqual(res["added"], 3)
        self.assertEqual(sorted(res["deletedAlbums"]), ["Tiny", "Trip B"])
        self.assertNotIn(self.b, self.fake.albums)
        self.assertEqual(self.members(self.a), sorted(self.ids[:7] + [self.ids[9]]))

    def test_merge_undo_recreates_deleted_albums(self):
        res = self.post("merge", {"sourceIds": [self.b, self.c], "target": "Trip A", "deleteSources": True})
        self.undo(res["runId"])
        self.assertEqual(self.members(self.a), sorted(self.ids[:5]))
        tiny = self.fake.album_by_name("Tiny")
        self.assertIsNotNone(tiny)
        self.assertEqual(self.members(tiny["id"]), [self.ids[9]])
        self.assertEqual(self.members(self.fake.album_by_name("Trip B")["id"]), sorted(self.ids[5:7]))

    def test_merge_can_keep_sources(self):
        self.post("merge", {"sourceIds": [self.c], "target": "Trip A", "deleteSources": False})
        self.assertIn(self.c, self.fake.albums)
        self.assertIn(self.ids[9], self.members(self.a))

    def test_merge_overwrite_replaces_target_contents(self):
        res = self.post("merge", {"sourceIds": [self.b], "target": "Trip A", "deleteSources": False, "replaceTarget": True})
        self.assertEqual(self.members(self.a), sorted(self.ids[5:7]))
        self.assertEqual(res["removedFromTarget"], 5)
        self.undo(res["runId"])
        self.assertEqual(self.members(self.a), sorted(self.ids[:5]))

    def test_merge_into_a_new_album(self):
        res = self.post("merge", {"sourceIds": [self.b, self.c], "target": "Combined"})
        self.assertTrue(res["created"])
        self.assertEqual(self.members(res["targetId"]), sorted(self.ids[5:7] + [self.ids[9]]))

    def test_delete_keeps_photos_and_undo_restores(self):
        res = self.post("delete", {"albumIds": [self.b]})
        self.assertEqual(res["deleted"], ["Trip B"])
        self.assertNotIn(self.b, self.fake.albums)
        self.assertEqual(len(self.fake.assets), 40)       # photos untouched
        self.undo(res["runId"])
        restored = self.fake.album_by_name("Trip B")
        self.assertEqual(self.members(restored["id"]), sorted(self.ids[5:7]))

    def test_rename_and_undo(self):
        res = self.post("rename", {"albumId": self.c, "name": "Small one"})
        self.assertEqual(self.fake.albums[self.c]["albumName"], "Small one")
        self.undo(res["runId"])
        self.assertEqual(self.fake.albums[self.c]["albumName"], "Tiny")

    def test_create_refuses_duplicates(self):
        self.post("create", {"name": "Fresh"})
        self.assertIsNotNone(self.fake.album_by_name("Fresh"))
        with self.assertRaises(urllib.error.HTTPError):
            self.post("create", {"name": "fresh"})

    def test_history_lists_album_changes(self):
        self.post("delete", {"albumIds": [self.c]})
        _, hist = request(self.base + "/api/history")
        self.assertEqual(hist["runs"][0]["deleted"], ["Tiny"])

    def test_unknown_action(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("explode", {})
        self.assertEqual(ctx.exception.code, 404)


class FakeThemeBackend:
    """Scores are canned per description: {description: {asset_id: score}}."""

    def __init__(self, scores):
        self.scores = scores
        self.model = "model-a"
        self.vector_calls = 0

    def model_name(self, client):
        return self.model

    def text_vector(self, text, model):
        self.vector_calls += 1
        return [text]

    types: dict = {}          # asset id -> "IMAGE"/"VIDEO" (unlisted = IMAGE)

    def _scores(self, vec, type_=None):
        sc = self.scores.get(vec[0], {})
        return {i: s for i, s in sc.items() if not type_ or self.types.get(i, "IMAGE") == type_}

    def above(self, vec, cutoff, type_=None):
        return {i: s for i, s in self._scores(vec, type_).items() if s >= cutoff}

    def top(self, vec, limit, type_=None):
        return sorted(self._scores(vec, type_).items(), key=lambda kv: -kv[1])[:limit]

    def album_scores(self, vec, album_id):
        return self.scores.get(vec[0], {})

    people_assets: dict = {}   # person id -> asset ids (for "only people" smart albums)

    def select(self, *, cutoff=None, limit=None, vec=None, like=None, media=None, must=(), must_not=(),
               term_cutoff=0.1, people=(), people_match="all", exclude_album_ids=(), **_):
        if vec is not None:
            sc = dict(self.scores.get(vec[0], {}))
        elif like:
            sc = dict(self.scores.get("like:" + like, {}))
        else:
            sc = {}
        if people:
            sets = [set(self.people_assets.get(p, [])) for p in people]
            keep = set.union(*sets) if people_match == "any" else set.intersection(*sets)
            sc = {i: sc.get(i) for i in keep} if (vec is None and not like) else {i: v for i, v in sc.items() if i in keep}
        if media:
            sc = {i: v for i, v in sc.items() if self.types.get(i, "IMAGE") == media}
        for tv in must:
            sc = {i: v for i, v in sc.items() if self.scores.get(tv[0], {}).get(i, 0) >= term_cutoff}
        for tv in must_not:
            sc = {i: v for i, v in sc.items() if self.scores.get(tv[0], {}).get(i, 0) < term_cutoff}
        if cutoff is not None and (vec is not None or like):
            sc = {i: v for i, v in sc.items() if v >= cutoff}
        rows = sorted(sc.items(), key=lambda kv: -(kv[1] or 0))
        return rows[:limit] if limit else rows

    def select_counts(self, cutoffs, **kw):
        rows = self.select(**kw)
        return {"total": len(rows), **{f"{c:.3f}": sum(1 for _, v in rows if (v or 0) >= c) for c in cutoffs}}

    def counts(self, vec, cutoffs, type_=None):
        sc = self._scores(vec, type_)
        return {"total": len(sc), **{f"{c:.3f}": sum(1 for v in sc.values() if v >= c) for c in cutoffs}}


class TestThemes(WebCase):
    def setUp(self):
        super().setUp()
        # ids[0..9] score 0.20 down to 0.02 for "fnaf"
        self.backend = FakeThemeBackend({"fnaf": {self.ids[i]: round(0.20 - i * 0.02, 2) for i in range(10)}})
        self.httpd.RequestHandlerClass.theme_backend = self.backend

    def post(self, action, body):
        return request(self.base + f"/api/themes/{action}", method="POST", body=body)[1]

    def members(self, name):
        album = self.fake.album_by_name(name)
        return sorted(self.fake.album_members[album["id"]]) if album else []

    def make(self, cutoff=0.13, **extra):
        return self.post("save", {"theme": {"description": "fnaf", "name": "FNAF", "cutoff": cutoff, **extra}})["theme"]

    def test_preview_ranks_and_counts(self):
        data = self.post("preview", {"description": "fnaf", "limit": 3})
        self.assertEqual([i["id"] for i in data["items"]], self.ids[:3])
        self.assertEqual(data["counts"]["0.100"], 6)

    def test_run_creates_the_album_and_adds_only_matches(self):
        t = self.make(cutoff=0.13)
        res = self.post("run", {"ids": [t["id"]]})["results"][0]
        self.assertTrue(res["created"])
        self.assertEqual(res["added"], 4)                  # 0.20 0.18 0.16 0.14
        self.assertEqual(self.members("FNAF"), sorted(self.ids[:4]))
        self.assertIn("Auto theme", self.fake.album_by_name("FNAF")["description"])

    def test_second_run_adds_nothing_new(self):
        t = self.make()
        self.post("run", {"ids": [t["id"]]})
        res = self.post("run", {"ids": [t["id"]]})["results"][0]
        self.assertEqual(res["added"], 0)

    def test_removed_photos_are_never_readded(self):
        t = self.make()
        self.post("run", {"ids": [t["id"]]})
        album = self.fake.album_by_name("FNAF")
        self.fake.album_members[album["id"]].remove(self.ids[0])
        self.post("run", {"ids": [t["id"]]})
        self.assertNotIn(self.ids[0], self.members("FNAF"))
        self.post("forget", {"id": t["id"]})
        self.post("run", {"ids": [t["id"]]})
        self.assertIn(self.ids[0], self.members("FNAF"))

    def test_lowering_the_cutoff_adds_the_newly_matching(self):
        t = self.make(cutoff=0.15)
        self.post("run", {"ids": [t["id"]]})
        self.post("save", {"theme": {"id": t["id"], "description": "fnaf", "cutoff": 0.11}})
        res = self.post("run", {"ids": [t["id"]]})["results"][0]
        self.assertEqual(res["added"], 2)                  # 0.14 0.12
        self.assertEqual(self.members("FNAF"), sorted(self.ids[:5]))

    def test_new_uploads_are_picked_up(self):
        t = self.make()
        self.post("run", {"ids": [t["id"]]})
        self.backend.scores["fnaf"][self.ids[20]] = 0.5    # a new photo got its embedding
        res = self.post("run", {})["results"][0]
        self.assertEqual(res["added"], 1)

    def test_paused_themes_skip_run_all_but_run_on_demand(self):
        t = self.make(enabled=False)
        self.assertEqual(self.post("run", {})["results"], [])
        self.assertEqual(self.post("run", {"ids": [t["id"]]})["results"][0]["added"], 4)

    def test_undo_removes_what_a_run_added_and_it_stays_out(self):
        t = self.make()
        res = self.post("run", {"ids": [t["id"]]})["results"][0]
        request(self.base + "/api/undo", method="POST", body={"runId": res["runId"]})
        self.assertEqual(self.members("FNAF"), [])
        self.post("run", {"ids": [t["id"]]})
        self.assertEqual(self.members("FNAF"), [])

    def test_vector_is_cached_until_the_model_changes(self):
        t = self.make()
        self.post("run", {"ids": [t["id"]]}); self.post("run", {"ids": [t["id"]]})
        self.assertEqual(self.backend.vector_calls, 1)
        self.backend.model = "model-b"
        self.post("run", {"ids": [t["id"]]})
        self.assertEqual(self.backend.vector_calls, 2)

    def test_list_hides_vectors(self):
        t = self.make()
        self.post("run", {"ids": [t["id"]]})
        _, data = request(self.base + "/api/themes")
        self.assertNotIn("vectors", data["themes"][0])
        self.assertEqual(data["themes"][0]["lastAdded"], 4)

    def test_delete_theme_can_keep_or_delete_the_album(self):
        t = self.make()
        self.post("run", {"ids": [t["id"]]})
        self.post("delete", {"id": t["id"], "deleteAlbum": False})
        self.assertIsNotNone(self.fake.album_by_name("FNAF"))
        t2 = self.make()
        self.post("run", {"ids": [t2["id"]]})
        self.post("delete", {"id": t2["id"], "deleteAlbum": True})
        self.assertIsNone(self.fake.album_by_name("FNAF"))

    def test_validation(self):
        for theme in ({"description": ""}, {"description": "x", "cutoff": 2}, {"description": "x", "cutoff": "high"}):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("save", {"theme": theme})
            self.assertEqual(ctx.exception.code, 400)

    def test_two_themes_cannot_fill_the_same_album(self):
        self.make()
        with self.assertRaises(urllib.error.HTTPError):
            self.post("save", {"theme": {"description": "other", "album": "fnaf"}})

    def test_runs_do_not_overlap(self):
        from immich_organizer import themes
        with themes.run_lock():
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("run", {})
        self.assertEqual(ctx.exception.code, 502)


if __name__ == "__main__":
    unittest.main()


class TestMediaAndAlbumExtras(WebCase):
    def setUp(self):
        super().setUp()
        self.backend = FakeThemeBackend({"fnaf": {self.ids[i]: round(0.20 - i * 0.02, 2) for i in range(10)}})
        self.backend.types = {self.ids[1]: "VIDEO", self.ids[3]: "VIDEO"}
        self.httpd.RequestHandlerClass.theme_backend = self.backend

    def raw(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers={"X-Organizer-Token": TOKEN, **(headers or {})})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()

    def test_media_streams_with_range(self):
        status, headers, body = self.raw(f"/media/{self.ids[0]}?kind=video", {"Range": "bytes=10-19"})
        self.assertEqual((status, len(body)), (206, 10))
        self.assertEqual(headers["Content-Range"], "bytes 10-19/1024")
        self.assertEqual(body, bytes(range(10, 20)))
        status, headers, body = self.raw(f"/media/{self.ids[0]}?kind=original&download=1&name=a.jpg")
        self.assertEqual((status, len(body)), (200, 1024))
        self.assertIn("attachment", headers["Content-Disposition"])
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.raw("/media/not-a-uuid")
        self.assertEqual(err.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as err:      # token required
            urllib.request.urlopen(self.base + f"/media/{self.ids[0]}", timeout=10)
        self.assertEqual(err.exception.code, 401)

    def test_media_requests_are_logged(self):
        self.raw(f"/media/{self.ids[0]}?kind=video", {"Range": "bytes=0-", "X-Playback-Id": "ab12cd34-1"})
        line = self.logged[-1]
        self.assertIn(f"media video {self.ids[0][:8]} [ab12cd34-1] bytes=0- -> 206", line)
        self.assertIn("sent 0.0 of 0.0 MB", line)
        self.assertIn("waited on phone", line)
        self.assertTrue(line.endswith("complete"), line)
        with self.assertRaises(urllib.error.HTTPError):
            self.raw("/media/ffffffff-ffff-ffff-ffff-ffffffffffff?kind=video")
        self.assertIn("Immich answered 404", self.logged[-1])

    def test_speed_test_sends_what_was_asked(self):
        status, headers, body = self.raw("/api/diag/speed?mb=2")
        self.assertEqual((status, len(body), headers["Cache-Control"]), (200, 2_000_000, "no-store"))
        self.assertIn("speed test 2 MB: sent 2.0 MB", self.logged[-1])
        self.assertEqual(len(self.raw("/api/diag/speed?mb=999")[2]), 64_000_000)    # capped
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.raw("/api/diag/speed?mb=lots")
        self.assertEqual(err.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as err:      # token required
            urllib.request.urlopen(self.base + "/api/diag/speed", timeout=10)
        self.assertEqual(err.exception.code, 401)

    def test_asset_info(self):
        info = request(self.base + f"/api/asset/{self.ids[0]}")[1]
        self.assertEqual(info["id"], self.ids[0])
        # Immich v3 sends a video's length in milliseconds, older versions as "H:MM:SS.fffff"
        seconds = OrganizerHandler._seconds
        self.assertEqual(seconds(123786), 123.79)
        self.assertEqual(seconds("0:02:03.78600"), 123.79)
        self.assertIsNone(seconds(0))
        self.assertIsNone(seconds("0:00:00.00000"))
        self.assertIsNone(seconds(None))

    def test_theme_only_videos(self):
        post = lambda action, body: request(self.base + f"/api/themes/{action}", method="POST", body=body)[1]
        prev = post("preview", {"description": "fnaf", "limit": 10, "media": "VIDEO"})
        self.assertEqual([i["id"] for i in prev["items"]], [self.ids[1], self.ids[3]])
        t = post("save", {"theme": {"description": "fnaf", "name": "FNAF vids", "cutoff": 0.10, "media": "VIDEO"}})["theme"]
        self.assertEqual(t["media"], "VIDEO")
        res = post("run", {"ids": [t["id"]]})["results"][0]
        self.assertEqual(res["added"], 2)
        with self.assertRaises(urllib.error.HTTPError):
            post("preview", {"description": "fnaf", "media": "GIF"})

    def test_album_relevance_sort_and_type_filter(self):
        album = self.client.create_album("Mixed")
        self.client.add_assets_to_album(album["id"], self.ids[:6])
        data = request(self.base + f"/api/albums/{album['id']}/items?sort=relevance&q=fnaf")[1]
        self.assertEqual(data["sort"], "relevance")
        self.assertEqual([i["id"] for i in data["items"]], self.ids[:6])       # 0.20, 0.18, ... most confident first
        self.assertEqual(data["items"][0]["score"], 0.2)
        data = request(self.base + f"/api/albums/{album['id']}/items?sort=taken_desc&type=VIDEO")[1]
        self.assertTrue(all(i["type"] == "VIDEO" for i in data["items"]))


class TestSmartAlbums(WebCase):
    def setUp(self):
        super().setUp()
        sc = {self.ids[i]: round(0.20 - i * 0.02, 2) for i in range(10)}
        self.backend = FakeThemeBackend({"fnaf": sc, "dark": {self.ids[0]: 0.3, self.ids[2]: 0.3},
                                         f"like:{self.ids[5]}": {self.ids[i]: 0.9 - i * 0.05 for i in range(6)}})
        self.backend.people_assets = {"11111111-1111-1111-1111-111111111111": self.ids[3:8],
                                      "22222222-2222-2222-2222-222222222222": self.ids[6:9]}
        self.httpd.RequestHandlerClass.theme_backend = self.backend

    def post(self, action, body):
        return request(self.base + f"/api/themes/{action}", method="POST", body=body)[1]

    def members(self, name):
        album = self.fake.album_by_name(name)
        return sorted(self.fake.album_members[album["id"]]) if album else []

    def test_top_n_mode(self):
        t = self.post("save", {"theme": {"description": "fnaf", "name": "Top3", "mode": "top", "limit": 3}})["theme"]
        self.assertEqual(self.post("run", {"ids": [t["id"]]})["results"][0]["added"], 3)
        self.assertEqual(self.members("Top3"), sorted(self.ids[:3]))

    def test_like_a_photo_and_must_match_terms(self):
        prev = self.post("preview", {"theme": {"source": "like", "like": self.ids[5], "cutoff": 0.5, "all_of": "dark"}})
        self.assertEqual([i["id"] for i in prev["items"]], [self.ids[0], self.ids[2]])
        prev = self.post("preview", {"theme": {"description": "fnaf", "none_of": ["dark"], "cutoff": 0.15}})
        ids = [i["id"] for i in prev["items"]]
        self.assertNotIn(self.ids[0], ids)
        self.assertNotIn(self.ids[2], ids)
        self.assertEqual(ids[:2], [self.ids[1], self.ids[3]])        # preview lists best first, cut-off applies on run
        self.assertEqual(prev["counts"]["0.160"], 1)          # 0.18 (0.20 was excluded by "dark")

    def test_people_only_all_and_any(self):
        a, b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
        prev = self.post("preview", {"theme": {"source": "none", "people": [a, b]}})
        self.assertEqual(sorted(i["id"] for i in prev["items"]), sorted(self.ids[6:8]))
        self.assertIsNone(prev["items"][0]["score"])
        prev = self.post("preview", {"theme": {"source": "none", "people": [a, b], "people_match": "any"}})
        self.assertEqual(len(prev["items"]), 6)
        with self.assertRaises(urllib.error.HTTPError):
            self.post("preview", {"theme": {"source": "none"}})

    def test_actions_and_validation(self):
        t = self.post("save", {"theme": {"description": "fnaf", "name": "Fav", "cutoff": 0.17, "favorite": True,
                                         "archive": True}})["theme"]
        self.post("run", {"ids": [t["id"]]})
        flags = [u for u in self.fake.updates if u.get("isFavorite") or u.get("visibility") == "archive"]
        self.assertEqual(len(flags), 2)
        for bad in ({"description": "fnaf", "mode": "top", "limit": 0}, {"description": "fnaf", "taken_after": "yesterday"},
                    {"source": "like", "like": "nope"}, {"description": "fnaf", "cutoff": 2}):
            with self.assertRaises(urllib.error.HTTPError, msg=bad):
                self.post("save", {"theme": bad})

    def test_rules_are_imported_once(self):
        self.rules_path.write_text(json.dumps({"version": 1, "defaults": {"limit": 150, "filters": {"type": "IMAGE"}},
            "rules": [{"name": "Mountains", "album": "Mountains", "query": "mountain", "limit": 4},
                      {"name": "Vids", "album": "Vids", "query": "fnaf", "filters": {"type": "VIDEO"},
                       "exclude_albums": ["Vids"], "actions": {"favorite": True}}]}))
        themes = request(self.base + "/api/themes")[1]["themes"]
        byname = {t["name"]: t for t in themes}
        self.assertEqual((byname["Mountains"]["mode"], byname["Mountains"]["limit"], byname["Mountains"]["media"]),
                         ("top", 4, "IMAGE"))
        self.assertEqual((byname["Vids"]["media"], byname["Vids"]["exclude_albums"], byname["Vids"]["favorite"]),
                         ("VIDEO", ["Vids"], True))
        self.assertFalse(byname["Vids"]["enabled"])          # rules never ran by themselves
        again = request(self.base + "/api/themes")[1]["themes"]
        self.assertEqual(len(again), len(themes))           # not imported twice


class FakeSPView:
    """Just enough of searchplus.View for smart albums: per-asset arrays."""

    def __init__(self, ids, types=None, taken=None, indexed=None):
        import numpy as np
        self.ids = list(ids)
        self.pos = {a: i for i, a in enumerate(self.ids)}
        self.names = [f"{a[:8]}.jpg" for a in self.ids]
        self.types = np.array([(types or {}).get(a, "IMAGE") for a in self.ids], dtype="U5")
        self.taken = np.array([(taken or {}).get(a, "2024-01-01") for a in self.ids], dtype="U10")
        self.live = np.ones(len(self.ids), dtype=bool)
        self.indexed = np.array([a in (indexed if indexed is not None else self.ids) for a in self.ids], dtype=bool)

    def asset_vector(self, asset_id):
        if asset_id not in self.pos:
            raise ValueError("That photo is not in the Search+ index yet.")
        return ["like:" + asset_id]


class FakeSPEngine:
    """Scores are canned per text (or "like:<id>"): {key: {asset_id: score}}."""

    def __init__(self, view, scores):
        self._view, self.scores, self.text_calls = view, scores, 0

    def model_name(self):
        return "PE-test"

    def text_vector(self, text):
        self.text_calls += 1
        return [text]

    def view(self):
        return self._view

    def best(self, view, vector):
        import numpy as np
        sc = self.scores.get(vector[0], {})
        return np.array([sc.get(a, -1.0) for a in view.ids], dtype=np.float32)


class TestSmartAlbumsOnSearchPlus(WebCase):
    def setUp(self):
        super().setUp()
        self.backend = FakeThemeBackend({"fnaf": {self.ids[i]: 0.5 for i in range(10)}})   # Immich would add all ten
        self.backend.eligible_ids = lambda **kw: set(self.ids[2:])                           # e.g. a people filter
        self.httpd.RequestHandlerClass.theme_backend = self.backend
        view = FakeSPView(self.ids[:10], types={self.ids[1]: "VIDEO"}, indexed=self.ids[:9])
        self.engine = FakeSPEngine(view, {
            "fnaf": {self.ids[i]: round(0.24 - i * 0.01, 2) for i in range(10)},          # 0.24 .. 0.15
            "plush": {self.ids[i]: 0.20 for i in (0, 2, 3)},
            f"like:{self.ids[0]}": {self.ids[i]: round(1.0 - i * 0.05, 2) for i in range(10)},
        })
        self.httpd.RequestHandlerClass.theme_sp_engine = self.engine
        self.addCleanup(setattr, self.httpd.RequestHandlerClass, "theme_sp_engine", None)

    def post(self, action, body):
        return request(self.base + f"/api/themes/{action}", method="POST", body=body)[1]

    def members(self, name):
        album = self.fake.album_by_name(name)
        return sorted(self.fake.album_members[album["id"]]) if album else []

    def test_preview_uses_the_searchplus_scores(self):
        data = self.post("preview", {"theme": {"description": "fnaf", "engine": "searchplus"}, "limit": 3})
        self.assertEqual(data["engine"], "searchplus")
        self.assertEqual([i["id"] for i in data["items"]], self.ids[:3])
        self.assertEqual(data["items"][0]["score"], 0.24)
        self.assertEqual(data["counts"]["total"], 9)            # ids[9] is not in the Search+ index
        self.assertEqual(data["counts"]["0.200"], 5)

    def test_run_adds_what_passes_the_cutoff_and_caches_the_vector(self):
        t = self.post("save", {"theme": {"description": "fnaf", "name": "FNAF+", "cutoff": 0.21,
                                         "engine": "searchplus"}})["theme"]
        self.assertEqual(t["engine"], "searchplus")
        res = self.post("run", {"ids": [t["id"]]})["results"][0]
        self.assertEqual(res["added"], 4)                       # 0.24 0.23 0.22 0.21
        self.assertEqual(self.members("FNAF+"), sorted(self.ids[:4]))
        self.post("run", {"ids": [t["id"]]})
        self.assertEqual(self.engine.text_calls, 1)            # cached in the smart album

    def test_filters_terms_media_and_database_filters(self):
        spec = {"description": "fnaf", "engine": "searchplus", "media": "IMAGE", "people": [self.ids[0]],
                "all_of": ["plush"], "cutoff": 0.15}
        data = self.post("preview", {"theme": spec, "limit": 50})
        # plush >= 0.15 only for ids 0, 2, 3; the database filter drops 0 and 1; ids[1] is a video
        self.assertEqual([i["id"] for i in data["items"]], [self.ids[2], self.ids[3]])

    def test_like_a_photo_and_top_n(self):
        t = self.post("save", {"theme": {"source": "like", "like": self.ids[0], "name": "Like0", "mode": "top",
                                         "limit": 3, "engine": "searchplus"}})["theme"]
        self.post("run", {"ids": [t["id"]]})
        self.assertEqual(self.members("Like0"), sorted(self.ids[:3]))

    def search(self, body):
        return request(self.base + "/api/search", method="POST", body={"engine": "searchplus", **body})[1]

    def test_search_tab_with_the_searchplus_model(self):
        data = self.search({"query": "fnaf", "limit": 3})
        self.assertEqual(data["engine"], "searchplus")
        self.assertEqual([a["id"] for a in data["assets"]], self.ids[:3])
        self.assertEqual(data["assets"][0]["score"], 0.24)
        self.assertNotIn("total", data)

    def test_like_this_skips_the_photo_itself_and_skip_list(self):
        data = self.search({"like": self.ids[0], "limit": 3, "skipIds": [self.ids[1]]})
        self.assertEqual([a["id"] for a in data["assets"]], [self.ids[2], self.ids[3], self.ids[4]])

    def test_search_filters_that_searchplus_cannot_do_are_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/search", method="POST",
                    body={"engine": "searchplus", "query": "fnaf", "filters": {"city": "Paris"}})
        self.assertEqual(ctx.exception.code, 400)

    def test_unknown_engine_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            request(self.base + "/api/themes/save", method="POST", body={"theme": {"description": "x", "engine": "nope"}})
        self.assertEqual(ctx.exception.code, 400)


class TestSearchPlus(WebCase):
    def setUp(self):
        super().setUp()
        from immich_organizer import searchplus as sp
        from tests import test_searchplus as t
        self.sp = sp
        ids = self.ids
        # asset ids from the fake Immich, pictured as simple words
        self.catalog = [
            {"id": ids[0], "type": "IMAGE", "taken": "2026-01-05", "name": "a.jpg", "preview": "beach", "original": "", "duration_ms": 0},
            {"id": ids[1], "type": "IMAGE", "taken": "2026-01-04", "name": "b.jpg", "preview": "dog", "original": "", "duration_ms": 0},
            {"id": ids[2], "type": "VIDEO", "taken": "2026-01-03", "name": "c.mp4", "preview": "car|beach+dog", "original": "", "duration_ms": 5000},
        ]
        self.store = sp.Store(Path(self.tmp.name) / "sp")
        self.service = t.FakeService()
        self.indexer = sp.Indexer(self.store, self.service, catalog=lambda: self.catalog, frames=t.fake_frames)
        self.httpd.RequestHandlerClass.searchplus_parts = (self.store, self.service, self.indexer)
        self.backend = FakeThemeBackend({"dog": {ids[1]: 0.2, ids[3]: 0.15}, "like:" + ids[1]: {ids[1]: 1.0, ids[4]: 0.5}})
        self.httpd.RequestHandlerClass.theme_backend = self.backend
        self.addCleanup(setattr, self.httpd.RequestHandlerClass, "searchplus_parts", None)

    def post(self, action, body):
        return request(self.base + f"/api/searchplus/{action}", method="POST", body=body)[1]

    def build(self):
        self.post("settings", {"changes": {"keep_updated": False}})
        data = self.post("index", {"action": "start"})
        self.assertTrue(data["settings"]["indexing"])
        self.indexer.thread.join(10)

    def test_status_build_and_search(self):
        data = request(self.base + "/api/searchplus")[1]
        self.assertEqual(data["counts"]["indexed"], 0)
        self.assertEqual(data["service"]["container"], "stopped")
        self.build()
        data = request(self.base + "/api/searchplus")[1]
        self.assertEqual((data["counts"]["indexed"], data["counts"]["frames"]), (3, 4))
        self.assertEqual(data["indexer"]["state"], "done")

        res = self.post("search", {"text": "dog"})
        self.assertEqual([a["id"] for a in res["assets"]][:2], [self.ids[1], self.ids[2]])
        self.assertEqual(res["assets"][0]["name"], "b.jpg")
        res = self.post("search", {"text": "dog", "media": "VIDEO"})
        self.assertEqual([a["id"] for a in res["assets"]], [self.ids[2]])
        res = self.post("search", {"like": self.ids[1], "limit": 1})
        self.assertEqual([a["id"] for a in res["assets"]], [self.ids[2]])      # the photo itself is left out

    def test_compare_with_immich(self):
        self.build()
        res = self.post("search", {"text": "dog", "compare": True})
        self.assertEqual([a["id"] for a in res["immich"]["assets"]], [self.ids[1], self.ids[3]])
        self.assertEqual(res["immich"]["model"], "model-a")
        self.assertEqual(res["immich"]["overlap"], 1)
        res = self.post("search", {"like": self.ids[1], "compare": True})
        self.assertEqual([a["id"] for a in res["immich"]["assets"]], [self.ids[4]])

    def test_errors_pause_unload_and_reset(self):
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.post("search", {})
        self.assertEqual(err.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.post("search", {"like": self.ids[1]})                       # not indexed yet
        self.assertEqual(err.exception.code, 400)
        self.service.ready = mock.Mock(side_effect=self.sp.ServiceDown("loading"))
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.post("search", {"text": "dog"})
        self.assertEqual(err.exception.code, 503)
        del self.service.ready
        self.build()
        data = self.post("unload", {})
        self.assertTrue(self.service.stopped)
        self.assertFalse(data["settings"]["indexing"])
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.post("index", {"action": "reset"})
        self.assertEqual(err.exception.code, 400)
        data = self.post("index", {"action": "clear"})
        self.assertEqual(data["counts"]["failed"], 0)
        data = self.post("index", {"action": "reset", "confirm": "reset"})
        self.assertEqual(data["counts"]["indexed"], 0)
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.post("settings", {"changes": {"video_frames": 99}})
        self.assertEqual(err.exception.code, 400)

    def test_gpu_busy_is_a_503_with_its_own_message(self):
        message = "The GPU is in use by the AI Tagger \u2014 pause it to use Search+"
        self.service.ready = mock.Mock(side_effect=self.sp.GpuBusy(message))
        with self.assertRaises(urllib.error.HTTPError) as err:
            self.post("search", {"text": "dog"})
        self.assertEqual(err.exception.code, 503)
        self.assertEqual(json.loads(err.exception.read())["error"], message)


class TestAiTagger(WebCase):
    """The /api/aitagger* routes, over the fake Immich and the fake model containers."""

    def setUp(self):
        super().setUp()
        from immich_organizer import aitagger as at
        from immich_organizer import searchplus as sp
        from tests import test_aitagger as t
        self.at, self.sp, self.t = at, sp, t
        self.services = t.FakeServices()
        self.store = at.Store(Path(self.tmp.name) / "at")
        self.addCleanup(self.store.conn.close)
        self.catalog = t.catalog_for(self.ids)
        self.indexer = at.Indexer(self.store, self.services, client=self.client, catalog=lambda: self.catalog,
                                  frames=t.fake_frames)
        self.indexer.DROP_WAIT = 0.01
        self.addCleanup(self.indexer.stop, 5)
        self.httpd.RequestHandlerClass.aitagger_parts = (self.store, self.services, self.indexer)
        self.addCleanup(setattr, self.httpd.RequestHandlerClass, "aitagger_parts", None)

    def post(self, action, body=None):
        return request(self.base + f"/api/aitagger/{action}", method="POST", body=body or {})[1]

    def get(self, path):
        return request(self.base + path)[1]

    def refused(self, call, code):
        with self.assertRaises(urllib.error.HTTPError) as err:
            call()
        self.assertEqual(err.exception.code, code)
        return json.loads(err.exception.read())["error"]

    def build(self, pause=False, **settings):
        """Tag everything through the routes (and, with ``pause``, leave tagging switched off afterwards)."""
        self.post("settings", {"changes": {"keep_updated": False, **settings}})
        self.post("index", {"action": "start"})
        self.indexer.thread.join(30)
        self.assertFalse(self.indexer.running())
        if pause:
            self.post("index", {"action": "pause"})

    def description(self, i):
        return (self.fake.by_id[self.ids[i]].get("exifInfo") or {}).get("description") or ""

    def test_every_route_needs_the_token(self):
        for path in ("/api/aitagger", "/api/aitagger/assets", "/api/aitagger/sample"):
            self.refused(lambda: request(self.base + path, token=None), 401)
        for action in ("settings", "index", "load", "unload", "preview", "apply", "reprocess", "remove"):
            self.refused(lambda: request(self.base + f"/api/aitagger/{action}", method="POST", body={}, token=None), 401)

    def test_the_old_tag_routes_are_still_gone_and_unknown_ones_are_404(self):
        for path in ("/api/tags", "/api/describer", "/api/aitagger/nothing"):
            self.refused(lambda: request(self.base + path), 404)
        self.refused(lambda: self.post("nothing"), 404)

    def test_status_has_the_contract_shape(self):
        data = self.get("/api/aitagger")
        self.assertEqual(data["settings"], self.at.DEFAULTS)
        self.assertEqual(data["limits"], {"video_frames": [1, 8], "batch_size": [1, 64],
                                          "vram_gb": list(self.at.VRAM_GB_LIMITS), "wd_strictness": [0.2, 0.95],
                                          "pixai_strictness": [0.2, 0.95], "ram_strictness": [0.2, 0.95],
                                          "e621_strictness": [0.2, 0.95], "max_tags": [5, 100],
                                          "unload_after": [1, 60], "check_every": [1, 60]})
        self.assertEqual((data["settings"]["unload_after"], data["settings"]["check_every"]), (2, 1))
        self.assertEqual(data["limits"]["vram_gb"], [5, 8])                              # four taggers: 5-8 GB, default 6
        self.assertEqual(data["settings"]["vram_gb"], 6)
        for gone in ("describe", "instructions", "language", "vlm_parallel"):       # the describer's settings are gone
            self.assertNotIn(gone, data["settings"])
            self.assertNotIn(gone, data["limits"])
        self.assertEqual(data["settingsVersion"], 1)
        self.assertEqual(data["counts"], {"assets": 0, "images": 0, "videos": 0, "processed": 0, "pending": 0, "queued": 0,
                                          "outdated": 0, "failed": 0, "retrying": 0, "cleared": 0, "excluded": 0,
                                          "catalogAt": None})
        self.assertEqual(data["indexer"], {"state": "stopped", "detail": "", "error": None, "running": False,
                                           "ratePerMin": None, "etaMinutes": None})
        self.assertEqual(set(data["service"]), {"tagger", "gpu", "searchplusRunning", "exclusive"})      # no "vlm"
        self.assertEqual(data["unload"], {"loaded": False, "idleSeconds": None, "unloadInSeconds": None,    # a stand-in that
                                          "rule": None, "busy": False})                                      # can't say: not loaded
        self.assertEqual(data["service"]["tagger"]["status"], "down")
        self.assertEqual(data["service"]["gpu"], {"totalGb": 24, "usedGb": 1.0})
        self.assertFalse(data["service"]["searchplusRunning"])
        self.assertIs(data["service"]["exclusive"], False)                      # shipped: they share the card
        with mock.patch.object(self.sp, "AITAGGER_EXCLUSIVE", False):
            self.assertIs(self.get("/api/aitagger")["service"]["exclusive"], False)
        self.assertEqual(data["models"], {"wd": "wd-eva02-large-tagger-v3", "pixai": "pixai-tagger-v1.0",
                                          "ram": "RAM++ (swin-large)", "e621": "Hydra 3.5 (e621)"})
        self.assertEqual(list(data["models"]), ["wd", "pixai", "ram", "e621"])          # in registry order
        self.assertEqual((data["settings"]["use_ram"], data["settings"]["ram_strictness"]), (True, 0.5))
        self.assertEqual((data["settings"]["use_e621"], data["settings"]["e621_strictness"]), (True, 0.5))
        self.assertEqual(data["failures"], [])
        self.assertEqual(set(data["reprocessKeys"]), {"retag", "full"})
        self.assertIn("rules", data["reprocessKeys"]["retag"])
        self.assertIn("vocabulary", data["reprocessKeys"]["retag"])
        self.assertEqual(sorted(data["reprocessKeys"]["full"]),
                         ["use_e621", "use_pixai", "use_ram", "use_wd", "video_frames"])
        self.assertEqual(sorted(k for k in data["reprocessKeys"]["retag"] if k.endswith("_strictness")),
                         ["e621_strictness", "pixai_strictness", "ram_strictness", "wd_strictness"])

    def test_every_tagger_in_the_status_has_its_settings_and_limits(self):
        data = self.get("/api/aitagger")
        for key in data["models"]:
            self.assertIn(f"use_{key}", data["settings"])
            self.assertIn(f"{key}_strictness", data["settings"])
            self.assertEqual(data["limits"][f"{key}_strictness"], [0.2, 0.95])
            self.assertIn(f"use_{key}", data["reprocessKeys"]["full"])
            self.assertIn(f"{key}_strictness", data["reprocessKeys"]["retag"])

    def test_a_fifth_tagger_appears_in_the_status_and_works_through_the_routes(self):
        self.t.with_extra(self)
        data = self.get("/api/aitagger")
        self.assertEqual(list(data["models"]), ["wd", "pixai", "ram", "e621", "extra"])
        self.assertEqual(data["models"]["extra"], "extra-tagger-test")
        self.assertEqual((data["settings"]["use_extra"], data["settings"]["extra_strictness"]), (False, 0.5))
        self.assertEqual(data["limits"]["extra_strictness"], [0.2, 0.95])
        self.assertIn("use_extra", data["reprocessKeys"]["full"])
        self.assertIn("extra_strictness", data["reprocessKeys"]["retag"])
        for changes in ({"use_extra": "yes"}, {"extra_strictness": 1.5}, {"extra_strictness": 0.19},
                        {"extra_strictness": "0.5"}):
            self.refused(lambda: self.post("settings", {"changes": changes}), 400)
        data = self.post("settings", {"changes": {"use_extra": True, "extra_strictness": 0.4}})
        self.assertEqual((sorted(data["changed"]), data["suggest"]), (["extra_strictness", "use_extra"], "full"))
        data = self.post("settings", {"changes": {"extra_strictness": 0.45}})
        self.assertEqual((data["changed"], data["suggest"]), (["extra_strictness"], "retag"))
        self.catalog[0]["preview"] = self.t.PHOTO + "|extra:lake=0.9,wave=0.3|erating:general=0.9"
        got = self.post("preview", {"id": self.ids[0]})
        self.assertEqual(set(got["models"]), {"wd", "pixai", "ram", "e621", "extra", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in got["models"]["extra"]}, {"lake": True, "wave": False})
        self.assertEqual(self.services.tag_models, [["wd", "pixai", "ram", "e621", "extra"]])
        self.assertEqual({t["tag"]: t["source"] for t in got["tags"]}["lake"], "extra")

    def test_the_fifth_tagger_is_gone_from_the_status_when_it_is_removed_again(self):
        saved = list(self.at.TAGGERS)
        self.addCleanup(self.at.configure_taggers, saved)
        self.at.configure_taggers([*saved, self.t.EXTRA])
        self.assertIn("extra", self.get("/api/aitagger")["models"])
        self.at.configure_taggers(saved)
        data = self.get("/api/aitagger")
        self.assertEqual(list(data["models"]), ["wd", "pixai", "ram", "e621"])
        self.assertNotIn("use_extra", data["settings"])
        self.refused(lambda: self.post("settings", {"changes": {"use_extra": True}}), 400)

    def test_ram_works_through_the_routes_and_its_noise_stays_out(self):
        self.catalog[0]["preview"] = self.t.RAM_PHOTO
        got = self.post("preview", {"id": self.ids[0]})
        self.assertEqual(set(got["models"]), {"wd", "pixai", "ram", "e621", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in got["models"]["ram"]},
                         {"sea": True, "lake": True, "screenshot": True, "wave": False})        # no "image", no "catch"
        sources = {t["tag"]: t["source"] for t in got["tags"]}
        self.assertEqual((sources["lake"], sources["screenshot"], sources["sea"]), ("ram", "ram", "ram"))
        self.assertNotIn("image", sources)
        self.assertNotIn("catch", sources)
        self.assertEqual(self.services.tag_models, [["wd", "pixai", "ram", "e621"]])
        data = self.post("settings", {"changes": {"use_ram": False}})
        self.assertEqual((data["changed"], data["suggest"]), (["use_ram"], "full"))
        got = self.post("preview", {"id": self.ids[0]})
        self.assertEqual((got["models"]["ram"], self.services.tag_models[-1]), ([], ["wd", "pixai", "e621"]))
        self.assertNotIn("lake", {t["tag"] for t in got["tags"]})

    def test_hydra_works_through_the_routes_and_its_noise_and_characters_follow_the_settings(self):
        self.catalog[0]["preview"] = self.t.E621_PHOTO
        got = self.post("preview", {"id": self.ids[0]})
        self.assertEqual(set(got["models"]), {"wd", "pixai", "ram", "e621", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in got["models"]["e621"]},
                         {"anthro": True, "fur": True, "wolf": True, "canine": True, "fenrir": True,
                          "norse mythology": True, "tail": False, "fox": False, "loki": False})     # no "mammal"
        sources = {t["tag"]: t["source"] for t in got["tags"]}
        self.assertEqual((sources["anthro"], sources["wolf"], sources["fenrir"], sources["norse mythology"]), ("e621",) * 4)
        self.assertNotIn("mammal", sources)
        self.assertEqual(self.services.tag_models, [["wd", "pixai", "ram", "e621"]])
        self.assertEqual(got["models"]["rating"]["general"], 0.9)                        # Hydra has no rating to add
        # its own strictness, checked like the others
        for changes in ({"use_e621": "yes"}, {"e621_strictness": 0.19}, {"e621_strictness": 0.96}, {"e621_strictness": "0.5"}):
            self.refused(lambda: self.post("settings", {"changes": changes}), 400)
        data = self.post("settings", {"changes": {"e621_strictness": 0.25}})
        self.assertEqual((data["changed"], data["suggest"]), (["e621_strictness"], "retag"))
        got = self.post("preview", {"id": self.ids[0]})
        self.assertTrue({"tail", "fox", "loki"} <= {t["tag"] for t in got["tags"]})       # 0.3, 0.25 and 0.3 pass 0.25
        # the character switch takes its characters and series away, not its species
        self.post("settings", {"changes": {"character_tags": False}})
        got = self.post("preview", {"id": self.ids[0]})
        names = {t["tag"] for t in got["tags"]}
        self.assertEqual(names & {"fenrir", "loki", "norse mythology", "miku"}, set())
        self.assertTrue({"wolf", "canine", "fox", "anthro"} <= names)
        self.assertEqual(self.services.tag_models[-1], ["wd", "pixai", "ram", "e621"])
        data = self.post("settings", {"changes": {"use_e621": False}})
        self.assertEqual((data["changed"], data["suggest"]), (["use_e621"], "full"))
        got = self.post("preview", {"id": self.ids[0]})
        self.assertEqual((got["models"]["e621"], self.services.tag_models[-1]), ([], ["wd", "pixai", "ram"]))
        self.assertNotIn("wolf", {t["tag"] for t in got["tags"]})

    def test_tagging_everything_through_the_routes(self):
        self.build()
        data = self.get("/api/aitagger")
        self.assertTrue(data["settings"]["indexing"])
        self.assertEqual(data["indexer"]["state"], "done")
        c = data["counts"]
        self.assertEqual((c["assets"], c["images"], c["videos"], c["processed"], c["pending"], c["failed"]), (5, 4, 1, 3, 0, 2))
        self.assertEqual(sorted(f["name"] for f in data["failures"]), ["IMG_0003.jpg", "IMG_0004.jpg"])
        self.assertEqual(set(data["failures"][0]), {"id", "name", "error", "attempts", "at"})
        self.assertIn("[AI Tagger]", self.description(0))

    def test_settings_are_validated_saved_and_versioned(self):
        data = self.post("settings", {"changes": {"max_tags": 12, "vocabulary": "1girl -> woman", "blocked": ["Cat"]}})
        self.assertEqual((data["settings"]["max_tags"], data["settings"]["vocabulary"], data["settings"]["blocked"]),
                         (12, "1girl -> woman", ["cat"]))
        self.assertEqual(data["settingsVersion"], 2)
        self.assertEqual(sorted(data["changed"]), ["blocked", "max_tags", "vocabulary"])
        self.assertEqual(data["suggest"], "retag")                                   # renames re-apply to the stored scores
        self.assertEqual(data["queued"], 0)
        data = self.post("settings", {"changes": {"batch_size": 4, "indexing": True}})          # not content; indexing is ignored here
        self.assertEqual((data["settingsVersion"], data["settings"]["indexing"], data["suggest"]), (2, False, "none"))
        self.assertEqual(self.get("/api/aitagger")["settings"]["max_tags"], 12)
        for changes in ({"max_tags": 3}, {"video_frames": "6"}, {"wd_strictness": 2}, {"nope": 1},
                        {"use_ram": "yes"}, {"ram_strictness": 2}, {"ram_strictness": "0.5"}, {"pixai_strictness": 2},
                        {"use_pixai": "yes"}, {"use_e621": 1}, {"e621_strictness": 2},
                        {"wd_strictness": 0.1}, {"pixai_strictness": 0.05}, {"ram_strictness": 0.19},
                        {"vram_gb": self.at.VRAM_GB_LIMITS[1] + 1}, {"vram_gb": 4}, {"vram_gb": 3},
                        {"rules": [{"if_all": ["a"]}]}, {"vocabulary": 5}):
            self.refused(lambda: self.post("settings", {"changes": changes}), 400)
        for key, value in (("describe", False), ("describe", "false"), ("instructions", "Be brief."), ("language", "German"),
                           ("vlm_parallel", 4)):                                   # the describer's settings: unknown now
            message = self.refused(lambda: self.post("settings", {"changes": {key: value}}), 400)
            self.assertIn("Unknown", message)
            self.assertIn(key, message)
        for body in ({}, {"changes": {}}, {"changes": [1]}, {"changes": {"indexing": True, "max_tags": "x"}}):
            self.refused(lambda: self.post("settings", body), 400)
        self.assertEqual(self.get("/api/aitagger")["settingsVersion"], 2)                      # refused changes saved nothing
        data = self.post("settings", {"changes": {"use_pixai": False, "pixai_strictness": 0.7}})
        self.assertEqual((data["settings"]["use_pixai"], data["settings"]["pixai_strictness"]), (False, 0.7))
        self.assertEqual((sorted(data["changed"]), data["suggest"]), (["pixai_strictness", "use_pixai"], "full"))
        # RAM++'s settings (a v1 app sent them, and they are valid again) are accepted and checked like the others
        data = self.post("settings", {"changes": {"use_ram": False, "ram_strictness": 0.7}})
        self.assertEqual((data["settings"]["use_ram"], data["settings"]["ram_strictness"]), (False, 0.7))
        self.assertEqual((sorted(data["changed"]), data["suggest"]), (["ram_strictness", "use_ram"], "full"))
        data = self.post("settings", {"changes": {"ram_strictness": 0.6}})
        self.assertEqual((data["changed"], data["suggest"]), (["ram_strictness"], "retag"))
        data = self.post("settings", {"changes": {"use_e621": False, "e621_strictness": 0.7}})
        self.assertEqual((data["settings"]["use_e621"], data["settings"]["e621_strictness"]), (False, 0.7))
        self.assertEqual((sorted(data["changed"]), data["suggest"]), (["e621_strictness", "use_e621"], "full"))
        data = self.post("settings", {"changes": {"e621_strictness": 0.2, "vram_gb": 5}})        # the new lower limits
        self.assertEqual((data["settings"]["e621_strictness"], data["settings"]["vram_gb"]), (0.2, 5))

    def test_the_vocabulary_takes_combinations_and_refuses_unreadable_lines_with_the_line_number(self):
        text = "# renames and combinations\n1girl -> woman\nfurry + human -> human on anthro\nanthro | furry -> furry art\n" \
               "1girl + 1boy -> couple, -solo\na + !b -> c\na -> +b"
        data = self.post("settings", {"changes": {"vocabulary": text}})
        self.assertEqual(data["settings"]["vocabulary"], text)                       # kept as typed
        self.assertEqual((data["changed"], data["suggest"]), (["vocabulary"], "retag"))
        self.assertEqual(data["settingsVersion"], 2)
        for bad, message in [("1girl -> woman\na + b | c -> d", "Line 2: use + or |, not both"),
                             ("# note\n\nthe lake house", 'Line 3: write it as "tags -> result"'),
                             ("a | !b -> c", "Line 1: ! can't be used with |"),
                             ("x -> y\nz + -> w", "Line 2: put a tag on each side of + or |"),
                             ("a + b -> c, -c", "Line 1: a tag can't be both added and removed"),
                             ("a ->", "Line 1: there is nothing after ->")]:
            self.assertIn(message, self.refused(lambda: self.post("settings", {"changes": {"vocabulary": bad}}), 400))
        message = self.refused(lambda: self.post("settings", {"changes": {"vocabulary": "a | b + c -> d\nok -> fine\nnonsense"}}), 400)
        self.assertIn("Line 1: use + or |, not both", message)
        self.assertIn("Line 3: ", message)                                           # every bad line is named
        self.assertIn("at most 20000", self.refused(
            lambda: self.post("settings", {"changes": {"vocabulary": "# " + "x" * 20000}}), 400))
        data = self.get("/api/aitagger")
        self.assertEqual((data["settings"]["vocabulary"], data["settingsVersion"]), (text, 2))           # refused: nothing saved
        self.assertEqual(self.get("/api/aitagger")["settings"]["vocabulary"], text)

    def test_the_preview_trace_names_typed_combinations_by_line(self):
        self.post("settings", {"changes": {"rules": [{"if_all": ["girl"], "add": ["happy"]}],
                                           "vocabulary": "# summer\ngirl + beach -> summer, -solo\ndog | cat -> pet"}})
        data = self.post("preview", {"id": self.ids[0]})
        self.assertEqual(data["rules"], [{"rule": 0, "added": ["happy"], "removed": []},
                                         {"rule": "line 2", "added": ["summer"], "removed": ["solo"]}])
        tags = {t["tag"]: t["source"] for t in data["tags"]}
        self.assertEqual((tags["summer"], tags["happy"]), ("rule", "rule"))
        self.assertNotIn("solo", tags)
        self.assertNotIn("pet", tags)
        self.assertEqual(self.description(0), "")                                    # a preview writes nothing

    def test_settings_can_reprocess_the_old_results(self):
        self.build(pause=True)
        data = self.post("settings", {"changes": {"max_tags": 5}, "reprocess": "retag", "scope": "outdated"})
        self.assertEqual((data["queued"], data["suggest"], data["counts"]["queued"], data["counts"]["outdated"]), (3, "retag", 3, 3))
        self.post("index", {"action": "start"})
        self.indexer.thread.join(30)
        data = self.get("/api/aitagger")
        self.assertEqual((data["counts"]["queued"], data["counts"]["outdated"]), (0, 0))
        self.assertEqual(self.services.tag_calls, [6])                                            # the retag used no GPU
        for body, why in [({"changes": {"max_tags": 6}, "reprocess": "everything"}, "reprocess"),
                          ({"changes": {"max_tags": 6}, "reprocess": "retag", "scope": "some"}, "scope")]:
            self.assertIn(why, self.refused(lambda: self.post("settings", body), 400))
        self.assertIn("none, retag or full", self.refused(lambda: self.post("settings", {"changes": {"max_tags": 6}, "reprocess": 3}), 400))
        self.assertEqual(self.get("/api/aitagger")["settings"]["max_tags"], 5)                    # nothing was saved
        data = self.post("settings", {"changes": {"max_tags": 7}, "reprocess": "none"})
        self.assertEqual(data["queued"], 0)
        self.post("index", {"action": "pause"})
        data = self.post("settings", {"changes": {"max_tags": 8}, "reprocess": "describe", "scope": "all"})   # an old app: a retag
        self.assertEqual((data["queued"], {q["mode"] for q in self.store.queue()}), (3, {"retag"}))
        data = self.post("settings", {"changes": {"use_pixai": False}, "reprocess": "full", "scope": "all"})
        self.assertEqual((data["queued"], data["suggest"]), (3, "full"))
        self.assertEqual(data["counts"]["queued"], 3)

    def test_index_actions(self):
        self.post("settings", {"changes": {"keep_updated": False}})
        data = self.post("index", {"action": "start"})
        self.assertTrue(data["settings"]["indexing"])
        self.indexer.thread.join(30)
        data = self.post("index", {"action": "pause"})
        self.assertFalse(data["settings"]["indexing"])
        self.assertEqual(data["counts"]["retrying"], 2)
        data = self.post("index", {"action": "clear"})
        self.assertEqual((data["counts"]["failed"], data["counts"]["cleared"]), (0, 2))
        data = self.post("index", {"action": "retry"})
        self.assertEqual((data["counts"]["failed"], data["counts"]["pending"]), (0, 2))
        self.refused(lambda: self.post("index", {"action": "reset"}), 400)
        self.refused(lambda: self.post("index", {}), 400)

    def test_load_and_unload(self):
        data = self.post("load")
        self.assertEqual(self.services.loads, 1)
        self.assertEqual(data["indexer"]["state"], "stopped")                   # loading does not start tagging
        self.assertFalse(data["settings"]["indexing"])
        self.post("index", {"action": "start"})
        data = self.post("unload")
        self.assertEqual(self.services.unloads, 1)
        self.assertFalse(data["settings"]["indexing"])
        self.indexer.thread.join(10)
        self.assertFalse(self.indexer.running())

    def test_loading_failures_are_503(self):
        self.services.load = mock.Mock(side_effect=self.at.ServiceDown("could not start immich_aitagger: no such image"))
        self.assertIn("no such image", self.refused(lambda: self.post("load"), 503))

    def test_preview_writes_nothing_and_apply_writes(self):
        data = self.post("preview", {"id": self.ids[0]})
        self.assertEqual((data["id"], data["name"], data["type"], data["captures"], data["written"]),
                         (self.ids[0], "IMG_0000.jpg", "IMAGE", 1, False))
        self.assertEqual(set(data), {"id", "name", "type", "captures", "frames", "models", "rules", "tags",
                                     "block", "currentDescription", "newDescription", "written"})     # no describer section
        self.assertEqual(set(data["models"]), {"wd", "pixai", "ram", "e621", "rating"})
        self.assertEqual({t["tag"] for t in data["models"]["pixai"]}, {"beach", "sea", "wave"})
        self.assertEqual((data["models"]["ram"], data["models"]["e621"]), ([], []))          # PHOTO: nothing for RAM++ or Hydra
        self.assertEqual(data["block"], "[AI Tagger]\nTags: girl, beach, miku, solo, sea, rating: general\n[/AI Tagger]")
        self.assertEqual(data["tags"][0], {"tag": "girl", "score": 0.9, "source": "wd"})
        self.assertEqual(data["currentDescription"], "")
        self.assertEqual(data["newDescription"], data["block"])
        self.assertEqual(self.description(0), "")
        data = self.post("apply", {"id": self.ids[0]})
        self.assertTrue(data["written"])
        self.assertEqual(self.description(0), data["newDescription"])
        self.assertEqual(self.get("/api/aitagger")["counts"]["processed"], 1)

    def test_preview_errors(self):
        self.assertIn("asset id", self.refused(lambda: self.post("preview", {"id": "not-an-id"}), 400))
        self.refused(lambda: self.post("preview", {}), 400)
        self.assertIn("library list", self.refused(lambda: self.post("preview", {"id": "00000000-0000-0000-0000-0000000000ee"}), 404))
        self.assertIn("Could not read", self.refused(lambda: self.post("preview", {"id": self.ids[3]}), 400))
        self.services.down = True
        message = self.refused(lambda: self.post("preview", {"id": self.ids[0]}), 503)
        self.assertIn("Try again in a minute", message)
        self.refused(lambda: self.post("apply", {"id": self.ids[0]}), 503)

    def test_immich_not_answering_is_502_not_503(self):
        self.fake.fail_next[f"/api/assets/{self.ids[0]}"] = 5
        self.refused(lambda: self.post("apply", {"id": self.ids[0]}), 502)

    def test_reprocess_by_ids_tag_outdated_and_all(self):
        self.build(pause=True)
        got = self.post("reprocess", {"scope": "ids", "ids": self.ids[:2], "mode": "describe"})   # an old app: a retag
        self.assertEqual((got["queued"], got["counts"]["queued"]), (2, 2))
        self.assertEqual({q["id"]: q["mode"] for q in self.store.queue()}, {self.ids[0]: "retag", self.ids[1]: "retag"})
        got = self.post("reprocess", {"scope": "ids", "ids": [self.ids[0]], "mode": "full"})
        self.assertEqual(got["counts"]["queued"], 2)
        self.assertEqual({q["id"]: q["mode"] for q in self.store.queue()}, {self.ids[0]: "full", self.ids[1]: "retag"})
        got = self.post("reprocess", {"scope": "tag", "tag": "Dog", "mode": "retag"})
        self.assertEqual(got["queued"], 2)                                  # the dog photo and the video (already queued: kept)
        self.assertEqual({q["id"]: q["mode"] for q in self.store.queue()}[self.ids[1]], "retag")
        self.assertEqual({q["id"]: q["mode"] for q in self.store.queue()}[self.ids[2]], "retag")
        got = self.post("reprocess", {"scope": "all", "mode": "retag"})
        self.assertEqual((got["queued"], got["counts"]["queued"]), (3, 3))
        self.assertEqual(self.post("reprocess", {"scope": "outdated", "mode": "retag"})["queued"], 0)     # nothing is outdated
        self.post("settings", {"changes": {"max_tags": 9}})
        self.assertEqual(self.post("reprocess", {"scope": "outdated", "mode": "retag"})["queued"], 3)
        for body in ({"scope": "ids", "mode": "retag"}, {"scope": "ids", "ids": [], "mode": "retag"},
                     {"scope": "ids", "ids": ["x"], "mode": "retag"}, {"scope": "tag", "mode": "retag"},
                     {"scope": "everything", "mode": "retag"}, {"scope": "all"}, {"scope": "all", "mode": "again"}, {}):
            self.refused(lambda: self.post("reprocess", body), 400)

    def test_reprocessing_starts_the_indexer_only_when_tagging_is_on(self):
        self.build()                                                       # tagging is on; the thread has finished
        self.assertFalse(self.indexer.running())
        self.post("reprocess", {"scope": "all", "mode": "retag"})
        self.indexer.thread.join(30)
        self.assertEqual(self.get("/api/aitagger")["counts"]["queued"], 0)       # it ran
        self.post("index", {"action": "pause"})
        got = self.post("reprocess", {"scope": "all", "mode": "retag"})
        self.assertFalse(self.indexer.running())
        self.assertEqual((got["queued"], got["counts"]["queued"]), (3, 3))       # paused: it waits in the queue

    def test_remove_strips_the_text_and_can_exclude(self):
        self.build()
        self.fake.by_id[self.ids[0]]["exifInfo"]["description"] = "Mine.\n\n" + self.description(0)
        data = self.post("remove", {"ids": [self.ids[0], self.ids[1]], "exclude": True})
        self.assertEqual((data["removed"], data["excluded"], data["failed"]), (2, 2, []))
        self.assertEqual((self.description(0), self.description(1)), ("Mine.", ""))
        self.assertEqual(self.get("/api/aitagger")["counts"]["excluded"], 2)
        self.assertEqual(self.get("/api/aitagger")["counts"]["processed"], 1)
        data = self.post("remove", {"ids": [self.ids[2]]})
        self.assertEqual((data["removed"], data["excluded"]), (1, 0))
        self.assertEqual(self.description(2), "")
        for body in ({}, {"ids": []}, {"ids": "x"}, {"ids": ["x"]}, {"ids": [self.ids[0]], "exclude": "yes"}):
            self.refused(lambda: self.post("remove", body), 400)

    def test_asset_list_filters_and_pages(self):
        self.build()
        data = self.get("/api/aitagger/assets")
        self.assertEqual((data["total"], data["page"]), (3, 1))
        self.assertEqual([i["id"] for i in data["items"]], self.ids[:3])
        first = data["items"][0]
        self.assertEqual(set(first), {"id", "name", "type", "taken", "tags", "settingsVersion", "processedAt"})   # no description
        self.assertEqual((first["name"], first["type"], first["settingsVersion"]), ("IMG_0000.jpg", "IMAGE", 1))
        self.assertEqual(first["tags"][:2], ["girl", "beach"])
        self.assertEqual(data["tags"][0], {"tag": "rating: general", "count": 3})
        self.assertEqual(dict((t["tag"], t["count"]) for t in data["tags"])["dog"], 2)
        self.assertEqual([i["id"] for i in self.get("/api/aitagger/assets?tag=dog")["items"]], self.ids[1:3])
        self.assertEqual([i["id"] for i in self.get("/api/aitagger/assets?q=beach")["items"]], [self.ids[0]])
        self.assertEqual(self.get("/api/aitagger/assets?q=img_000")["total"], 3)                 # the file name
        self.assertEqual(self.get("/api/aitagger/assets?q=nice")["total"], 0)                    # there is no description to search
        self.assertEqual(self.get("/api/aitagger/assets?outdated=1")["total"], 0)
        self.post("settings", {"changes": {"max_tags": 9}})
        self.assertEqual(self.get("/api/aitagger/assets?outdated=1")["total"], 3)
        paged = self.get("/api/aitagger/assets?size=2&page=2")
        self.assertEqual((paged["total"], len(paged["items"]), paged["page"]), (3, 1, 2))
        self.assertEqual(len(self.get("/api/aitagger/assets?size=1")["items"]), 1)
        self.assertEqual(self.get("/api/aitagger/assets?size=100000")["total"], 3)           # the size is capped, not refused
        self.refused(lambda: self.get("/api/aitagger/assets?page=x"), 400)

    def test_sample_gives_a_random_asset_of_the_kind_asked(self):
        got = self.get("/api/aitagger/sample?type=VIDEO")
        self.assertEqual(got, {"id": self.ids[2], "name": "IMG_0002.jpg", "type": "VIDEO"})
        for _ in range(5):
            self.assertEqual(self.get("/api/aitagger/sample?type=IMAGE")["type"], "IMAGE")
        self.assertIn(self.get("/api/aitagger/sample")["id"], self.ids[:5])
        self.refused(lambda: self.get("/api/aitagger/sample?type=AUDIO"), 400)
        self.catalog = []
        self.store.sync_catalog([])
        self.assertIn("no video", self.refused(lambda: self.get("/api/aitagger/sample?type=VIDEO"), 404))

    def test_the_gpu_being_busy_is_a_503_with_the_message_as_is(self):
        message = "The GPU is in use by the AI Tagger — pause it to use Search+"
        self.services.ensure_ready = mock.Mock(side_effect=self.at.GpuBusy(message))
        self.assertEqual(self.refused(lambda: self.post("preview", {"id": self.ids[0]}), 503), message)


class SearchService:
    """Built in ``setUp``: the real ``searchplus.Service`` (so its interactive-use and in-flight bookkeeping is the real
    one) with the network, docker and the model replaced. A text is a word of ``test_searchplus.BASIS``."""

    @staticmethod
    def make(idle=30, running=True):
        import base64
        import numpy as np
        from immich_organizer import searchplus as sp
        from tests import test_searchplus as t

        def enc(v):
            return base64.b64encode(np.asarray(v, dtype="<f2").tobytes()).decode()

        class Fake(sp.Service):
            def __init__(self):
                super().__init__()
                self.idle, self.running, self.health_calls, self.state_calls, self.stops = idle, running, 0, 0, 0

            def ready(self, wait=0, progress=None, stop=None):
                return {"status": "ok", "model": "fake", "dim": t.DIM}

            def health(self, timeout=3):
                self.health_calls += 1
                if not self.running:
                    return None
                return {"status": "ok", "model": "fake", "dim": t.DIM, "idleSeconds": self.idle, "idleExitMinutes": 20}

            def container_state(self, fresh=False):
                self.state_calls += 1
                return "running" if self.running else "stopped"

            def stop(self):
                self.stops += 1
                was, self.running = self.running, False
                return was

            def _post(self, path, body, timeout=600):
                if path == "/embed/text":
                    return {"vectors": [enc(t.unit(t.BASIS[x])) for x in body["texts"]], "dim": t.DIM}
                words = [base64.b64decode(b) for b in body["images"]]
                vecs = [enc(t.unit(np.sum([t.BASIS[w] for w in b.decode().split("+")], axis=0))) for b in words]
                return {"vectors": vecs, "errors": [None] * len(vecs), "dim": t.DIM}

        return Fake()


class TestSearchPlusUnload(WebCase):
    """GET /api/searchplus: the ``unload`` object and the two new settings; what counts as interactive use of the model."""

    def setUp(self):
        super().setUp()
        from immich_organizer import searchplus as sp
        from tests import test_searchplus as t
        self.sp, self.t = sp, t
        ids = self.ids
        self.catalog = [
            {"id": ids[0], "type": "IMAGE", "taken": "2026-01-05", "name": "a.jpg", "preview": "beach", "original": "", "duration_ms": 0},
            {"id": ids[1], "type": "IMAGE", "taken": "2026-01-04", "name": "b.jpg", "preview": "dog", "original": "", "duration_ms": 0},
            {"id": ids[2], "type": "VIDEO", "taken": "2026-01-03", "name": "c.mp4", "preview": "car|beach+dog", "original": "", "duration_ms": 5000},
        ]
        self.store = sp.Store(Path(self.tmp.name) / "sp")
        self.addCleanup(self.store.conn.close)
        self.service = SearchService.make()
        self.indexer = sp.Indexer(self.store, self.service, catalog=lambda: self.catalog, frames=t.fake_frames)
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)
        self.httpd.RequestHandlerClass.searchplus_parts = (self.store, self.service, self.indexer)
        self.addCleanup(setattr, self.httpd.RequestHandlerClass, "searchplus_parts", None)
        self.backend = FakeThemeBackend({})
        self.httpd.RequestHandlerClass.theme_backend = self.backend
        self.addCleanup(setattr, self.httpd.RequestHandlerClass, "theme_backend", None)

    def status(self):
        return request(self.base + "/api/searchplus")[1]

    def post(self, action, body):
        return request(self.base + f"/api/searchplus/{action}", method="POST", body=body)[1]

    def refused(self, call):
        with self.assertRaises(urllib.error.HTTPError) as err:
            call()
        self.assertEqual(err.exception.code, 400)
        return json.loads(err.exception.read())["error"]

    def build(self):
        """Index everything (the run ends with the card freed, so the model server idles for long meanwhile), then put the
        model server back as it was."""
        self.service.idle = 500
        self.post("settings", {"changes": {"keep_updated": False}})
        self.indexer.start()
        self.indexer.thread.join(30)
        self.assertFalse(self.indexer.running())
        self.service.idle, self.service.running, self.service.stops = 30, True, 0

    def test_the_status_has_the_unload_object_and_the_settings(self):
        data = self.status()
        self.assertEqual(data["unload"], {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 1170, "rule": "server",
                                          "busy": False})            # nothing indexing: only the server's own exit
        self.assertEqual((data["settings"]["unload_after"], data["settings"]["check_every"]), (2, 1))
        self.assertEqual(data["limits"], {"video_frames": [1, 8], "unload_after": [1, 60], "check_every": [1, 60]})
        self.assertEqual(data["service"]["container"], "running")

    def test_not_loaded_is_said_plainly(self):
        self.service.running = False
        self.assertEqual(self.status()["unload"], {"loaded": False, "idleSeconds": None, "unloadInSeconds": None,
                                                   "rule": None, "busy": False})

    def test_while_the_indexer_waits_the_countdown_is_the_short_rule(self):
        self.post("settings", {"changes": {"keep_updated": True}})
        self.indexer.start()
        self.assertTrue(self.t.wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.assertEqual(self.status()["unload"], {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 90,
                                                   "rule": "after-work", "busy": False})
        self.service.idle = 130                                     # and the panel really stops it
        self.assertTrue(self.t.wait_for(lambda: self.service.stops == 1))
        self.assertFalse(self.status()["unload"]["loaded"])

    def test_the_status_is_cheap_one_health_look_and_one_remembered_state_per_poll(self):
        self.status()
        before = (self.service.health_calls, self.service.state_calls)
        for _ in range(3):
            self.status()
        self.assertEqual(self.service.health_calls - before[0], 3)      # one /health per poll, none extra for ``unload``
        self.assertEqual(self.service.state_calls - before[1], 3)       # (the real one remembers its answer for 5 s)

    def test_the_new_settings_are_checked_by_the_route(self):
        data = self.post("settings", {"changes": {"unload_after": 5, "check_every": 10}})
        self.assertEqual((data["settings"]["unload_after"], data["settings"]["check_every"]), (5, 10))
        for changes in ({"unload_after": 0}, {"unload_after": 61}, {"check_every": 0}, {"check_every": "soon"},
                        {"unload_after": 5, "check_every": 99}):
            with self.subTest(changes=changes):
                self.refused(lambda: self.post("settings", {"changes": changes}))
        self.assertEqual(self.status()["settings"]["unload_after"], 5)

    def test_a_text_search_is_interactive_use_a_like_search_is_not(self):
        self.build()
        self.assertIsNone(self.service.interactive_age())
        self.post("search", {"like": self.ids[1]})                       # stored vectors only: the model is not used
        self.assertIsNone(self.service.interactive_age())
        self.service.idle = 130
        self.post("search", {"text": "dog"})
        self.assertIsNotNone(self.service.interactive_age())
        self.assertEqual(self.status()["unload"]["rule"], "server")      # (indexer not running here)

    def test_after_a_search_the_short_rule_waits_for_the_servers_own_idle_time(self):
        self.build()
        self.post("search", {"text": "dog"})
        self.post("settings", {"changes": {"keep_updated": True}})
        self.indexer.start()
        self.assertTrue(self.t.wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.service.idle = 0                                            # the search was the server's last request
        time.sleep(0.3)
        unload = self.status()["unload"]
        self.assertEqual(unload["rule"], "interactive")                  # not the 90 s of the short rule
        self.assertGreater(unload["unloadInSeconds"], 1100)
        self.service.idle = 130                                          # the short rule alone would stop it now
        time.sleep(0.3)
        self.assertEqual(self.service.stops, 0)                          # a search a moment ago: within the grace

    def test_the_search_tab_with_the_searchplus_model_is_interactive_use(self):
        self.build()
        self.assertIsNone(self.service.interactive_age())
        data = request(self.base + "/api/search", method="POST", body={"engine": "searchplus", "query": "dog", "limit": 3})[1]
        self.assertEqual(data["engine"], "searchplus")
        self.assertIsNotNone(self.service.interactive_age())

    def test_smart_albums_on_searchplus_are_interactive_use_too(self):
        self.build()
        self.assertIsNone(self.service.interactive_age())
        data = request(self.base + "/api/themes/preview", method="POST",
                       body={"theme": {"description": "dog", "engine": "searchplus"}, "limit": 3})[1]
        self.assertEqual(data["engine"], "searchplus")
        self.assertIsNotNone(self.service.interactive_age())

    def test_the_indexer_alone_never_counts_as_interactive_use(self):
        self.build()                                                     # it sent pictures to the model
        self.assertIsNone(self.service.interactive_age())


class TestAiTaggerUnload(WebCase):
    """GET /api/aitagger: the ``unload`` object and the two new settings; the Test card is interactive use."""

    def setUp(self):
        super().setUp()
        from immich_organizer import aitagger as at
        from tests import test_aitagger as t
        self.at, self.t = at, t
        self.services = t.IdleServices()
        self.store = at.Store(Path(self.tmp.name) / "at")
        self.addCleanup(self.store.conn.close)
        self.catalog = t.catalog_for(self.ids)
        self.indexer = at.Indexer(self.store, self.services, client=self.client, catalog=lambda: self.catalog,
                                  frames=t.fake_frames)
        self.indexer.DROP_WAIT = 0.01
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)
        self.httpd.RequestHandlerClass.aitagger_parts = (self.store, self.services, self.indexer)
        self.addCleanup(setattr, self.httpd.RequestHandlerClass, "aitagger_parts", None)

    def get(self, path="/api/aitagger"):
        return request(self.base + path)[1]

    def post(self, action, body=None):
        return request(self.base + f"/api/aitagger/{action}", method="POST", body=body or {})[1]

    def refused(self, call, code=400):
        with self.assertRaises(urllib.error.HTTPError) as err:
            call()
        self.assertEqual(err.exception.code, code)
        return json.loads(err.exception.read())["error"]

    def test_the_status_has_the_unload_object(self):
        self.services.idle = 40
        self.assertEqual(self.get()["unload"], {"loaded": True, "idleSeconds": 40, "unloadInSeconds": 1160, "rule": "server",
                                                "busy": False})
        self.services.running = False
        self.assertEqual(self.get()["unload"], {"loaded": False, "idleSeconds": None, "unloadInSeconds": None,
                                                "rule": None, "busy": False})

    def test_after_the_work_the_short_rule_counts_down_and_the_panel_stops_the_models(self):
        self.services.idle = 40
        self.post("settings", {"changes": {"keep_updated": True}})
        self.post("index", {"action": "start"})
        self.assertTrue(self.t.wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.assertEqual(self.get()["unload"], {"loaded": True, "idleSeconds": 40, "unloadInSeconds": 80,
                                                "rule": "after-work", "busy": False})
        self.services.idle = 125
        self.assertTrue(self.t.wait_for(lambda: self.services.unloads == 1))
        self.assertFalse(self.get()["unload"]["loaded"])

    def test_a_busy_tagger_or_a_request_in_flight_is_busy(self):
        self.services.busy_n = 1
        self.assertTrue(self.get()["unload"]["busy"])
        self.assertIsNone(self.get()["unload"]["unloadInSeconds"])
        self.services.busy_n, self.services.inflight_n = 0, 2
        self.assertTrue(self.get()["unload"]["busy"])

    def test_preview_and_apply_are_interactive_use(self):
        self.assertEqual(self.services.marks, 0)
        self.post("preview", {"id": self.ids[0]})
        self.assertEqual(self.services.marks, 2)                         # before the models run, and after
        self.post("apply", {"id": self.ids[0]})
        self.assertEqual(self.services.marks, 4)
        self.assertEqual(self.get()["unload"]["unloadInSeconds"], 1200 - self.services.idle)   # (indexer not running)
        self.post("settings", {"changes": {"keep_updated": True}})
        self.post("index", {"action": "start"})
        self.assertTrue(self.t.wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.services.idle = 125
        self.services.age = 600                                          # the Test card ten minutes ago
        time.sleep(0.3)
        self.assertEqual(self.services.unloads, 0)
        unload = self.get()["unload"]
        self.assertEqual((unload["rule"], unload["unloadInSeconds"]), ("interactive", 600))

    def test_the_indexer_alone_never_counts_as_interactive_use(self):
        self.post("settings", {"changes": {"keep_updated": False}})
        self.services.idle = 500
        self.post("index", {"action": "start"})
        self.indexer.thread.join(30)
        self.assertEqual(self.services.marks, 0)
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_the_new_settings_are_checked_by_the_route_and_make_nothing_outdated(self):
        data = self.post("settings", {"changes": {"unload_after": 5, "check_every": 10}})
        self.assertEqual((data["settings"]["unload_after"], data["settings"]["check_every"]), (5, 10))
        self.assertEqual((data["changed"], data["suggest"]), ([], "none"))
        self.assertEqual(data["settingsVersion"], 1)
        for changes in ({"unload_after": 0}, {"unload_after": 61}, {"check_every": 0}, {"check_every": 1.5},
                        {"unload_after": "2"}, {"unload_after": True}, {"unload_after": 5, "check_every": 99}):
            with self.subTest(changes=changes):
                self.refused(lambda: self.post("settings", {"changes": changes}))
        self.assertEqual(self.get()["settings"]["unload_after"], 5)
        self.assertEqual(self.get()["limits"]["unload_after"], [1, 60])
