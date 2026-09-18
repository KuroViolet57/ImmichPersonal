import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from immich_organizer.cli import main
from immich_organizer.config import load_settings
from tests.fake_immich import API_KEY, FakeImmich


class CliCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich().start()
        self.addCleanup(self.fake.stop)
        self.ids = [a["id"] for a in self.fake.assets]

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "IMMICH_ORGANIZER_HOME": self.tmp.name,
            "IMMICH_URL": self.fake.url,
            "IMMICH_API_KEY": API_KEY,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

        self.rules_path = Path(self.tmp.name) / "rules.json"
        self.write_rules([{"name": "Mountains", "album": "Mountains", "query": "mountain", "limit": 4}])

    def write_rules(self, rules):
        self.rules_path.write_text(json.dumps({"version": 1, "rules": rules}))

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()


class TestValidate(CliCase):
    def test_valid_rules_pass(self):
        code, out = self.run_cli("validate", "-r", str(self.rules_path))
        self.assertEqual(code, 0)
        self.assertIn("1 rule(s)", out)

    def test_invalid_rules_report_the_field(self):
        self.write_rules([{"album": "A", "query": "x", "limit": -3}])
        code, out = self.run_cli("validate", "-r", str(self.rules_path))
        self.assertEqual(code, 2)
        self.assertIn("rules[0].limit", out)


class TestDoctorAndAlbums(CliCase):
    def test_doctor_reports_a_healthy_server(self):
        self.fake.smart_results["a photograph"] = self.ids[:1]
        self.fake.similar_results[self.ids[0]] = self.ids[1:2]
        code, out = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("authenticated as me@example.com", out)
        self.assertIn("smart search returns results", out)
        self.assertIn("similar-asset search works", out)

    def test_doctor_warns_when_embeddings_are_missing(self):
        code, out = self.run_cli("doctor")  # no canned smart results
        self.assertEqual(code, 1)
        self.assertIn("Smart Search", out)

    def test_albums_are_listed(self):
        self.fake.add_album("Trip", members=self.ids[:2])
        code, out = self.run_cli("albums")
        self.assertEqual(code, 0)
        self.assertIn("Trip", out)


class TestSearch(CliCase):
    def test_search_lists_matches_without_changing_anything(self):
        self.fake.smart_results["mountain"] = self.ids
        code, out = self.run_cli("search", "--query", "mountain", "--limit", "3")
        self.assertEqual(code, 0)
        self.assertIn("3 match(es)", out)
        self.assertEqual(self.fake.albums, {})

    def test_search_with_album_is_a_dry_run_by_default(self):
        self.fake.smart_results["mountain"] = self.ids
        code, out = self.run_cli("search", "--query", "mountain", "--limit", "2", "--album", "M")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(self.fake.albums, {})

    def test_search_apply_files_the_results(self):
        self.fake.smart_results["mountain"] = self.ids
        code, out = self.run_cli(
            "search", "--query", "mountain", "--limit", "2", "--album", "M", "--apply"
        )
        self.assertEqual(code, 0)
        self.assertIn("Added 2", out)
        album = self.fake.album_by_name("M")
        self.assertEqual(self.fake.album_members[album["id"]], self.ids[:2])

    def test_search_by_reference_asset(self):
        ref = self.ids[0]
        self.fake.similar_results[ref] = self.ids[1:4]
        code, out = self.run_cli("search", "--like", ref, "--limit", "10")
        self.assertEqual(code, 0)
        self.assertIn("3 match(es)", out)

    def test_html_preview_is_written(self):
        self.fake.smart_results["mountain"] = self.ids
        target = Path(self.tmp.name) / "preview.html"
        code, out = self.run_cli(
            "search", "--query", "mountain", "--limit", "2", "--html", str(target)
        )
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())
        body = target.read_text()
        self.assertIn("data:image/png;base64,", body)  # thumbnails are inlined
        self.assertIn("no similarity score", body)


