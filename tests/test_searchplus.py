import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from immich_organizer import searchplus as sp

DIM = 4
BASIS = {"beach": [1, 0, 0, 0], "dog": [0, 1, 0, 0], "cat": [0, 0, 1, 0], "car": [0, 0, 0, 1]}


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


class FakeService:
    """Frames are bytes like b"beach" or b"beach+dog"; their vector is the (normalised) sum of the words."""

    def __init__(self):
        self.calls = 0
        self.down = False
        self.stopped = False

    def ready(self, wait=0, progress=None, stop=None):
        if self.down:
            raise sp.ServiceDown("down")
        return {"status": "ok", "model": "fake", "dim": DIM}

    def embed_images(self, images):
        if self.down:
            raise sp.ServiceDown("down")
        self.calls += 1
        vecs, errs = [], []
        for b in images:
            if b == b"broken":
                vecs.append(None)
                errs.append("UnidentifiedImageError")
            else:
                vecs.append(unit(np.sum([BASIS[w] for w in b.decode().split("+")], axis=0)))
                errs.append(None)
        return vecs, errs

    def embed_text(self, texts):
        return np.stack([unit(BASIS[t]) for t in texts])

    def stop(self):
        self.stopped = True
        return True

    def health(self, timeout=3):
        return None

    def container_state(self):
        return "stopped"


CATALOG = [
    {"id": "a1", "type": "IMAGE", "taken": "2026-01-05 10:00", "name": "a1.jpg", "preview": "beach", "original": "", "duration_ms": 0},
    {"id": "a2", "type": "IMAGE", "taken": "2026-01-04 10:00", "name": "a2.jpg", "preview": "dog", "original": "", "duration_ms": 0},
    {"id": "v1", "type": "VIDEO", "taken": "2026-01-03 10:00", "name": "v1.mp4", "preview": "car|car|beach+dog|car", "original": "", "duration_ms": 9000},
    {"id": "a3", "type": "IMAGE", "taken": "2025-12-01 10:00", "name": "a3.jpg", "preview": "cat", "original": "", "duration_ms": 0},
    {"id": "bad", "type": "IMAGE", "taken": "2025-11-01 10:00", "name": "bad.jpg", "preview": "MISSING", "original": "", "duration_ms": 0},
    {"id": "brk", "type": "IMAGE", "taken": "2025-10-01 10:00", "name": "brk.jpg", "preview": "broken", "original": "", "duration_ms": 0},
]


def fake_frames(item, n):
    if item["preview"] == "MISSING":
        raise FileNotFoundError("no preview image")
    parts = item["preview"].split("|")
    return [p.encode() for p in parts], "video" if item["type"] == "VIDEO" else "image"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"IMMICH_ORGANIZER_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = sp.Store(Path(self.tmp.name) / "sp")
        self.service = FakeService()
        self.catalog = list(CATALOG)
        self.indexer = sp.Indexer(self.store, self.service, catalog=lambda: self.catalog, frames=fake_frames)
        self.indexer.IMAGES_PER_REQUEST = 3

    def build(self):
        sp.save_settings({"keep_updated": False})
        self.indexer.start()
        self.indexer.thread.join(10)
        self.assertFalse(self.indexer.running())


class TestIndexAndSearch(Base):
    def test_builds_everything_and_records_failures(self):
        self.build()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        c = self.store.counts()
        self.assertEqual((c["assets"], c["indexed"], c["frames"]), (6, 4, 7))     # 3 photos + 4 video frames
        # failures show at once (not as "still to do"), and are tried again later, 3 times in all
        self.assertEqual((c["failed"], c["retrying"], c["pending"]), (2, 2, 0))
        self.assertEqual(self.store.todo(10), [])
        with mock.patch.object(sp, "RETRY_AFTER", -5):
            self.assertEqual({t["id"] for t in self.store.todo(10)}, {"bad", "brk"})
            self.indexer.start()
            self.indexer.thread.join(10)
            self.assertEqual(self.store.todo(10), [])
        c = self.store.counts()
        self.assertEqual((c["failed"], c["retrying"], c["pending"]), (2, 0, 0))
        errors = {f["id"]: f["error"] for f in self.store.failures()}
        self.assertIn("no preview", errors["bad"])
        self.assertIn("Unidentified", errors["brk"])
        self.assertEqual(self.store.model, "fake")
        # Clear list hides them; Try again brings them back
        self.store.clear_failed()
        c = self.store.counts()
        self.assertEqual((c["failed"], c["cleared"], c["pending"]), (0, 2, 0))
        self.assertEqual(self.store.failures(), [])
        self.store.retry_failed()
        self.assertEqual(self.store.counts()["pending"], 2)

    def test_search_ranks_filters_and_uses_the_best_video_frame(self):
        self.build()
        q = self.service.embed_text(["dog"])[0]
        res = sp.search(self.store, q)
        self.assertEqual([r["id"] for r in res[:2]], ["a2", "v1"])            # the video has one dog frame
        self.assertAlmostEqual(res[0]["score"], 1.0, places=2)
        self.assertAlmostEqual(res[1]["score"], 0.7071, places=2)
        self.assertEqual(res[1]["type"], "VIDEO")
        self.assertEqual(res[0]["date"], "2026-01-04")
        self.assertEqual([r["id"] for r in sp.search(self.store, q, media="VIDEO")], ["v1"])
        self.assertEqual({r["id"] for r in sp.search(self.store, q, after="2026-01-04")}, {"a1", "a2"})
        self.assertEqual({r["id"] for r in sp.search(self.store, q, before="2025-12-31")}, {"a3"})
        self.assertEqual(len(sp.search(self.store, q, limit=2)), 2)
        # "more like this photo" leaves the photo itself out
        like = self.store.view().asset_vector("a1")
        self.assertEqual(sp.search(self.store, like, skip="a1")[0]["id"], "v1")

    def test_deleted_assets_disappear_and_new_ones_are_picked_up(self):
        self.build()
        self.catalog = [r for r in CATALOG if r["id"] != "a2"] + [
            {"id": "n1", "type": "IMAGE", "taken": "2026-02-01", "name": "n1.jpg", "preview": "dog", "original": "",
             "duration_ms": 0}]
        self.store.sync_catalog(self.catalog)
        q = self.service.embed_text(["dog"])[0]
        self.assertNotIn("a2", [r["id"] for r in sp.search(self.store, q)])
        self.assertEqual([t["id"] for t in self.store.todo(10)][:1], ["n1"])
        self.indexer.last_sync = None
        self.build()
        self.assertEqual(sp.search(self.store, q)[0]["id"], "n1")

    def test_model_server_down_is_not_the_photos_fault(self):
        self.service.down = True
        self.build()
        self.assertEqual(self.indexer.state, "error")
        self.assertEqual(self.store.counts()["failed"], 0)
        self.assertFalse(self.store.conn.execute("select count(*) from failed").fetchone()[0])

    def test_refuses_to_mix_models_and_start_over_clears(self):
        self.build()
        with self.assertRaises(RuntimeError):
            self.store.adopt_model("other", 8)
        self.store.reset()
        self.assertEqual(self.store.counts()["indexed"], 0)
        self.store.adopt_model("other", 8)
        self.assertEqual(self.store.dim, 8)

    def test_half_written_row_is_dropped(self):
        self.build()
        with open(self.store.vec_path, "ab") as fh:
            fh.write(b"\x00\x01\x02")
        store = sp.Store(self.store.folder)
        self.assertEqual(store.row_count(), 7)
        self.assertEqual(store.vec_path.stat().st_size, 7 * DIM * 2)

    def test_a_slow_file_does_not_hold_up_the_others(self):
        def frames(item, n):
            if item["id"] == "a1":          # newest, so first in line
                time.sleep(0.5)
            return fake_frames(item, n)

        self.indexer.frames = frames
        self.build()
        order = [r[0] for r in self.store.conn.execute("select id from indexed order by first_row")]
        self.assertEqual(order[-1], "a1")

    def test_files_that_are_not_media_are_not_retried(self):
        def frames(item, n):
            if item["id"] == "brk":
                raise ValueError("not a picture")
            return fake_frames(item, n)

        self.indexer.frames = frames
        self.build()
        c = self.store.counts()
        self.assertEqual((c["failed"], c["retrying"]), (2, 1))             # "bad" (no preview yet) is retried

    def test_reading_while_writing_from_other_threads(self):
        self.store.sync_catalog(CATALOG)
        self.store.adopt_model("fake", DIM)
        stop, errors = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    self.store.counts()
                    self.store.failures()
                    self.store.view()
                    assert self.store.dim == DIM and self.store.model == "fake"
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(300):
            self.store.append(f"a{i % 3 + 1}", "image", [unit(BASIS["dog"])])
            if i % 50 == 0:
                self.store.sync_catalog(CATALOG)
        stop.set()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_try_again_wakes_an_indexer_that_is_up_to_date(self):
        sp.save_settings({"keep_updated": True})
        self.indexer.start()
        for _ in range(100):
            if self.indexer.state == "done":
                break
            time.sleep(0.05)
        self.assertEqual(self.indexer.state, "done")
        self.store.retry_failed()                # "bad" and "brk" may be tried again right away
        self.indexer.start()
        for _ in range(100):
            if self.store.conn.execute("select max(attempts) from failed").fetchone()[0] == 2:
                break
            time.sleep(0.05)
        self.assertEqual(self.store.conn.execute("select max(attempts) from failed").fetchone()[0], 1)
        self.indexer.stop(wait_s=5)
        self.assertFalse(self.indexer.running())

    def test_pause_stops_the_thread(self):
        gate = threading.Event()

        def slow_frames(item, n):
            gate.wait(5)
            return fake_frames(item, n)

        self.indexer.frames = slow_frames
        self.indexer.start()
        time.sleep(0.2)
        self.indexer.stop()
        gate.set()
        self.indexer.thread.join(10)
        self.assertFalse(self.indexer.running())
        self.assertEqual(self.indexer.state, "stopped")


