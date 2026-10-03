import unittest

from immich_organizer.client import (
    AuthError,
    ImmichClient,
    ImmichError,
    NotFoundError,
    normalise_base_url,
)
from tests.fake_immich import API_KEY, FakeImmich


class TestUrlNormalisation(unittest.TestCase):
    def test_variants_all_land_on_the_api_root(self):
        for given in (
            "http://localhost:2283",
            "http://localhost:2283/",
            "http://localhost:2283/api",
            "http://localhost:2283/api/",
            "localhost:2283",
        ):
            self.assertEqual(normalise_base_url(given), "http://localhost:2283/api")

    def test_https_and_subpath_are_preserved(self):
        self.assertEqual(
            normalise_base_url("https://photos.example.com/immich"),
            "https://photos.example.com/immich/api",
        )

    def test_empty_url_rejected(self):
        with self.assertRaises(ValueError):
            normalise_base_url("   ")


class TestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = FakeImmich().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()

    def setUp(self):
        self.fake.smart_results.clear()
        self.fake.similar_results.clear()
        self.fake.fail_next.clear()
        self.client = ImmichClient(self.fake.url, API_KEY, retries=0)

    def test_ping_and_identity(self):
        self.assertTrue(self.client.ping())
        self.assertEqual(self.client.me()["email"], "me@example.com")
        self.assertEqual(self.client.about()["version"], "v2.0.0")

    def test_bad_key_raises_auth_error(self):
        client = ImmichClient(self.fake.url, "wrong-key", retries=0)
        with self.assertRaises(AuthError) as ctx:
            client.me()
        self.assertIn("rejected the API key", str(ctx.exception))

    def test_missing_asset_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.client.get_asset("11111111-1111-4111-8111-111111111111")

    def test_unreachable_server_explains_itself(self):
        client = ImmichClient("http://127.0.0.1:1", "k", retries=0, timeout=2)
        with self.assertRaises(ImmichError) as ctx:
            client.ping()
        self.assertIn("Could not reach Immich", str(ctx.exception))

    def test_retries_recover_from_a_transient_5xx(self):
        self.fake.fail_next["/api/server/about"] = 1
        client = ImmichClient(self.fake.url, API_KEY, retries=2)
        self.assertEqual(client.about()["version"], "v2.0.0")

    def test_iter_smart_search_paginates(self):
        ids = [a["id"] for a in self.fake.assets]
        self.fake.smart_results["everything"] = ids
        got = list(self.client.iter_smart_search({"query": "everything"}, limit=25, page_size=10))
        self.assertEqual([a["id"] for a in got], ids[:25])

    def test_iter_smart_search_asks_for_the_same_page_size_every_time(self):
        # Immich's offset is (page - 1) * size: a smaller last page would start inside the one before it, return
        # repeats, and for a limit one over a whole page need one request for every result of the first
        ids = [a["id"] for a in self.fake.assets]
        self.fake.smart_results["everything"] = ids
        got = list(self.client.iter_smart_search({"query": "everything"}, limit=23, page_size=10))
        self.assertEqual([a["id"] for a in got], ids[:23])
        asked = [(b["page"], b["size"]) for kind, b in self.fake.searches if kind == "smart"]
        self.assertEqual(asked, [(1, 10), (2, 10), (3, 10)])
        self.fake.searches.clear()
        got = list(self.client.iter_smart_search({"query": "everything"}, limit=11, page_size=10))
        self.assertEqual([a["id"] for a in got], ids[:11])
        self.assertEqual(len(self.fake.searches), 2)

    def test_iter_smart_search_stops_when_results_run_out(self):
        ids = [a["id"] for a in self.fake.assets[:3]]
        self.fake.smart_results["few"] = ids
        got = list(self.client.iter_smart_search({"query": "few"}, limit=100, page_size=2))
        self.assertEqual(len(got), 3)

    def test_iter_smart_search_respects_a_zero_limit(self):
        self.fake.smart_results["x"] = [a["id"] for a in self.fake.assets]
        self.assertEqual(list(self.client.iter_smart_search({"query": "x"}, limit=0)), [])

    def test_similar_search_uses_query_asset_id(self):
        ref = self.fake.assets[0]["id"]
        self.fake.similar_results[ref] = [a["id"] for a in self.fake.assets[1:4]]
        got = list(self.client.iter_smart_search({"queryAssetId": ref}, limit=10))
        self.assertEqual(len(got), 3)

    def test_album_round_trip(self):
        album = self.client.create_album("Trip", description="d")
        ids = [a["id"] for a in self.fake.assets[:3]]
        added = self.client.add_assets_to_album(album["id"], ids)
        self.assertTrue(all(r["success"] for r in added))

        # Adding the same assets again is reported per-asset, not as a failure.
        again = self.client.add_assets_to_album(album["id"], ids)
        self.assertTrue(all(r["error"] == "duplicate" for r in again))

        removed = self.client.remove_assets_from_album(album["id"], ids)
        self.assertTrue(all(r["success"] for r in removed))

    def test_empty_id_lists_short_circuit(self):
        before = len(self.fake.requests)
        self.assertEqual(self.client.add_assets_to_album("any", []), [])
        self.client.update_assets([], isFavorite=True)
        self.assertEqual(len(self.fake.requests), before)

    def test_thumbnail_returns_bytes_and_type(self):
        payload, content_type = self.client.thumbnail(self.fake.assets[0]["id"])
        self.assertTrue(payload.startswith(b"\x89PNG"))
        self.assertEqual(content_type, "image/png")

    def test_thumbnail_rejects_bad_size(self):
        with self.assertRaises(ValueError):
            self.client.thumbnail(self.fake.assets[0]["id"], size="enormous")

    def test_update_asset_sends_only_the_given_fields(self):
        asset_id = self.fake.assets[0]["id"]
        self.fake.asset_puts.clear()
        out = self.client.update_asset(asset_id, description="A nice day.")
        self.assertEqual(self.fake.asset_puts, [(asset_id, {"description": "A nice day."})])
        self.assertEqual(out["exifInfo"]["description"], "A nice day.")
        self.assertEqual(self.client.get_asset(asset_id)["exifInfo"]["description"], "A nice day.")
        self.assertIn(("PUT", f"/api/assets/{asset_id}"), self.fake.requests)
        with self.assertRaises(NotFoundError):
            self.client.update_asset("00000000-0000-0000-0000-0000000000ff", description="x")

    def test_tags_are_upserted_attached_listed_and_detached(self):
        asset_id = self.fake.assets[1]["id"]
        tags = self.client.upsert_tags(["AI/beach", "AI/girl"])
        self.assertEqual([t["value"] for t in tags], ["AI/beach", "AI/girl"])
        self.assertEqual([t["name"] for t in tags], ["beach", "girl"])
        self.assertEqual(self.client.upsert_tags(["AI/beach"])[0]["id"], tags[0]["id"])      # again: the same tag
        self.assertTrue({"AI", "AI/beach", "AI/girl"} <= {t["value"] for t in self.client.list_tags()})
        ids = [t["id"] for t in tags]
        self.assertEqual(self.client.tag_assets(ids, [asset_id]), {"count": 2})
        self.assertEqual(self.client.tag_assets(ids, [asset_id]), {"count": 0})              # already attached
        self.assertEqual(sorted(t["value"] for t in self.client.get_asset(asset_id)["tags"]), ["AI/beach", "AI/girl"])
        self.assertEqual(self.client.untag_assets(ids[0], [asset_id]), [{"id": asset_id, "success": True}])
        self.assertEqual([t["value"] for t in self.client.get_asset(asset_id)["tags"]], ["AI/girl"])
        self.assertIn(("DELETE", f"/api/tags/{ids[0]}/assets"), self.fake.requests)

    def test_tag_calls_with_nothing_to_do_make_no_request(self):
        before = len(self.fake.requests)
        self.assertEqual(self.client.upsert_tags([]), [])
        self.assertEqual(self.client.tag_assets([], ["a"]), {"count": 0})
        self.assertEqual(self.client.tag_assets(["t"], []), {"count": 0})
        self.assertEqual(self.client.untag_assets("t", []), [])
        self.assertEqual(len(self.fake.requests), before)


if __name__ == "__main__":
    unittest.main()
