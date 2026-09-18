import json
import os
import tempfile
import threading
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


if __name__ == "__main__":
    unittest.main()