class TestProgress(Base):
    def test_time_left_counts_video_frames(self):
        self.store.sync_catalog(CATALOG)                     # 5 photos + 1 video still to do
        now = [1000.0]
        self.indexer.clock = lambda: now[0]
        for i in range(10):                                  # 10 items, 20 pictures in the last minute
            self.indexer.done_times.append((940.0 + i * 6, 2))
        self.indexer.state = "running"
        st = self.indexer.status()
        self.assertAlmostEqual(st["ratePerMin"], 10.0, delta=1.5)
        # 5 photos + 1 video x 4 frames = 9 pictures at ~20 a minute -> under a minute
        self.assertEqual(st["etaMinutes"], 0)


class TestSettingsAndFrames(Base):
    def test_settings_are_checked(self):
        self.assertEqual(sp.load_settings()["video_frames"], 4)
        self.assertEqual(sp.save_settings({"video_frames": 6})["video_frames"], 6)
        with self.assertRaises(ValueError):
            sp.save_settings({"video_frames": 20})
        with self.assertRaises(ValueError):
            sp.save_settings({"nope": 1})

    def test_prepare_frames_uses_the_preview_and_shrinks_it(self):
        from PIL import Image
        import io
        path = Path(self.tmp.name) / "p.jpg"
        Image.new("RGB", (1600, 1200), "red").save(path)
        frames, kind = sp.prepare_frames({"id": "x", "type": "IMAGE", "preview": str(path), "original": ""}, 4)
        self.assertEqual(kind, "image")
        with Image.open(io.BytesIO(frames[0])) as im:
            self.assertEqual(max(im.size), sp.FRAME_SIDE)
        with self.assertRaises(FileNotFoundError):
            sp.prepare_frames({"id": "x", "type": "IMAGE", "preview": "/nope.jpg", "original": ""}, 4)

    def test_without_a_preview_the_picture_itself_is_used(self):
        from PIL import Image
        path = Path(self.tmp.name) / "big.png"
        Image.new("RGB", (1080, 1920), "blue").save(path)
        frames, kind = sp.prepare_frames({"id": "x", "type": "IMAGE", "preview": "", "original": str(path)}, 4)
        self.assertEqual((kind, len(frames)), ("image", 1))

    def test_files_that_are_not_media_get_a_clear_reason(self):
        code = Path(self.tmp.name) / "windowCount.ts"
        code.write_text("import {Request} from '../lib/request';\n")
        with self.assertRaises(ValueError) as err:
            sp.prepare_frames({"id": "x", "type": "VIDEO", "preview": "", "original": str(code), "duration_ms": 0}, 4)
        self.assertIn("TypeScript", str(err.exception))
        svg = Path(self.tmp.name) / "font.svg"
        svg.write_text('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>')
        with self.assertRaises(ValueError) as err:
            sp.prepare_frames({"id": "y", "type": "IMAGE", "preview": "", "original": str(svg)}, 4)
        self.assertIn("SVG", str(err.exception))
        with self.assertRaises(FileNotFoundError):
            sp.prepare_frames({"id": "z", "type": "IMAGE", "preview": "", "original": "/nope.jpg"}, 4)
        from PIL import Image
        cut = Path(self.tmp.name) / "cut.png"
        Image.effect_noise((400, 400), 60).convert("RGB").save(cut)
        data = cut.read_bytes()
        cut.write_bytes(data[:len(data) // 2])                 # an incomplete copy
        with self.assertRaises(ValueError) as err:
            sp.prepare_frames({"id": "c", "type": "IMAGE", "preview": "", "original": str(cut)}, 4)
        self.assertIn("cut short", str(err.exception))

    def test_prepare_frames_spreads_over_a_gif(self):
        from PIL import Image
        path = Path(self.tmp.name) / "a.gif"
        frames = [Image.new("RGB", (200, 200), c) for c in ("red", "green", "blue", "white", "black", "yellow")]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=100)
        got, kind = sp.prepare_frames({"id": "g", "type": "IMAGE", "preview": "", "original": str(path)}, 3)
        self.assertEqual((kind, len(got)), ("animation", 3))


class TestGpuBusy(Base):
    """While the AI Tagger's container holds the graphics card (and the two are set to take turns), Search+ must not
    start its model server. v3 has no describer container: the check is on ``immich_aitagger`` only."""

    def docker(self, running: dict):
        """A stand-in for subprocess.run: `docker inspect` answers from ``running``; other calls are recorded."""
        self.docker_calls = []

        def run(cmd, **kwargs):
            self.docker_calls.append(list(cmd))
            if cmd[:2] == ["docker", "inspect"]:
                if cmd[-1] not in running:
                    return mock.Mock(returncode=1, stdout="", stderr="Error: No such object")
                return mock.Mock(returncode=0, stdout="true\n" if running[cmd[-1]] else "false\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        return mock.patch.object(sp.subprocess, "run", side_effect=run)

    def test_gpu_busy_is_a_service_down_with_the_contract_message(self):
        self.assertTrue(issubclass(sp.GpuBusy, sp.ServiceDown))
        self.assertEqual(sp.AITAGGER_CONTAINER, "immich_aitagger")
        self.assertFalse(hasattr(sp, "AITAGGER_VLM_CONTAINER"))
        with mock.patch.object(sp, "AITAGGER_EXCLUSIVE", True), self.docker({"immich_searchplus": False, "immich_aitagger": True}):
            with self.assertRaises(sp.GpuBusy) as ctx:
                sp.Service().start()
        self.assertEqual(str(ctx.exception), "The GPU is in use by the AI Tagger — pause it to use Search+")
        self.assertFalse([c for c in self.docker_calls if c[:2] != ["docker", "inspect"]])      # nothing was started

    def test_ready_raises_it_too_so_searches_get_a_clear_answer(self):
        service = sp.Service()
        with mock.patch.object(sp, "AITAGGER_EXCLUSIVE", True), self.docker({"immich_aitagger": True}), \
                mock.patch.object(service, "health", return_value=None):
            with self.assertRaises(sp.GpuBusy):
                service.ready(wait=1)

    def test_the_exclusive_switch_is_the_one_place_that_decides(self):
        self.assertFalse(sp.AITAGGER_EXCLUSIVE)     # shipped: they share the card (measured 17.3 GB with everything)
        with mock.patch.object(sp, "AITAGGER_EXCLUSIVE", True), self.docker({"immich_searchplus": False, "immich_aitagger": True}):
            with self.assertRaises(sp.GpuBusy):
                sp.Service().start()
        with mock.patch.object(sp, "AITAGGER_EXCLUSIVE", False):
            with self.docker({"immich_searchplus": False, "immich_aitagger": True}):          # the tagger runs: Search+ starts anyway
                sp.Service().start()
            self.assertEqual(self.docker_calls[-1], ["docker", "start", "immich_searchplus"])
            with self.docker({"immich_aitagger": True}):                                      # and from nothing, by compose
                sp.Service().start()
            self.assertEqual(self.docker_calls[-1][:3], ["docker", "compose", "-f"])
            service = sp.Service()
            with self.docker({"immich_searchplus": False, "immich_aitagger": True}), \
                    mock.patch.object(service, "health", side_effect=[None, {"status": "ok"}]):
                self.assertEqual(service.ready(wait=30), {"status": "ok"})                      # ready() no longer says GpuBusy

    def test_search_plus_starts_as_before_when_the_tagger_is_not_running(self):
        for tagger in ({}, {"immich_aitagger": False}):
            with self.docker({"immich_searchplus": False, **tagger}):
                sp.Service().start()
            self.assertEqual(self.docker_calls[-1], ["docker", "start", "immich_searchplus"])
        with self.docker({}):                                                   # no containers at all: compose up
            sp.Service().start()
        self.assertEqual(self.docker_calls[-1][:3], ["docker", "compose", "-f"])

    def test_the_old_describer_container_does_not_make_the_card_busy(self):
        # v2's second container is gone from the compose file; a leftover one must not block Search+ any more
        with mock.patch.object(sp, "AITAGGER_EXCLUSIVE", True), self.docker({"immich_searchplus": False, "immich_aitagger_vlm": True}):
            sp.Service().start()
        self.assertEqual(self.docker_calls[-1], ["docker", "start", "immich_searchplus"])
        self.assertFalse([c for c in self.docker_calls if c[-1] == "immich_aitagger_vlm"])        # it is not even looked at

    def test_the_container_name_can_be_set_in_the_environment(self):
        with mock.patch.object(sp, "AITAGGER_CONTAINER", "my_tagger"), mock.patch.object(sp, "AITAGGER_EXCLUSIVE", True), \
                self.docker({"immich_searchplus": False, "my_tagger": True, "immich_aitagger": False}):
            with self.assertRaises(sp.GpuBusy):
                sp.Service().start()

    def test_a_running_search_plus_is_left_alone(self):
        with self.docker({"immich_searchplus": True, "immich_aitagger": True}):
            sp.Service().start()
        self.assertEqual([c for c in self.docker_calls if c[:2] != ["docker", "inspect"]], [])

    def test_docker_missing_means_not_busy(self):
        with mock.patch.object(sp.subprocess, "run", side_effect=FileNotFoundError("docker")):
            self.assertFalse(sp.container_running("immich_aitagger"))

    def test_the_indexer_waits_for_the_card_and_does_not_count_it_as_a_drop(self):
        class Busy(FakeService):
            def __init__(self, busy):
                super().__init__()
                self.busy, self.looks = busy, 0

            def ready(self, wait=0, progress=None, stop=None):
                self.looks += 1
                if self.busy:
                    self.busy -= 1
                    raise sp.GpuBusy("The GPU is in use by the AI Tagger — pause it to use Search+")
                return super().ready(wait, progress, stop)

        self.service = Busy(5)                         # more than the 3 drops that end an indexer
        self.indexer = sp.Indexer(self.store, self.service, catalog=lambda: self.catalog, frames=fake_frames)
        self.indexer.BUSY_WAIT = 0.01
        self.indexer.IMAGES_PER_REQUEST = 3
        self.build()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["indexed"], 4)
        self.assertEqual(self.service.looks, 6)

    def test_pausing_ends_the_wait_at_once(self):
        class Busy(FakeService):
            def ready(self, wait=0, progress=None, stop=None):
                raise sp.GpuBusy("The GPU is in use by the AI Tagger")

        self.indexer = sp.Indexer(self.store, Busy(), catalog=lambda: self.catalog, frames=fake_frames)
        self.indexer.BUSY_WAIT = 60
        self.indexer.start()
        time.sleep(0.3)
        self.assertEqual(self.indexer.state, "starting")
        self.assertIn("waiting", self.indexer.detail)
        started = time.monotonic()
        self.indexer.stop(wait_s=5)
        self.assertFalse(self.indexer.running())
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(self.indexer.state, "stopped")


class TestCaptureHelpers(Base):
    def test_video_frames_can_be_cut_at_given_positions(self):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd[cmd.index("-ss") + 1])
            return mock.Mock(stdout=b"jpeg")

        with mock.patch.object(sp.subprocess, "run", side_effect=run):
            self.assertEqual(len(sp.video_frames("v.mp4", 80000, 2, positions=[0.25, 0.5])), 2)
            self.assertEqual(calls, ["20.00", "40.00"])
            calls.clear()
            sp.video_frames("v.mp4", 80000, 2)                           # as before: spread evenly
            self.assertEqual(calls, ["20.00", "60.00"])

    def test_animation_frames_can_be_taken_at_given_positions(self):
        from PIL import Image
        from immich_organizer.media import animation_frames
        path = Path(self.tmp.name) / "a.gif"
        frames = [Image.new("RGB", (50, 50), c) for c in ("red", "green", "blue", "white", "black", "yellow", "pink", "gray")]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=100)
        self.assertEqual(len(animation_frames(str(path), 4)), 4)
        self.assertEqual(len(animation_frames(str(path), 99, positions=[0.3, 0.7])), 2)


