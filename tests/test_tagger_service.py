"""Tests for the AI Tagger model server (immich_organizer/tagger_service.py) that need neither a GPU nor torch.

The service is a standalone file (it runs inside its own container), so it is loaded by path here, with the device
forced to "cpu". The models themselves are checked on the GPU by deploy/aitagger/bench.py and the measurements in
docs/AI-TAGGER.md; what is tested here is the contract around them: the request checks, the answer keys, the
per-picture errors, the calibration and the out-of-memory splitting.
"""
import base64
import importlib.util
import io
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError:                                            # the service itself needs both
    np = Image = None

PATH = Path(__file__).resolve().parents[1] / "immich_organizer" / "tagger_service.py"


def load_service():
    os.environ["AITAGGER_DEVICE"] = "cpu"
    spec = importlib.util.spec_from_file_location("tagger_service_under_test", PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def picture(size=(40, 30), mode="RGB", color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


@unittest.skipIf(np is None, "numpy and Pillow are needed")
class Calibration(unittest.TestCase):
    ts = None

    @classmethod
    def setUpClass(cls):
        cls.ts = load_service()

    def test_score_is_half_at_the_models_own_threshold(self):
        ts = self.ts
        for t in (0.17, 0.35, 0.45, 0.75, 0.9):
            self.assertAlmostEqual(float(ts._sigmoid(ts._logit(t) - ts._logit(t))), 0.5)
            self.assertGreater(float(ts._sigmoid(ts._logit(t + 0.05) - ts._logit(t))), 0.5)
            self.assertLess(float(ts._sigmoid(ts._logit(t - 0.05) - ts._logit(t))), 0.5)

    def test_raw_logit_form_used_for_ram_matches_the_probability_form(self):
        ts = self.ts
        logits = np.array([-3.0, 0.0, 2.5])
        t = 0.62
        by_logit = ts._sigmoid(logits - ts._logit(t))
        by_probability = ts._sigmoid(ts._logit(ts._sigmoid(logits)) - ts._logit(t))
        np.testing.assert_allclose(by_logit, by_probability, atol=1e-6)

    def test_a_threshold_of_one_never_fires(self):
        ts = self.ts
        logit_t = ts._logit(np.clip(np.array([1.0]), 1e-4, 1 - 1e-4))
        self.assertLess(float(ts._sigmoid(np.array([8.0]) - logit_t)[0]), 0.5)     # p = 0.9997 is still below

    def test_model_registry(self):
        self.assertEqual(self.ts.MODEL_NAMES, ("wd", "pixai", "ram", "e621"))

    def test_oom_detection(self):
        ts = self.ts
        self.assertTrue(ts._is_oom(RuntimeError("CUDA out of memory. Tried to allocate 82.00 MiB")))
        self.assertTrue(ts._is_oom(RuntimeError("Failed to allocate memory for requested buffer")))
        self.assertFalse(ts._is_oom(ValueError("bad input")))


@unittest.skipIf(np is None, "numpy and Pillow are needed")
class RamCheckpointNames(unittest.TestCase):
    def test_official_names_map_onto_the_inference_network(self):
        ts = load_service()
        checkpoint = {"model": {
            "visual_encoder.layers.0.blocks.0.attn.qkv.weight": 1,
            "visual_encoder.layers.0.blocks.0.attn.relative_position_index": 2,       # rebuilt at construction: dropped
            "visual_encoder.layers.0.blocks.0.attn_mask": 3,                          # likewise
            "image_proj.weight": 4, "wordvec_proj.bias": 5, "fc.weight": 6, "label_embed": 7, "reweight_scale": 8,
            "tagging_head.encoder.layer.1.crossattention.self.query.weight": 9,
            "tagging_head.encoder.layer.1.crossattention.self.value.bias": 10,
            "tagging_head.encoder.layer.0.crossattention.output.dense.weight": 11,
            "tagging_head.encoder.layer.0.crossattention.output.LayerNorm.bias": 12,
            "tagging_head.encoder.layer.0.intermediate.dense.weight": 13,
            "tagging_head.encoder.layer.0.output.dense.bias": 14,
            "tagging_head.encoder.layer.0.output.LayerNorm.weight": 15,
            "tagging_head.embeddings.word_embeddings.weight": 16,                      # unused in tagging mode
            "tagging_head.encoder.layer.0.attention.self.query.weight": 17,            # self-attention: removed
            "text_encoder.something": 18,
        }}
        out = ts._ram_state(checkpoint)
        self.assertEqual(out["layers.1.q.weight"], 9)
        self.assertEqual(out["layers.1.v.bias"], 10)
        self.assertEqual(out["layers.0.o.weight"], 11)
        self.assertEqual(out["layers.0.ln1.bias"], 12)
        self.assertEqual(out["layers.0.fc1.weight"], 13)
        self.assertEqual(out["layers.0.fc2.bias"], 14)
        self.assertEqual(out["layers.0.ln2.weight"], 15)
        self.assertEqual({k: out[k] for k in ("image_proj.weight", "wordvec_proj.bias", "fc.weight", "label_embed",
                                              "reweight_scale")}, {"image_proj.weight": 4, "wordvec_proj.bias": 5,
                                                                   "fc.weight": 6, "label_embed": 7,
                                                                   "reweight_scale": 8})
        self.assertIn("visual_encoder.layers.0.blocks.0.attn.qkv.weight", out)
        self.assertEqual(len(out), 13)


@unittest.skipIf(np is None, "numpy and Pillow are needed")
class Hydra(unittest.TestCase):
    """The e621 tagger's numpy side: picture sizes, calibration from the validation counts, implications, the rows."""
    ts = None

    @classmethod
    def setUpClass(cls):
        cls.ts = load_service()

    def test_picture_sizes_follow_the_repositorys_naflex_grid(self):
        size = self.ts._hydra_size
        # expected values come from hydra/model.py's get_image_size_for_seq, which this is a port of (3,018 sizes agree)
        self.assertEqual(size(512, 512), (512, 512))                   # exactly 1,024 patches
        self.assertEqual(size(1440, 1080), (576, 432))                 # 36 x 27 patches, the aspect ratio kept
        self.assertEqual(size(1080, 1440), (432, 576))
        self.assertEqual(size(4000, 3000), (576, 432))
        self.assertEqual(size(720, 1280), (384, 672))
        self.assertEqual(size(100, 50), (96, 48))                      # small pictures are not enlarged (floored to 16)
        self.assertEqual(size(15, 40), (16, 32))
        self.assertEqual(size(10000, 100), (4800, 48))                 # extreme shapes still fit in 1,024 patches
        for h, w in ((1, 1), (333, 777), (6000, 40), (2047, 2049)):
            new_h, new_w = size(h, w)
            self.assertEqual((new_h % 16, new_w % 16), (0, 0))
            self.assertLessEqual(new_h // 16 * (new_w // 16), 1024)
            self.assertGreaterEqual(new_h, 16)

    def test_bf16_rounding_is_round_to_nearest_even(self):
        f = self.ts._round_bf16
        np.testing.assert_array_equal(f(np.array([1.0, 0.0, -2.5], np.float32)), [1.0, 0.0, -2.5])
        self.assertEqual(float(f(np.array([0.1], np.float32))[0]), 0.10009765625)
        self.assertEqual(float(f(np.array([1 + 2 ** -8], np.float32))[0]), 1.0)                 # a tie: to the even one
        self.assertEqual(float(f(np.array([1 + 3 * 2 ** -8], np.float32))[0]), 1 + 2 ** -6)    # a tie: to the even one
        self.assertEqual(float(f(np.array([1 + 2 ** -8 + 2 ** -20], np.float32))[0]), 1 + 2 ** -7)   # above the tie: up

    def test_labels_come_from_the_model_files_metadata(self):
        rows = self.ts._hydra_labels({"classifier.labels": "wolf species canis\ncanis species canine\nsolo general\n"
                                                           "xyz_(lore) lore"})
        self.assertEqual(rows, [("wolf", "species", ["canis"]), ("canis", "species", ["canine"]),
                                ("solo", "general", []), ("xyz_(lore)", "lore", [])])
        with self.assertRaises(ValueError):
            self.ts._hydra_labels({"classifier.labels": "lonely"})

    def test_thresholds_are_the_best_f1_with_at_least_ten_percent_precision(self):
        # tag x threshold x [tp, fp, tn, fn]; three thresholds: 0.25, 0.5, 0.75
        v = np.zeros((6, 3, 4), np.float32)
        v[0] = [[5, 5, 90, 0], [8, 2, 88, 2], [3, 0, 90, 7]]          # F1 .667, .800, .462: the best is 0.5
        v[1] = [[1, 50, 40, 0], [1, 60, 30, 0], [1, 70, 20, 0]]       # precision 2%, 1.6%, 1.4%: never returned
        # tag 2: no positives anywhere (a NaN precision): never returned
        v[3] = [[10, 190, 0, 0], [1, 9, 0, 40], [0, 0, 0, 50]]        # F1 .095 (precision 5%: out), .039 (10%), 0: so 0.5
        v[4] = [[9, 1, 90, 1], [5, 0, 91, 5], [2, 0, 93, 8]]          # F1 .9, .667, .333: the best is 0.25
        v[5] = [[1, 0, 98, 9], [1, 0, 98, 9], [1, 0, 98, 9]]          # a tie: the lowest threshold wins (the first maximum)
        logit_t = self.ts._hydra_thresholds(v)
        bf16 = lambda t: float(self.ts._round_bf16(np.atleast_1d(self.ts._logit(t)).astype(np.float32))[0])   # noqa: E731
        self.assertEqual(float(logit_t[0]), bf16(0.5))
        self.assertEqual(float(logit_t[0]), 0.0)
        self.assertGreater(float(logit_t[1]), 100)
        self.assertGreater(float(logit_t[2]), 100)
        self.assertEqual(float(logit_t[3]), 0.0)
        self.assertEqual(float(logit_t[4]), bf16(0.25))
        self.assertEqual(float(logit_t[5]), bf16(0.25))
        # a threshold nothing reaches keeps even a very sure logit below one half
        self.assertLess(float(self.ts._sigmoid(np.float64(30.0) - logit_t[1])), 0.001)
        # and at the tag's own threshold the calibrated score is exactly one half
        self.assertAlmostEqual(float(self.ts._sigmoid(logit_t[4] - logit_t[4])), 0.5)

    def test_implications_make_every_implied_tag_score_at_least_as_high(self):
        rows = ([("wolf", "species", ["canis"]), ("canis", "species", ["canine"]), ("canine", "species", ["mammal"]),
                           ("mammal", "species", []), ("solo", "general", ["not_a_tag"]), ("fox", "species", ["canine"])])
        src, dst = self.ts._implication_edges(rows)
        self.assertEqual(sorted(zip(src.tolist(), dst.tolist())),
                         [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3), (5, 2), (5, 3)])    # transitive, unknown tags end a chain
        scores = np.array([[0.9, 0.1, 0.2, 0.3, 0.8, 0.4]], np.float32)       # wolf .9, fox .4, solo .8
        self.ts._inherit(scores, src, dst)
        np.testing.assert_allclose(scores[0], [0.9, 0.9, 0.9, 0.9, 0.8, 0.4])   # a wolf is a canis, a canine and a mammal
        again = scores.copy()
        self.ts._inherit(again, src, dst)
        np.testing.assert_array_equal(again, scores)                              # idempotent

    def test_a_cycle_in_the_implications_does_not_hang(self):
        src, dst = self.ts._implication_edges(([("a", "general", ["b"]), ("b", "general", ["a"])]))
        self.assertEqual(sorted(zip(src.tolist(), dst.tolist())), [(0, 1), (1, 0)])
        scores = np.array([[0.7, 0.2]], np.float32)
        self.ts._inherit(scores, src, dst)
        np.testing.assert_allclose(scores[0], [0.7, 0.7])

    def test_rows_hold_the_wanted_categories_best_first_and_never_below_the_models_minimum(self):
        names = np.array(["wolf", "canine", "male", "anthro", "pulchra_fellini", "artist_x"], dtype=object)
        keys = {"general": np.array([2, 3]), "species": np.array([0, 1]), "character": np.array([4])}   # artist_x: dropped
        cal = np.array([[0.95, 0.6, 0.55, 0.9, 0.21, 0.99], [0.1, 0.19, 0.5, 0.05, 0.3, 0.8]])
        rows = self.ts._hydra_rows(cal, names, keys, 0.05)
        self.assertEqual(rows[0], {"general": {"anthro": 0.9, "male": 0.55}, "species": {"wolf": 0.95, "canine": 0.6},
                                   "character": {"pulchra_fellini": 0.21}})
        self.assertEqual(list(rows[0]["general"]), ["anthro", "male"])             # best first
        self.assertEqual(rows[1], {"general": {"male": 0.5}, "species": {}, "character": {"pulchra_fellini": 0.3}})
        # a higher floor from the panel is respected, a lower one cannot go under the minimum (0.2)
        self.assertEqual(self.ts._hydra_rows(cal, names, keys, 0.5)[0]["species"], {"wolf": 0.95, "canine": 0.6})
        self.assertEqual(self.ts._hydra_rows(cal, names, keys, 0.5)[0]["character"], {})
        self.assertEqual(self.ts.E621_MIN_SCORE, 0.2)

    def test_the_resize_matrix_is_the_magic_kernel_sharp(self):
        try:
            import torch  # noqa: F401 - the service imports it lazily too
        except ImportError:
            self.skipTest("torch is not installed")
        m = self.ts._mks_matrix(1000, 400).numpy()
        self.assertEqual(m.shape, (400, 1000))
        np.testing.assert_allclose(m.sum(axis=1), 1.0, atol=1e-5)                  # every output pixel is a weighted mean
        self.assertLess(m.min(), 0.0)                                              # the sharpening lobes are negative
        mid = m[200]
        self.assertAlmostEqual(float(mid.argmax()), 500.0, delta=2)
        self.assertLessEqual(int((mid != 0).sum()), int(5 * 2.5) + 3)              # support 2.5 pixels of the output each side
        # the kernel itself, at a shrink factor of 1: 17/16 at 0, 0 at +-1, -1/32 at +-2
        k = self.ts._mks_matrix(50, 50).numpy()[25]
        np.testing.assert_allclose([k[23], k[24], k[25], k[26], k[27]], [-1 / 32, 0.0, 17 / 16, 0.0, -1 / 32], atol=1e-6)
        edge = self.ts._mks_matrix(50, 25).numpy()[0]                              # the first row: edge pixels are replicated
        self.assertAlmostEqual(float(edge.sum()), 1.0, places=5)
        self.assertGreaterEqual(float(edge[0]), 0.45)                              # (replicated pixels pile up on it)


@unittest.skipIf(np is None, "numpy and Pillow are needed")
class Preparation(unittest.TestCase):
    ts = None

    @classmethod
    def setUpClass(cls):
        cls.ts = load_service()

    def test_each_model_gets_its_own_input(self):
        out = self.ts._prepare(picture((300, 200)), ["wd", "pixai", "ram", "e621"], 448, 1008)
        self.assertEqual(out["wd"].shape, (448, 448, 3))
        self.assertEqual(out["pixai"].shape, (200, 300, 3))            # PixAI scales and pads on the GPU
        self.assertEqual(out["ram"].shape, (384, 384, 3))              # RAM++: squashed to 384 x 384
        self.assertEqual(out["e621"].shape, (200, 300, 3))             # Hydra: the GPU resizes it to its patch grid
        for key in out:
            self.assertEqual(out[key].dtype, np.uint8)
        self.assertTrue(out["pixai"].flags.writeable)
        self.assertTrue(out["e621"].flags.writeable)

    def test_only_the_wanted_models_are_prepared(self):
        self.assertEqual(sorted(self.ts._prepare(picture(), ["ram"], 448, 1008)), ["ram"])
        self.assertEqual(sorted(self.ts._prepare(picture(), ["wd", "pixai"], 448, 1008)), ["pixai", "wd"])
        self.assertEqual(sorted(self.ts._prepare(picture(), ["e621"], 448, 1008)), ["e621"])

    def test_hydra_gets_a_huge_picture_box_shrunk_by_a_whole_factor_and_others_as_they_are(self):
        small = self.ts._prepare(picture((1500, 1000)), ["e621"], 448, 1008)["e621"]
        self.assertEqual(small.shape, (1000, 1500, 3))                  # up to ~2,000 pixels: kept
        huge = self.ts._prepare(picture((5000, 3000)), ["e621"], 448, 1008)["e621"]
        self.assertEqual(huge.shape, (750, 1250, 3))                    # box shrink by 4 (the GPU does the final resize)

    def test_wd_pads_to_a_white_square(self):
        out = self.ts._prepare(picture((100, 20), color=(0, 0, 0)), ["wd"], 448, 1008)["wd"]
        self.assertTrue((out[0, 0] == 255).all())                      # the padding above the picture
        self.assertTrue((out[224, 224] == 0).all())                    # the picture in the middle

    def test_transparency_is_flattened_onto_white_and_grey_is_accepted(self):
        rgba = self.ts._prepare(picture((10, 10), "RGBA", (0, 0, 0, 0)), ["ram"], 448, 1008)["ram"]
        self.assertTrue((rgba == 255).all())
        grey = self.ts._prepare(picture((10, 10), "L", 90), ["ram"], 448, 1008)["ram"]
        self.assertEqual(grey.shape, (384, 384, 3))


@unittest.skipIf(np is None, "numpy and Pillow are needed")
class OutOfMemorySplitting(unittest.TestCase):
    ts = None

    @classmethod
    def setUpClass(cls):
        cls.ts = load_service()

    def tagger(self, **batch):
        t = self.ts.Tagger.__new__(self.ts.Tagger)                     # no loading thread, no models
        t.batch = {"wd": 8, "pixai": 8, "ram": 8, "e621": 8, **batch}
        t.last_used = 0.0
        return t

    def job(self, n, models):
        return self.ts.Job([{m: i for m in models} for i in range(n)], [None] * n, list(models), 0.05)

    def test_a_too_big_batch_is_split_and_the_micro_batch_stays_lowered(self):
        t = self.tagger()
        retried_inside_an_exception = []
        calls = []

        def forward(arrays, floor):
            calls.append(len(arrays))
            if len(calls) > 1:
                retried_inside_an_exception.append(sys.exc_info()[0] is not None)
            if len(arrays) > 2:
                raise RuntimeError("CUDA out of memory. Tried to allocate 82.00 MiB")
            return [{"general": {f"tag{a}": 0.9}} for a in arrays]

        t._forward_ram = forward
        results, errors = t._process(self.job(8, ["ram"]))
        self.assertEqual(errors, [None] * 8)
        self.assertEqual([r["ram"]["general"] for r in results], [{f"tag{i}": 0.9} for i in range(8)])
        self.assertEqual(t.batch["ram"], 2)
        self.assertEqual(t.batch["wd"], 8)                              # another model's micro-batch is untouched
        self.assertEqual(calls[0], 8)
        # The retries must run after the failed attempt's exception is gone: while it is alive, its traceback keeps
        # the failed batch's tensors in GPU memory (measured: even a single picture then ran out of memory).
        self.assertFalse(any(retried_inside_an_exception))

    def test_a_picture_that_never_fits_fails_only_its_own_slot(self):
        t = self.tagger()

        def forward(arrays, floor):
            if 3 in arrays:                                             # picture number 3 never fits
                raise RuntimeError("CUDA out of memory")
            return [{"general": {"ok": 1.0}} for _ in arrays]

        t._forward_ram = forward
        results, errors = t._process(self.job(5, ["ram"]))
        self.assertIsNone(results[3])
        self.assertIn("out of memory", errors[3])
        self.assertTrue(errors[3].startswith("ram:"))
        self.assertEqual([errors[i] for i in (0, 1, 2, 4)], [None] * 4)
        self.assertTrue(all(results[i] for i in (0, 1, 2, 4)))

    def test_another_error_splits_without_touching_the_micro_batch(self):
        t = self.tagger()

        def forward(arrays, floor):
            if 1 in arrays:
                raise ValueError("bad picture")
            return [{"general": {}} for _ in arrays]

        t._forward_ram = forward
        results, errors = t._process(self.job(4, ["ram"]))
        self.assertEqual(errors[1], "ram: ValueError: bad picture")
        self.assertEqual(t.batch["ram"], 8)
        self.assertIsNone(results[1])
        self.assertEqual([r is not None for r in results], [True, False, True, True])

    def test_the_e621_model_splits_on_out_of_memory_like_the_others(self):
        t = self.tagger()
        calls = []

        def forward(arrays, floor):
            calls.append(len(arrays))
            if len(arrays) > 4:
                raise RuntimeError("CUDA out of memory. Tried to allocate 1.40 GiB")
            return [{"general": {f"tag{a}": 0.9}, "species": {}, "character": {}, "copyright": {}} for a in arrays]

        t._forward_e621 = forward
        results, errors = t._process(self.job(8, ["e621"]))
        self.assertEqual(errors, [None] * 8)
        self.assertEqual([next(iter(r["e621"]["general"])) for r in results], [f"tag{i}" for i in range(8)])
        self.assertEqual(t.batch["e621"], 4)
        self.assertEqual(t.batch["pixai"], 8)
        self.assertEqual(calls[:3], [8, 4, 4])

    def test_each_model_answers_under_its_own_key_and_a_failed_model_fails_the_slot(self):
        t = self.tagger()
        t._forward_wd = lambda arrays, floor: [{"general": {"a": 0.6}, "character": {}, "rating": {}} for _ in arrays]
        t._forward_pixai = lambda arrays, floor: [{"general": {"b": 0.7}, "character": {}, "copyright": {},
                                                   "rating": {}} for _ in arrays]
        t._forward_ram = lambda arrays, floor: [{"general": {"c": 0.8}} for _ in arrays]
        t._forward_e621 = lambda arrays, floor: [{"general": {"anthro": 0.9}, "species": {"wolf": 0.7}, "character": {},
                                                  "copyright": {}} for _ in arrays]
        results, errors = t._process(self.job(2, ["wd", "pixai", "ram", "e621"]))
        self.assertEqual(errors, [None, None])
        self.assertEqual(sorted(results[0]), ["e621", "pixai", "ram", "wd"])
        self.assertEqual(results[0]["ram"], {"general": {"c": 0.8}})
        self.assertNotIn("rating", results[0]["ram"])
        self.assertEqual(sorted(results[1]["e621"]), ["character", "copyright", "general", "species"])
        self.assertNotIn("rating", results[1]["e621"])

        def broken(arrays, floor):
            raise RuntimeError("boom")
        t._forward_ram = broken
        results, errors = t._process(self.job(2, ["wd", "ram"]))
        self.assertEqual(results, [None, None])
        self.assertEqual(errors, ["ram: RuntimeError: boom"] * 2)


class FakeTagger:
    """What the HTTP handler needs from the Tagger."""

    def __init__(self, ts, status="ok"):
        self.status, self.error, self.models, self.busy = status, "", [{"kind": "ram"}], 0
        self.batch = {"wd": 8, "pixai": 8, "ram": 8, "e621": 8}
        self.last_used = 0.0
        self.wd, self.pixai = {"size": 448}, {"size": 1008}
        self.jobs = []
        self.ts = ts

    def touch(self):
        pass

    enter = leave = touch

    def submit(self, job):
        self.jobs.append(job)
        n = len(job.inputs)
        results = [None if job.errors[i] else {m: {"general": {"x": 0.9}} for m in job.models} for i in range(n)]
        return results, list(job.errors)


@unittest.skipIf(np is None, "numpy and Pillow are needed")
class Http(unittest.TestCase):
    ts = None

    @classmethod
    def setUpClass(cls):
        cls.ts = load_service()
        cls.fake = FakeTagger(cls.ts)
        cls.ts.TAGGER = cls.fake
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.ts.Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.base + "/tag", data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as err:
            return err.code, json.load(err)

    def test_health_lists_the_micro_batch_of_every_model(self):
        with urllib.request.urlopen(self.base + "/health", timeout=20) as resp:
            health = json.load(resp)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["effectiveBatch"], {"wd": 8, "pixai": 8, "ram": 8, "e621": 8})

    def test_all_four_models_by_default_in_registry_order(self):
        status, out = self.post({"images": [picture()]})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.jobs[-1].models, ["wd", "pixai", "ram", "e621"])
        self.assertEqual(sorted(out["results"][0]), ["e621", "pixai", "ram", "wd"])

    def test_the_models_field_selects_and_orders(self):
        status, out = self.post({"images": [picture()], "models": ["ram", "wd"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.jobs[-1].models, ["wd", "ram"])
        self.assertEqual(sorted(out["results"][0]), ["ram", "wd"])
        status, out = self.post({"images": [picture()], "models": ["ram"]})
        self.assertEqual(sorted(out["results"][0]), ["ram"])
        self.assertEqual(self.fake.jobs[-1].floor, 0.05)
        status, out = self.post({"images": [picture()], "models": ["e621", "wd"], "floor": 0.3})
        self.assertEqual(status, 200)
        self.assertEqual(self.fake.jobs[-1].models, ["wd", "e621"])
        self.assertEqual(sorted(out["results"][0]), ["e621", "wd"])
        self.assertEqual(self.fake.jobs[-1].floor, 0.3)

    def test_bad_requests(self):
        for body in ({"images": [picture()], "models": ["rams"]}, {"images": [picture()], "models": "ram"},
                     {"images": [picture()], "models": ["wd", 3]}):
            status, out = self.post(body)
            self.assertEqual(status, 400, body)
            self.assertIn('"ram"', out["error"])
            self.assertIn('"e621"', out["error"])
        self.assertEqual(self.post({"images": []})[0], 400)
        self.assertEqual(self.post({"images": [picture()], "floor": "x"})[0], 400)
        self.assertEqual(self.post(b"{not json")[0], 400)
        self.assertEqual(self.post({"images": [picture()] * 65})[0], 400)

    def test_an_unreadable_picture_fails_only_its_own_slot(self):
        junk = base64.b64encode(b"not an image").decode()
        status, out = self.post({"images": [picture(), "not base64 !!", junk, picture()], "floor": 0.5})
        self.assertEqual(status, 200)
        self.assertEqual(out["errors"], [None, "invalid base64", "cannot identify image file", None])
        self.assertEqual([r is None for r in out["results"]], [False, True, True, False])
        self.assertEqual(self.fake.jobs[-1].floor, 0.5)

    def test_while_loading_the_answer_is_503(self):
        original = self.ts.TAGGER
        self.ts.TAGGER = FakeTagger(self.ts, status="loading")
        try:
            status, out = self.post({"images": [picture()]})
        finally:
            self.ts.TAGGER = original
        self.assertEqual(status, 503)
        self.assertEqual(out["status"], "loading")


if __name__ == "__main__":
    unittest.main()