class TestPlanApplyUndo(CliCase):
    def test_plan_reports_but_changes_nothing(self):
        self.fake.smart_results["mountain"] = self.ids
        code, out = self.run_cli("plan", "-r", str(self.rules_path), "-q")
        self.assertEqual(code, 0)
        self.assertIn("4 asset(s) would be added", out)
        self.assertIn("Nothing changed", out)
        self.assertEqual(self.fake.albums, {})

    def test_apply_needs_confirmation(self):
        self.fake.smart_results["mountain"] = self.ids
        with mock.patch("builtins.input", return_value="n"):
            code, out = self.run_cli("apply", "-r", str(self.rules_path), "-q")
        self.assertEqual(code, 0)
        self.assertIn("Aborted", out)
        self.assertEqual(self.fake.albums, {})

    def test_apply_with_yes_files_the_assets(self):
        self.fake.smart_results["mountain"] = self.ids
        code, out = self.run_cli("apply", "-r", str(self.rules_path), "-q", "-y")
        self.assertEqual(code, 0)
        self.assertIn("Added 4 asset(s)", out)
        album = self.fake.album_by_name("Mountains")
        self.assertEqual(self.fake.album_members[album["id"]], self.ids[:4])

    def test_apply_then_undo_round_trip(self):
        self.fake.smart_results["mountain"] = self.ids
        self.run_cli("apply", "-r", str(self.rules_path), "-q", "-y")
        album = self.fake.album_by_name("Mountains")
        self.assertEqual(len(self.fake.album_members[album["id"]]), 4)

        code, out = self.run_cli("undo", "-y")
        self.assertEqual(code, 0)
        self.assertIn("Removed 4", out)
        self.assertEqual(self.fake.album_members[album["id"]], [])

    def test_history_shows_the_run(self):
        self.fake.smart_results["mountain"] = self.ids
        self.run_cli("apply", "-r", str(self.rules_path), "-q", "-y")
        code, out = self.run_cli("history")
        self.assertEqual(code, 0)
        self.assertIn("Mountains: 4", out)

    def test_undo_with_nothing_recorded(self):
        code, out = self.run_cli("undo", "-y")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to undo", out)

    def test_apply_stops_when_a_rule_failed(self):
        self.write_rules([{"album": "Missing", "query": "mountain", "create_album": False}])
        self.fake.smart_results["mountain"] = self.ids
        code, out = self.run_cli("apply", "-r", str(self.rules_path), "-q", "-y")
        self.assertEqual(code, 1)
        self.assertIn("rule(s) failed", out)
        self.assertEqual(self.fake.albums, {})

    def test_only_runs_the_named_rule(self):
        self.write_rules([
            {"name": "ra", "album": "A", "query": "a", "limit": 2},
            {"name": "rb", "album": "B", "query": "b", "limit": 2},
        ])
        self.fake.smart_results["a"] = self.ids[:3]
        self.fake.smart_results["b"] = self.ids[3:6]
        code, out = self.run_cli("apply", "-r", str(self.rules_path), "-q", "-y", "--only", "rb")
        self.assertEqual(code, 0)
        self.assertIsNone(self.fake.album_by_name("A"))
        self.assertIsNotNone(self.fake.album_by_name("B"))


class TestConfiguration(CliCase):
    def test_missing_configuration_is_explained(self):
        with mock.patch.dict(os.environ, {"IMMICH_URL": "", "IMMICH_API_KEY": ""}, clear=False):
            os.environ.pop("IMMICH_URL")
            os.environ.pop("IMMICH_API_KEY")
            with self.assertRaises(SystemExit) as ctx:
                main(["albums"])
            self.assertIn("Not configured yet", str(ctx.exception))

    def test_setup_verifies_and_saves(self):
        code, out = self.run_cli("setup", "--server", self.fake.url, "--api-key", API_KEY)
        self.assertEqual(code, 0)
        self.assertIn("connected as me@example.com", out)
        saved = json.loads((Path(self.tmp.name) / "config.json").read_text())
        self.assertEqual(saved["api_key"], API_KEY)

    def test_setup_rejects_a_bad_key(self):
        code, out = self.run_cli("setup", "--server", self.fake.url, "--api-key", "wrong")
        self.assertEqual(code, 2)
        self.assertIn("rejected the API key", out)
        self.assertFalse((Path(self.tmp.name) / "config.json").exists())

    def test_cli_flags_beat_the_environment(self):
        code, out = self.run_cli("--api-key", "wrong", "albums")
        self.assertEqual(code, 2)
        self.assertIn("rejected the API key", out)

    def test_settings_redaction_hides_the_key(self):
        with mock.patch.dict(os.environ, {"IMMICH_API_KEY": "abcdefghijklmnop"}):
            self.assertEqual(load_settings().redacted()["api_key"], "abcd...mnop")


if __name__ == "__main__":
    unittest.main()