# ---------------------------------------------------------------- keeping up to date, and freeing the card

def wait_for(cond, timeout=5.0):
    """Poll ``cond`` until it is true (the indexer runs on its own thread)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


class IdleService(FakeService):
    """A model server that says how long it has been idle (what /health says), so the unload rules can be tried
    without waiting. ``idle`` is /health's idleSeconds, ``age`` the panel's seconds since a person last used the model
    (None: not at all), ``inflight_n`` the panel's requests in flight."""

    def __init__(self):
        super().__init__()
        self.running, self.idle, self.exit_minutes, self.age, self.inflight_n = True, 0, 20, None, 0
        self.status, self.stops, self.stop_error = "ok", 0, None

    def unload_inputs(self, fresh=False):
        if not self.running:
            return False, None
        return True, {"status": self.status, "idleSeconds": self.idle, "idleExitMinutes": self.exit_minutes}

    def inflight(self):
        return self.inflight_n

    def interactive_age(self):
        return self.age

    def stop(self):
        self.stops += 1
        if self.stop_error:
            raise self.stop_error
        was, self.running = self.running, False
        self.stopped = True
        return was

    def health(self, timeout=3):
        return self.unload_inputs()[1]

    def container_state(self, fresh=False):
        return "running" if self.running else "stopped"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def health(idle=0, status="ok", exit_minutes=20, **more):
    return {"status": status, "idleSeconds": idle, "idleExitMinutes": exit_minutes, **more}


