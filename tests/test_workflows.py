import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from immich_organizer.cli import main
from immich_organizer.workflows import (
    ALBUM_METHOD,
    ENV_PLUGIN_KEY,
    FILTER_METHOD,
    REDACTED,
    SmartAlbumSpec,
    WorkflowError,
    build_payload,
    redact,
    resolve_plugin_api_key,
    trigger_warning,
)
from tests.fake_immich import API_KEY, FakeImmich

SECRET = "plugin-key-should-never-be-printed"


class TestSpec(unittest.TestCase):
    def test_needs_exactly_one_matcher(self):
        for kwargs in ({}, {"query": "a", "like_asset": "b"}):
            with self.assertRaises(WorkflowError):
                SmartAlbumSpec(name="n", album="A", **kwargs).validate()

    def test_rejects_an_unknown_trigger(self):
        with self.assertRaises(WorkflowError):
            SmartAlbumSpec(name="n", album="A", query="a", trigger="OnTuesdays").validate()

    def test_rejects_an_out_of_range_depth(self):
        with self.assertRaises(WorkflowError):
            SmartAlbumSpec(name="n", album="A", query="a", limit=99999).validate()

    def test_album_is_required(self):
        with self.assertRaises(WorkflowError):
            SmartAlbumSpec(name="n", album="  ", query="a").validate()


