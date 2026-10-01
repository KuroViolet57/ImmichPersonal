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
    """While the AI Tagger's language model holds the graphics card, Search+ must not start its model server."""

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
        self.assertEqual(sp.AITAGGER_VLM_CONTAINER, "immich_aitagger_vlm")
        with self.docker({"immich_searchplus": False, "immich_aitagger_vlm": True}):
            with self.assertRaises(sp.GpuBusy) as ctx:
                sp.Service().start()
        self.assertEqual(str(ctx.exception), "The GPU is in use by the AI Tagger — pause it to use Search+")
        self.assertFalse([c for c in self.docker_calls if c[:2] != ["docker", "inspect"]])      # nothing was started

    def test_ready_raises_it_too_so_searches_get_a_clear_answer(self):
        service = sp.Service()
        with self.docker({"immich_aitagger_vlm": True}), mock.patch.object(service, "health", return_value=None):
            with self.assertRaises(sp.GpuBusy):
                service.ready(wait=1)

    def test_the_exclusive_switch_is_the_one_place_that_decides(self):
        self.assertTrue(sp.AITAGGER_EXCLUSIVE)                                  # the shipped value: they take turns
        with mock.patch.object(sp, "AITAGGER_EXCLUSIVE", True), self.docker({"immich_searchplus": False, "immich_aitagger_vlm": True}):
            with self.assertRaises(sp.GpuBusy):
                sp.Service().start()
        with mock.patch.object(sp, "AITAGGER_EXCLUSIVE", False):
            with self.docker({"immich_searchplus": False, "immich_aitagger_vlm": True}):      # the tagger runs: Search+ starts anyway
                sp.Service().start()
            self.assertEqual(self.docker_calls[-1], ["docker", "start", "immich_searchplus"])
            with self.docker({"immich_aitagger_vlm": True}):                                  # and from nothing, by compose
                sp.Service().start()
            self.assertEqual(self.docker_calls[-1][:3], ["docker", "compose", "-f"])
            service = sp.Service()
            with self.docker({"immich_searchplus": False, "immich_aitagger_vlm": True}), \
                    mock.patch.object(service, "health", side_effect=[None, {"status": "ok"}]):
                self.assertEqual(service.ready(wait=30), {"status": "ok"})                      # ready() no longer says GpuBusy

    def test_search_plus_starts_as_before_when_the_language_model_is_not_running(self):
        for tagger in ({}, {"immich_aitagger_vlm": False}):
            with self.docker({"immich_searchplus": False, **tagger}):
                sp.Service().start()
            self.assertEqual(self.docker_calls[-1], ["docker", "start", "immich_searchplus"])
        with self.docker({}):                                                   # no containers at all: compose up
            sp.Service().start()
        self.assertEqual(self.docker_calls[-1][:3], ["docker", "compose", "-f"])

    def test_a_running_search_plus_is_left_alone(self):
        with self.docker({"immich_searchplus": True, "immich_aitagger_vlm": True}):
            sp.Service().start()
        self.assertEqual([c for c in self.docker_calls if c[:2] != ["docker", "inspect"]], [])

    def test_docker_missing_means_not_busy(self):
        with mock.patch.object(sp.subprocess, "run", side_effect=FileNotFoundError("docker")):
            self.assertFalse(sp.container_running("immich_aitagger_vlm"))

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


if __name__ == "__main__":
    unittest.main()