class TestUnloadRules(unittest.TestCase):
    """``unload_status``: the rules as a pure function (both features use it)."""

    def status(self, **kw):
        kw = {"running": True, "health": health(), "unload_after": 2, "indexer": "waiting", **kw}
        return sp.unload_status(**kw)

    def test_not_loaded(self):
        out, due = self.status(running=False, health=None)
        self.assertEqual(out, {"loaded": False, "idleSeconds": None, "unloadInSeconds": None, "rule": None, "busy": False})
        self.assertFalse(due)

    def test_after_the_work_the_short_rule_counts_down_and_fires(self):
        out, due = self.status(health=health(30))
        self.assertEqual(out, {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 90, "rule": "after-work", "busy": False})
        self.assertFalse(due)
        out, due = self.status(health=health(119))
        self.assertEqual((out["unloadInSeconds"], due), (1, False))
        out, due = self.status(health=health(120))
        self.assertEqual((out["unloadInSeconds"], out["rule"], due), (0, "after-work", True))
        out, due = self.status(health=health(500), unload_after=10)             # 10 minutes: 600 s of quiet
        self.assertEqual((out["unloadInSeconds"], due), (100, False))

    def test_never_while_the_indexer_works_or_a_request_is_in_flight(self):
        out, due = self.status(health=health(500), indexer="working")
        self.assertEqual((out["busy"], out["unloadInSeconds"], due), (True, None, False))
        out, due = self.status(health=health(500), busy=True)                    # a request the panel has out
        self.assertEqual((out["busy"], out["unloadInSeconds"], due), (True, None, False))
        out, due = self.status(health=health(500, busy=1))                       # the tagger's own busy counter
        self.assertEqual((out["busy"], out["unloadInSeconds"], due), (True, None, False))
        out, due = self.status(health=health(500, busy=0))
        self.assertTrue(due)

    def test_an_interactive_use_earns_the_servers_own_idle_time(self):
        out, due = self.status(health=health(500), interactive_age=600)           # a search 10 minutes ago
        self.assertEqual((out["rule"], out["unloadInSeconds"], due), ("interactive", 600, False))
        out, due = self.status(health=health(60), interactive_age=60)             # the search was the last request
        self.assertEqual((out["rule"], out["unloadInSeconds"], due), ("interactive", 1140, False))
        out, due = self.status(health=health(200), interactive_age=1250)          # the 20 minutes are over: the short rule
        self.assertEqual((out["rule"], out["unloadInSeconds"], due), ("after-work", 0, True))
        out, due = self.status(health=health(30), interactive_age=1250)
        self.assertEqual((out["rule"], out["unloadInSeconds"], due), ("after-work", 90, False))

    def test_a_paused_indexer_leaves_it_to_the_server(self):
        out, due = self.status(health=health(500), indexer="off")
        self.assertEqual((out["rule"], out["unloadInSeconds"], out["busy"], due), ("server", 700, False, False))
        out, due = self.status(health=health(1500), indexer="off")
        self.assertEqual((out["unloadInSeconds"], due), (0, False))               # the panel never stops it then

    def test_a_longer_unload_after_than_the_server_waits_is_the_servers_exit(self):
        out, due = self.status(health=health(300), unload_after=30)
        self.assertEqual((out["rule"], out["unloadInSeconds"], due), ("server", 900, False))

    def test_while_loading_nothing_counts(self):
        out, due = self.status(health=health(0, status="loading"))
        self.assertEqual((out["loaded"], out["idleSeconds"], out["unloadInSeconds"], out["busy"], due),
                         (True, None, None, False, False))
        out, due = self.status(health=None)                                        # running, not answering (yet)
        self.assertEqual((out["loaded"], out["idleSeconds"], out["unloadInSeconds"], due), (True, None, None, False))

    def test_a_server_that_never_exits_has_no_server_countdown(self):
        out, due = self.status(health=health(500, exit_minutes=0), indexer="off")
        self.assertEqual((out["rule"], out["unloadInSeconds"]), ("server", None))
        out, due = self.status(health=health(30, exit_minutes=0))
        self.assertEqual((out["rule"], out["unloadInSeconds"]), ("after-work", 90))
        out, due = self.status(health=health(500, exit_minutes=0), interactive_age=100)    # the grace is 20 minutes then
        self.assertEqual((out["rule"], out["unloadInSeconds"], due), ("interactive", 1100, False))


class Idle(Base):
    """A real Indexer on an IdleService, looking every few milliseconds."""

    def setUp(self):
        super().setUp()
        self.service = IdleService()
        self.make()

    def make(self, **kw):
        self.indexer = sp.Indexer(self.store, self.service, catalog=lambda: self.catalog, frames=fake_frames, **kw)
        self.indexer.IMAGES_PER_REQUEST = 3
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)

    def up_to_date(self, **settings):
        """Index everything and leave the indexer waiting for new photos."""
        sp.save_settings({"keep_updated": True, **settings})
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting), self.indexer.detail)

    def quiet(self, seconds=0.25):
        """Give the waiting loop time to look a few times."""
        time.sleep(seconds)