class TestKeyResolution(unittest.TestCase):
    def test_environment_beats_the_flag(self):
        with mock.patch.dict(os.environ, {ENV_PLUGIN_KEY: "from-env"}):
            key, source = resolve_plugin_api_key("from-flag", "configured")
        self.assertEqual(key, "from-env")
        self.assertIn(ENV_PLUGIN_KEY, source)

    def test_flag_is_used_but_flagged_as_visible(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            key, source = resolve_plugin_api_key("from-flag", "configured")
        self.assertEqual(key, "from-flag")
        self.assertIn("shell history", source)

    def test_configured_key_is_the_last_resort_and_says_so(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            key, source = resolve_plugin_api_key(None, "configured")
        self.assertEqual(key, "configured")
        self.assertIn("over-permissioned", source)

    def test_no_key_anywhere_explains_how_to_set_one(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(WorkflowError) as ctx:
                resolve_plugin_api_key(None, None)
        self.assertIn(ENV_PLUGIN_KEY, str(ctx.exception))


class TestPayload(unittest.TestCase):
    def _payload(self, **kwargs):
        spec = SmartAlbumSpec(name="Mountains", album="Mountains", query="a mountain", **kwargs)
        return build_payload(spec, SECRET)

    def test_two_steps_in_order(self):
        steps = self._payload()["steps"]
        self.assertEqual([s["method"] for s in steps], [FILTER_METHOD, ALBUM_METHOD])

    def test_filter_carries_the_query_and_key(self):
        config = self._payload()["steps"][0]["config"]
        self.assertEqual(config["query"], "a mountain")
        self.assertEqual(config["apiKey"], SECRET)
        self.assertNotIn("likeAssetId", config)

    def test_reference_photo_mode(self):
        spec = SmartAlbumSpec(name="n", album="A", like_asset="asset-1")
        config = build_payload(spec, SECRET)["steps"][0]["config"]
        self.assertEqual(config["likeAssetId"], "asset-1")
        self.assertNotIn("query", config)

    def test_album_step_lets_immich_create_the_album(self):
        config = self._payload()["steps"][1]["config"]
        self.assertEqual(config, {"albumIds": [], "albumName": "Mountains"})

    def test_default_trigger_is_the_reliable_one(self):
        self.assertEqual(self._payload()["trigger"], "AssetTagged")

    def test_inverse_is_only_set_when_asked(self):
        self.assertNotIn("inverse", self._payload()["steps"][0]["config"])
        self.assertTrue(self._payload(inverse=True)["steps"][0]["config"]["inverse"])

    def test_redaction_hides_the_key_and_keeps_the_rest(self):
        payload = self._payload()
        safe = redact(payload)
        self.assertEqual(safe["steps"][0]["config"]["apiKey"], REDACTED)
        self.assertEqual(safe["steps"][0]["config"]["query"], "a mountain")
        self.assertNotIn(SECRET, json.dumps(safe))
        # The original must not be mutated by redacting it.
        self.assertEqual(payload["steps"][0]["config"]["apiKey"], SECRET)


class TestTriggerWarning(unittest.TestCase):
    def test_reliable_trigger_has_no_warning(self):
        self.assertIsNone(trigger_warning("AssetTagged"))

    def test_upload_triggers_warn_about_embeddings(self):
        for trigger in ("AssetCreate", "AssetMetadataExtraction"):
            self.assertIn("embedding", trigger_warning(trigger))


class TestWorkflowCli(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich().start()
        self.addCleanup(self.fake.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "IMMICH_ORGANIZER_HOME": self.tmp.name,
            "IMMICH_URL": self.fake.url,
            "IMMICH_API_KEY": API_KEY,
            ENV_PLUGIN_KEY: SECRET,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(argv))
        return code, buf.getvalue()

    def _ready_server(self):
        self.fake.install_core_plugin()
        self.fake.install_smart_album_plugin()

    def test_create_refuses_when_the_plugin_is_missing(self):
        self.fake.install_core_plugin()  # core only: no content filter
        code, out = self.run_cli("workflow", "create", "--query", "a mountain",
                                 "--album", "Mountains", "--apply")
        self.assertEqual(code, 1)
        self.assertIn(FILTER_METHOD, out)
        self.assertIn("never runs", out)
        self.assertEqual(self.fake.workflows, [])

    def test_create_is_a_dry_run_by_default(self):
        self._ready_server()
        code, out = self.run_cli("workflow", "create", "--query", "a mountain", "--album", "Mountains")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", out)
        self.assertEqual(self.fake.workflows, [])

    def test_create_posts_the_workflow(self):
        self._ready_server()
        code, out = self.run_cli("workflow", "create", "--query", "a mountain",
                                 "--album", "Mountains", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.workflows), 1)
        created = self.fake.workflows[0]
        self.assertEqual(created["trigger"], "AssetTagged")
        self.assertEqual(created["steps"][0]["config"]["apiKey"], SECRET)
        self.assertEqual(created["steps"][1]["config"]["albumName"], "Mountains")

    def test_the_key_is_never_printed(self):
        self._ready_server()
        _, out = self.run_cli("workflow", "create", "--query", "a mountain", "--album",
                              "Mountains", "--show-payload", "--apply")
        self.assertNotIn(SECRET, out)
        self.assertIn(REDACTED, out)
        self.assertIn(ENV_PLUGIN_KEY, out)  # reports where the key came from

    def test_upload_trigger_warns(self):
        self._ready_server()
        _, out = self.run_cli("workflow", "create", "--query", "a mountain",
                              "--album", "M", "--trigger", "AssetCreate")
        self.assertIn("Warning", out)
        self.assertIn("embedding", out)

    def test_falls_back_to_the_configured_key_and_says_so(self):
        # Without a dedicated plugin key the tool's own key is used, since it
        # works -- but the output has to admit it is probably broader than
        # asset.read, which is all the plugin step needs.
        self._ready_server()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_PLUGIN_KEY)
            code, out = self.run_cli("workflow", "create", "--query", "a mountain",
                                     "--album", "A", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("over-permissioned", out)
        self.assertNotIn(API_KEY, out)
        self.assertEqual(self.fake.workflows[0]["steps"][0]["config"]["apiKey"], API_KEY)

    def test_query_and_like_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.run_cli("workflow", "create", "--query", "a", "--like", "x", "--album", "A")

    def test_list_shows_workflows(self):
        self._ready_server()
        self.run_cli("workflow", "create", "--query", "a mountain", "--album", "Mountains", "--apply")
        code, out = self.run_cli("workflow", "list")
        self.assertEqual(code, 0)
        self.assertIn("Mountains", out)
        self.assertIn(FILTER_METHOD, out)

    def test_list_on_a_server_without_workflows(self):
        self.fake.supports_workflows = False
        code, out = self.run_cli("workflow", "list")
        self.assertEqual(code, 1)
        self.assertIn("does not have Workflows", out)


if __name__ == "__main__":
    unittest.main()