class TestUnloadAfterTheWork(Idle):
    def test_the_model_is_stopped_once_it_has_been_idle_for_unload_after_minutes(self):
        self.service.idle = 30
        self.up_to_date()
        self.quiet()
        self.assertEqual(self.service.stops, 0)
        self.assertTrue(self.service.running)
        self.service.idle = 125
        self.assertTrue(wait_for(lambda: self.service.stops == 1))
        self.assertFalse(self.service.running)
        self.assertTrue(self.indexer.running())                       # it goes on watching for new photos
        self.assertEqual(self.indexer.unload_status()["loaded"], False)
        self.quiet()
        self.assertEqual(self.service.stops, 1)                       # not stopped again: it is not running

    def test_never_while_a_request_is_in_flight(self):
        self.service.idle, self.service.inflight_n = 500, 1
        self.up_to_date()
        self.quiet()
        self.assertEqual(self.service.stops, 0)
        self.assertTrue(self.indexer.unload_status()["busy"])
        self.service.inflight_n = 0
        self.assertTrue(wait_for(lambda: self.service.stops == 1))

    def test_never_while_the_indexer_is_working(self):
        gate = threading.Event()

        def slow(item, n):
            gate.wait(5)
            return fake_frames(item, n)

        self.indexer.frames = slow
        self.service.idle = 500
        sp.save_settings({"keep_updated": True})
        self.indexer.start()
        self.quiet(0.3)
        self.assertEqual(self.service.stops, 0)
        status = self.indexer.unload_status()
        self.assertEqual((status["loaded"], status["busy"], status["unloadInSeconds"]), (True, True, None))
        gate.set()
        self.assertTrue(wait_for(lambda: self.service.stops == 1))
        self.assertEqual(self.store.counts()["indexed"], 4)           # the work was done first

    def test_not_within_the_interactive_grace(self):
        self.service.idle, self.service.age = 500, 600                # a search ten minutes ago
        self.up_to_date()
        self.quiet()
        self.assertEqual(self.service.stops, 0)
        status = self.indexer.unload_status()
        self.assertEqual((status["rule"], status["unloadInSeconds"]), ("interactive", 600))
        self.service.idle, self.service.age = 200, 1250               # 20 minutes passed since it; the indexer worked since
        self.assertTrue(wait_for(lambda: self.service.stops == 1))

    def test_the_unload_after_setting_decides(self):
        self.service.idle = 130
        self.up_to_date(unload_after=5)
        self.quiet()
        self.assertEqual(self.service.stops, 0)
        self.service.idle = 305
        self.assertTrue(wait_for(lambda: self.service.stops == 1))

    def test_a_paused_indexer_never_stops_it_the_server_does(self):
        self.service.idle = 500
        status = self.indexer.unload_status()                          # no thread: paused / not started
        self.assertEqual((status["rule"], status["unloadInSeconds"], status["busy"]), ("server", 700, False))
        sp.save_settings({"keep_updated": True})
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done"))
        self.indexer.stop(5)
        self.quiet()
        self.assertFalse(self.indexer.running())
        self.assertLessEqual(self.service.stops, 1)                   # only before it was paused (idle was 500)
        stops = self.service.stops
        self.quiet()
        self.assertEqual(self.service.stops, stops)

    def test_a_failing_stop_does_not_end_the_indexer(self):
        self.service.idle, self.service.stop_error = 500, OSError("docker is not answering")
        self.up_to_date()
        self.assertTrue(wait_for(lambda: self.service.stops >= 2))     # it keeps trying, quietly
        self.assertTrue(self.indexer.running())
        self.assertEqual(self.indexer.state, "done")

    def test_a_new_photo_after_the_unload_is_indexed_again(self):
        self.service.idle = 500
        self.up_to_date()
        self.assertTrue(wait_for(lambda: self.service.stops == 1))
        self.catalog = self.catalog + [{"id": "n1", "type": "IMAGE", "taken": "2026-02-01", "name": "n1.jpg",
                                        "preview": "dog", "original": "", "duration_ms": 0}]
        self.indexer.start()                                           # "look again now"
        self.assertTrue(wait_for(lambda: self.store.counts()["indexed"] == 5))

    def test_nothing_is_stopped_when_the_stand_in_cannot_say(self):
        self.service = FakeService()                                   # no unload_inputs: never unloaded
        self.make()
        self.up_to_date()
        self.quiet()
        self.assertEqual(self.service.stopped, False)
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": False, "idleSeconds": None, "unloadInSeconds": None, "rule": None, "busy": False})


class TestWhenKeepUpdatedIsOff(Idle):
    """With "keep it up to date" off the thread ends after the work, but not before the card is freed."""

    def finished(self):
        return not self.indexer.running()

    def test_the_card_is_freed_before_the_thread_ends(self):
        self.service.idle = 500
        sp.save_settings({"keep_updated": False})
        self.indexer.start()
        self.assertTrue(wait_for(self.finished))
        self.assertEqual((self.indexer.state, self.service.stops), ("done", 1))

    def test_it_waits_out_the_short_rule(self):
        self.service.idle = 30
        sp.save_settings({"keep_updated": False})
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.quiet()
        self.assertTrue(self.indexer.running())                        # still there: 90 s to go
        self.assertEqual(self.service.stops, 0)
        self.service.idle = 125
        self.assertTrue(wait_for(self.finished))
        self.assertEqual(self.service.stops, 1)

    def test_an_interactive_grace_leaves_it_to_the_server(self):
        self.service.idle, self.service.age = 500, 600
        sp.save_settings({"keep_updated": False})
        self.indexer.start()
        self.assertTrue(wait_for(self.finished))
        self.assertEqual(self.service.stops, 0)                        # the server's own 20 minutes

    def test_a_restart_wakes_it_for_new_work(self):
        self.service.idle = 30
        sp.save_settings({"keep_updated": False})
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.catalog = self.catalog + [{"id": "n1", "type": "IMAGE", "taken": "2026-02-01", "name": "n1.jpg",
                                        "preview": "dog", "original": "", "duration_ms": 0}]
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.store.counts()["indexed"] == 5))
        self.assertEqual(self.service.stops, 0)

    def test_pausing_ends_the_wait_at_once(self):
        self.service.idle = 30
        sp.save_settings({"keep_updated": False})
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.indexer.stop(5)
        self.assertTrue(self.finished())
        self.assertEqual(self.service.stops, 0)

    def test_a_request_still_in_flight_keeps_it_waiting(self):
        self.service.idle, self.service.inflight_n = 500, 1
        sp.save_settings({"keep_updated": False})
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.quiet()
        self.assertTrue(self.indexer.running())
        self.assertEqual(self.service.stops, 0)
        self.service.inflight_n = 0
        self.assertTrue(wait_for(self.finished))
        self.assertEqual(self.service.stops, 1)


class TestTheSleepBetweenLooks(unittest.TestCase):
    """What the waiting loop sleeps: at most a minute, until the unload rule may fire, never a tight loop."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = sp.Store(Path(tmp.name))
        self.addCleanup(self.store.conn.close)
        self.indexer = sp.Indexer(self.store, FakeService(), catalog=lambda: [])
        self.settings = {"check_every": 10}

    def wait(self, unload):
        return self.indexer._idle_wait(self.settings, unload)

    def test_the_sleep(self):
        loaded = {"loaded": True, "rule": "after-work", "busy": False}
        self.assertEqual(self.wait(None), 1)                                    # no probe yet: look at once (1 s at least)
        self.indexer._watch.probed_at = self.indexer._watch.clock()
        self.assertEqual(self.wait(None), 60)                                   # a minute at most: that is what runs the check
        self.assertEqual(self.wait({**loaded, "unloadInSeconds": 500}), 60)
        self.assertEqual(self.wait({**loaded, "unloadInSeconds": 30}), 31)      # the unload is due in 30 s: look just after
        self.assertEqual(self.wait({**loaded, "unloadInSeconds": 0}), 5)        # due but it did not stop: not a tight loop
        self.assertEqual(self.wait({**loaded, "unloadInSeconds": None, "busy": True}), 5)   # busy: look again soon
        self.assertEqual(self.wait({**loaded, "rule": "server", "unloadInSeconds": 900}), 60)   # the server's job
        self.assertEqual(self.wait({"loaded": False}), 60)


class TestUnloadStatusObject(Idle):
    """The ``unload`` object of GET /api/searchplus, for each case."""

    def test_not_loaded(self):
        self.service.running = False
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": False, "idleSeconds": None, "unloadInSeconds": None, "rule": None, "busy": False})

    def test_waiting_for_new_photos(self):
        self.service.idle = 30
        self.up_to_date()
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 90, "rule": "after-work", "busy": False})

    def test_after_an_interactive_use(self):
        self.service.idle, self.service.age = 30, 30
        self.up_to_date()
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 1170, "rule": "interactive", "busy": False})

    def test_paused(self):
        self.service.idle = 30
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 1170, "rule": "server", "busy": False})

    def test_a_request_in_flight(self):
        self.service.idle, self.service.inflight_n = 30, 1
        self.up_to_date()
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": None, "rule": "after-work", "busy": True})

    def test_the_caller_can_hand_in_what_it_already_knows(self):
        self.service.running = False                                  # the object trusts what it is given, asks nothing
        self.assertEqual(self.indexer.unload_status(running=True, health=health(30)),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 1170, "rule": "server", "busy": False})
        self.assertEqual(self.indexer.unload_status(running=False)["loaded"], False)


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"IMMICH_ORGANIZER_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults_and_limits(self):
        s = sp.load_settings()
        self.assertEqual((s["unload_after"], s["check_every"]), (2, 1))
        self.assertEqual((sp.LIMITS["unload_after"], sp.LIMITS["check_every"]), ((1, 60), (1, 60)))

    def test_they_are_saved_and_checked(self):
        s = sp.save_settings({"unload_after": 5, "check_every": 10})
        self.assertEqual((s["unload_after"], s["check_every"]), (5, 10))
        self.assertEqual(sp.load_settings()["unload_after"], 5)
        self.assertEqual(sp.save_settings({"unload_after": "7"})["unload_after"], 7)         # the form sends numbers as text too
        for key in ("unload_after", "check_every"):
            for bad in (0, 61, -1, "x", None):
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    sp.save_settings({key: bad})
        self.assertEqual(sp.load_settings()["unload_after"], 7)                               # a refused change changed nothing
        for key in ("unload_after", "check_every"):                                          # the edges are fine
            for ok in (1, 60):
                self.assertEqual(sp.save_settings({key: ok})[key], ok)

    def test_a_hand_edited_file_cannot_make_it_unload_at_once(self):
        sp.settings_path().write_text('{"unload_after": 0, "check_every": "soon", "video_frames": 4}')
        s = sp.load_settings()
        self.assertEqual((s["unload_after"], s["check_every"]), (2, 1))
        sp.settings_path().write_text('{"unload_after": 500}')
        self.assertEqual(sp.load_settings()["unload_after"], 2)


class TestChangeProbe(Base):
    """The library list is read in full only when the cheap probe changed, and at least once an hour."""

    def setUp(self):
        super().setUp()
        self.clock, self.signature, self.probes, self.reads = FakeClock(), ["A"], [], []

        def probe():
            self.probes.append(self.clock())
            return (self.signature[0],)

        def catalog():
            self.reads.append(self.clock())
            return list(self.catalog)

        self.indexer = sp.Indexer(self.store, self.service, catalog=catalog, frames=fake_frames, clock=self.clock,
                                  probe=probe)
        self.indexer.IMAGES_PER_REQUEST = 3
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)
        sp.save_settings({"keep_updated": True})

    def up_to_date(self):
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting), self.indexer.detail)

    def advance(self, seconds):
        self.clock.t += seconds

    def test_the_first_look_reads_everything(self):
        self.up_to_date()
        self.assertEqual((len(self.reads), len(self.probes)), (1, 1))

    def test_an_unchanged_probe_reads_nothing_however_often_it_looks(self):
        self.up_to_date()
        for _ in range(5):
            self.advance(61)
            self.assertTrue(wait_for(lambda n=len(self.probes): len(self.probes) > n))
        self.assertEqual(len(self.reads), 1)
        self.assertGreaterEqual(len(self.probes), 6)

    def test_it_does_not_even_probe_before_check_every_minutes_have_passed(self):
        self.up_to_date()
        self.signature[0] = "B"
        self.advance(59)
        time.sleep(0.2)
        self.assertEqual((len(self.probes), len(self.reads)), (1, 1))
        self.advance(2)
        self.assertTrue(wait_for(lambda: len(self.reads) == 2))

    def test_a_changed_probe_picks_up_the_new_upload(self):
        self.up_to_date()
        self.assertEqual(self.store.counts()["indexed"], 4)
        self.catalog = self.catalog + [{"id": "n1", "type": "IMAGE", "taken": "2026-02-01", "name": "n1.jpg",
                                        "preview": "dog", "original": "", "duration_ms": 0}]
        self.signature[0] = "B"
        self.advance(61)
        self.assertTrue(wait_for(lambda: self.store.counts()["indexed"] == 5))
        self.assertEqual(len(self.reads), 2)
        self.advance(61)                                               # nothing changed since: no more reads
        self.assertTrue(wait_for(lambda: len(self.probes) == 3))
        time.sleep(0.1)
        self.assertEqual(len(self.reads), 2)

    def test_check_every_is_a_setting(self):
        self.up_to_date()
        sp.save_settings({"check_every": 5})
        self.signature[0] = "B"
        self.advance(4 * 60)
        time.sleep(0.2)
        self.assertEqual(len(self.reads), 1)                           # four minutes: not yet
        self.advance(61)
        self.assertTrue(wait_for(lambda: len(self.reads) == 2))

    def test_a_safety_read_every_hour_even_when_the_probe_never_changes(self):
        self.up_to_date()
        self.advance(3599)
        self.assertTrue(wait_for(lambda: len(self.probes) >= 2))
        time.sleep(0.1)
        self.assertEqual(len(self.reads), 1)
        self.advance(2)                                                # one hour since the last full read
        self.assertTrue(wait_for(lambda: len(self.reads) == 2))
        self.advance(3601)
        self.assertTrue(wait_for(lambda: len(self.reads) == 3))

    def test_a_probe_that_fails_reads_nothing_and_does_not_stop_the_indexer(self):
        self.up_to_date()

        def broken():
            raise RuntimeError("docker exec failed")

        self.indexer._watch.probe = broken
        self.advance(61)
        time.sleep(0.2)
        self.assertEqual((len(self.reads), self.indexer.state), (1, "done"))
        self.assertTrue(self.indexer.running())
        self.advance(3600)                                             # the safety read is the net
        self.assertTrue(wait_for(lambda: len(self.reads) == 2))

    def test_start_looks_again_at_once(self):
        self.up_to_date()
        self.signature[0] = "B"
        self.indexer.start()                                           # "Try again" / "Resume": do not wait a minute
        self.assertTrue(wait_for(lambda: len(self.reads) == 2))

    def test_the_wait_is_never_longer_than_a_minute_so_the_unload_check_runs(self):
        self.assertEqual(sp.Indexer.IDLE_POLL, 60)
        self.assertEqual(sp.IDLE_POLL, 60)


class TestCatalogWatch(unittest.TestCase):
    """The decision on its own, with a clock in the hand."""

    def setUp(self):
        self.clock, self.answer, self.calls = FakeClock(), ("A",), 0

        def probe():
            self.calls += 1
            return self.answer

        self.watch = sp.CatalogWatch(probe, self.clock)

    def read(self):
        self.watch.synced(self.watch.seen)
        return self.clock()

    def test_first_then_quiet_then_changed_then_safety(self):
        self.assertEqual(self.watch.due(None, 1), "first")
        last = self.read()
        self.assertEqual(self.watch.due(last, 1), "")                  # not a minute yet: no probe
        self.assertEqual(self.calls, 1)
        self.clock.t += 60
        self.assertEqual(self.watch.due(last, 1), "")                  # probed, same answer
        self.assertEqual(self.calls, 2)
        self.answer = ("B",)
        self.clock.t += 60
        self.assertEqual(self.watch.due(last, 1), "changed")
        last = self.read()
        self.clock.t += 60
        self.assertEqual(self.watch.due(last, 1), "")                  # the answer is now the baseline
        self.clock.t += sp.SAFETY_SYNC
        self.assertEqual(self.watch.due(last, 1), "safety")

    def test_a_failing_probe_is_not_a_change(self):
        self.watch.due(None, 1)
        last = self.read()

        def broken():
            raise OSError("no docker")

        self.watch.probe = broken
        self.clock.t += 120
        self.assertEqual(self.watch.due(last, 1), "")

    def test_without_a_probe_every_look_is_a_read(self):
        watch = sp.CatalogWatch(None, self.clock)
        self.assertEqual(watch.due(None, 1), "first")
        last = self.clock()
        self.assertEqual(watch.due(last, 1), "")
        self.clock.t += 60
        self.assertEqual(watch.due(last, 1), "changed")

    def test_next_in(self):
        self.assertEqual(self.watch.next_in(1), 0)
        self.watch.due(None, 1)
        self.clock.t += 20
        self.assertEqual(self.watch.next_in(1), 40)
        self.assertEqual(self.watch.next_in(5), 280)
        self.clock.t += 100
        self.assertEqual(self.watch.next_in(1), 0)


class TestTheProbeQuery(unittest.TestCase):
    """The probe and the catalogue as they talk to Immich's database (docker is replaced)."""

    def docker(self, probe_out="", catalog_out=""):
        calls = []

        def run(cmd, **kwargs):
            calls.append((list(cmd), kwargs.get("input") or ""))
            if cmd[:3] == ["docker", "exec", "-i"]:
                sql = kwargs.get("input") or ""
                return mock.Mock(returncode=0, stdout=probe_out if "count(*)" in sql else catalog_out, stderr="")
            return mock.Mock(returncode=0, stdout='[{"Destination": "/data", "Source": "/srv/immich"}]', stderr="")

        return calls, mock.patch.object(sp.subprocess, "run", side_effect=run)

    def test_the_probe_is_one_small_select_on_columns_that_move_on_upload_and_delete(self):
        sql = sp.PROBE_SQL
        for needed in ('"createdAt"', '"deletedAt"', "asset_file", "type = 'preview'", "count(*)"):
            self.assertIn(needed, sql)
        for never in ("updatedAt", "updateId"):                         # they move whenever anything is written (tags!)
            self.assertNotIn(never, sql)
        for never in ("insert", "update ", "delete ", "alter", "drop"):
            self.assertNotIn(never, sql.lower())
        self.assertEqual(sql.lower().count("select"), 6)                # one statement: the outer select and five sub-selects

    def test_fetch_probe_reads_the_five_numbers(self):
        calls, patch = self.docker(probe_out="72987\x1f2026-10-02 22:07:08+00\x1f2026-10-01 09:40:33+00\x1f72972\x1f2026-10-02 22:07:09+00\x1e\n")
        with patch:
            got = sp.fetch_probe()
        self.assertEqual(got, ("72987", "2026-10-02 22:07:08+00", "2026-10-01 09:40:33+00", "72972", "2026-10-02 22:07:09+00"))
        self.assertEqual(len(calls), 1)                                  # one docker call, nothing else
        self.assertEqual(calls[0][0][:6], ["docker", "exec", "-i", "immich_postgres", "psql", "-U"])
        self.assertEqual(calls[0][1], sp.PROBE_SQL)
        with self.docker(probe_out="oops")[1], self.assertRaises(RuntimeError):
            sp.fetch_probe()

    def test_the_catalogue_carries_when_each_asset_was_added(self):
        row = "a1\x1fIMAGE\x1f/data/thumbs/a1.jpeg\x1f/data/upload/a1.jpg\x1f0\x1f2026-01-05 10:00:00+00\x1fa1.jpg\x1f1790978829\x1e\n"
        row2 = "v1\x1fVIDEO\x1f\x1f/data/upload/v1.mp4\x1f9000\x1f2026-01-03 10:00:00+00\x1fv1.mp4\x1f1790978900\x1e\n"
        calls, patch = self.docker(catalog_out=row + row2)
        with patch:
            rows = sp.fetch_catalog()
        self.assertEqual([r["id"] for r in rows], ["a1", "v1"])
        self.assertEqual((rows[0]["added"], rows[0]["preview"], rows[1]["preview"]), (1790978829, "/srv/immich/thumbs/a1.jpeg", ""))
        self.assertIn('"createdAt"', calls[0][1])                        # the 8th column
        with self.docker(catalog_out="a1\x1fIMAGE\x1fx\x1fy\x1f0\x1ft\x1fn\x1e")[1]:     # an old 7-field answer is skipped
            self.assertEqual(sp.fetch_catalog(), [])

    def test_the_real_probe_goes_with_the_real_catalogue_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = sp.Store(Path(tmp.name))
        self.addCleanup(store.conn.close)
        self.assertIs(sp.Indexer(store, FakeService())._watch.probe, sp.fetch_probe)
        self.assertIsNone(sp.Indexer(store, FakeService(), catalog=lambda: [])._watch.probe)


class TestPreviewHoldBack(Base):
    """An asset Immich has not finished (no preview file yet) is not worked on, and not failed, until it has one."""

    T0 = 1_790_000_000

    def setUp(self):
        super().setUp()
        self.now = [self.T0]
        patcher = mock.patch.object(sp, "_wall", side_effect=lambda: self.now[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.asked = []

        def frames(item, n):
            self.asked.append(item["id"])
            if item["preview"] == "":                                  # as prepare_frames does: the picture itself
                return [b"dog"], "image"
            return fake_frames(item, n)

        self.indexer = sp.Indexer(self.store, self.service, catalog=lambda: self.catalog, frames=frames)
        self.indexer.IMAGES_PER_REQUEST = 3
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)
        self.new = {"id": "n1", "type": "IMAGE", "taken": "2026-02-01", "name": "n1.jpg", "preview": "", "original": "",
                    "duration_ms": 0, "added": self.T0 - 60}
        self.catalog = list(CATALOG) + [self.new]

    def todo(self):
        return [t["id"] for t in self.store.todo(50)]

    def test_a_new_asset_without_a_preview_is_left_out_of_the_work_list(self):
        self.store.sync_catalog(self.catalog)
        self.assertNotIn("n1", self.todo())
        self.assertEqual(self.store.held(), 1)
        self.assertIn("a1", self.todo())

    def test_it_is_held_not_failed_and_picked_up_when_the_preview_appears(self):
        sp.save_settings({"keep_updated": True})
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.assertNotIn("n1", self.asked)
        self.assertIsNone(self.store.conn.execute("select 1 from failed where id='n1'").fetchone())
        self.assertIn("1 new item is waiting for Immich", self.indexer.detail)
        self.assertEqual(self.store.counts()["indexed"], 4)
        # Immich made the preview: the next full read brings its path, and the asset goes in
        self.catalog = list(CATALOG) + [{**self.new, "preview": "dog"}]
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.store.counts()["indexed"] == 5))
        self.assertIn("n1", self.asked)
        self.assertEqual(self.store.held(), 0)

    def test_after_thirty_minutes_without_a_preview_it_is_processed_as_before(self):
        self.store.sync_catalog(self.catalog)
        self.now[0] = self.T0 - 60 + sp.PREVIEW_GRACE - 1
        self.assertNotIn("n1", self.todo())
        self.now[0] = self.T0 - 60 + sp.PREVIEW_GRACE + 1
        self.assertIn("n1", self.todo())
        self.assertEqual(self.store.held(), 0)
        sp.save_settings({"keep_updated": False})
        self.indexer.start()
        self.indexer.thread.join(10)
        self.assertIn("n1", self.asked)                                  # it fell back to the file itself
        self.assertEqual(self.store.counts()["indexed"], 5)

    def test_assets_of_unknown_age_and_older_assets_are_never_held(self):
        rows = [{**self.new, "id": "x1", "added": 0},                     # a catalogue that does not say (and old stores)
                {**self.new, "id": "x2", "added": self.T0 - 7200}]
        self.store.sync_catalog(rows)
        self.assertEqual(sorted(self.todo()), ["x1", "x2"])

    def test_the_same_goes_for_videos(self):
        self.store.sync_catalog([{**self.new, "id": "vid", "type": "VIDEO", "duration_ms": 5000}])
        self.assertEqual(self.todo(), [])
        self.store.sync_catalog([{**self.new, "id": "vid", "type": "VIDEO", "duration_ms": 5000, "preview": "car"}])
        self.assertEqual(self.todo(), ["vid"])

    def test_a_store_made_before_this_gets_the_column_and_nothing_is_held(self):
        import sqlite3
        folder = Path(self.tmp.name) / "old"
        folder.mkdir()
        conn = sqlite3.connect(str(folder / "index.sqlite"))
        conn.executescript("create table assets (id text primary key, type text, taken text, name text, preview text,"
                           " original text, duration_ms integer default 0, gone integer default 0);"
                           "create table indexed (id text primary key, first_row integer, n_rows integer, kind text,"
                           " indexed_at text);"
                           "insert into assets (id, type, taken, name, preview, original) values ('old1','IMAGE','2024','o','','');")
        conn.commit()
        conn.close()
        store = sp.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual([t["id"] for t in store.todo(5)], ["old1"])      # added = 0: unknown, not held
        store.sync_catalog([{**self.new, "id": "old1"}])
        self.assertEqual(store.todo(5), [])                              # now it is known to be new
        self.assertEqual(store.held(), 1)


class TestTheServiceKeepsTrack(unittest.TestCase):
    """The facts the unload rules need from the real Service: requests in flight, interactive use, the container."""

    class Reply:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            import json
            return json.dumps(self.data).encode()

    def test_requests_in_flight_are_counted_and_released(self):
        service = sp.Service()
        seen = []

        def urlopen(req, timeout=0):
            seen.append(service.inflight())
            if "fail" in req.full_url:
                raise sp.urllib.error.URLError("down")
            return self.Reply({"vectors": [], "errors": []})

        with mock.patch.object(sp.urllib.request, "urlopen", urlopen):
            service.embed_images([b"x"])
            with self.assertRaises(sp.ServiceDown):
                service._post("/fail", {})
        self.assertEqual(seen, [1, 1])
        self.assertEqual(service.inflight(), 0)

    def test_only_a_text_embedding_is_interactive_use(self):
        import base64
        clock = FakeClock()
        service = sp.Service(clock=clock)
        vec = base64.b64encode(np.asarray([1, 0, 0, 0], dtype="<f2").tobytes()).decode()
        with mock.patch.object(service, "_post", return_value={"vectors": [], "errors": []}):
            service.embed_images([b"x"])                                  # the indexer's calls are not
        self.assertIsNone(service.interactive_age())
        with mock.patch.object(service, "_post", return_value={"vectors": [vec]}):
            service.embed_text(["a dog"])
        clock.t += 90
        self.assertEqual(service.interactive_age(), 90)
        with mock.patch.object(service, "_post", return_value={"vectors": [], "errors": []}):
            service.embed_images([b"x"])
        self.assertEqual(service.interactive_age(), 90)

    def test_a_failed_text_embedding_still_counts_as_a_use(self):
        clock = FakeClock()
        service = sp.Service(clock=clock)
        with mock.patch.object(service, "_post", side_effect=sp.ServiceDown("loading")), self.assertRaises(sp.ServiceDown):
            service.embed_text(["a dog"])
        self.assertEqual(service.interactive_age(), 0)

    def test_the_container_state_is_remembered_for_a_few_seconds(self):
        clock, calls = FakeClock(), []

        def run(cmd, **kwargs):
            calls.append(list(cmd))
            return mock.Mock(returncode=0, stdout="true\n", stderr="")

        service = sp.Service(clock=clock)
        with mock.patch.object(sp.subprocess, "run", side_effect=run):
            for _ in range(5):
                self.assertEqual(service.container_state(), "running")
            self.assertEqual(len(calls), 1)
            clock.t += sp.STATE_TTL + 1
            service.container_state()
            self.assertEqual(len(calls), 2)
            service.container_state(fresh=True)
            self.assertEqual(len(calls), 3)

    def test_stop_looks_fresh_and_forgets_what_it_remembered(self):
        clock, states, stops = FakeClock(), ["true\n", "true\n", "false\n"], []

        def run(cmd, **kwargs):
            if cmd[:2] == ["docker", "inspect"]:
                return mock.Mock(returncode=0, stdout=states.pop(0), stderr="")
            stops.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        service = sp.Service(clock=clock)
        with mock.patch.object(sp.subprocess, "run", side_effect=run):
            self.assertEqual(service.container_state(), "running")        # remembered ...
            self.assertTrue(service.stop())                                # ... but a stop asks again,
            self.assertEqual(stops[0][:3], ["docker", "stop", "-t"])
            self.assertEqual(service.container_state(), "stopped")        # and forgets
        self.assertEqual(states, [])

    def test_unload_inputs_is_the_container_and_its_health(self):
        service = sp.Service()
        with mock.patch.object(service, "container_state", return_value="stopped"), \
                mock.patch.object(service, "health") as h:
            self.assertEqual(service.unload_inputs(), (False, None))
            h.assert_not_called()
        with mock.patch.object(service, "container_state", return_value="running"), \
                mock.patch.object(service, "health", return_value={"status": "ok"}):
            self.assertEqual(service.unload_inputs(), (True, {"status": "ok"}))


if __name__ == "__main__":
    unittest.main()
