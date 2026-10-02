import base64
import inspect
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from immich_organizer import aitagger as at
from immich_organizer import searchplus as sp
from immich_organizer.client import ImmichClient
from tests.fake_immich import API_KEY, FakeImmich

# ---------------------------------------------------------------- fakes
#
# A "picture" is bytes like  b"wd:girl=0.9,beach=0.6|char:miku=0.8|pixai:beach=0.8|rating:general=0.9":
# what the tagger would answer for it. An asset's captures are separated by ";" in the catalogue's `preview`.
# Parts: wd (WD general), char (WD character), rating (WD rating), pixai (PixAI general), pchar (PixAI character),
# copy (PixAI copyright), prating (PixAI rating; left out, PixAI says nothing about the rating).

PHOTO = ("wd:girl=0.9,beach=0.6,solo=0.8,hat=0.3|char:miku=0.8|pixai:beach=0.8,sea=0.7,wave=0.4|"
         "rating:general=0.9,sensitive=0.08,questionable=0.01,explicit=0.01")
DOG = "wd:dog=0.9|pixai:dog=0.95,grass=0.7|rating:general=0.95,sensitive=0.05"
CLIP = ";".join(["wd:dog=0.9|pixai:dog=0.9,car=0.6|rating:general=0.9",
                "wd:dog=0.2|pixai:car=0.9|rating:general=0.9",
                "wd:dog=0.9|pixai:car=0.3|rating:sensitive=0.9,general=0.1"])


def read_picture(data: bytes):
    """(the tagger's answer for this picture, None) or (None, why)."""
    text = data.decode()
    if text == "broken":
        return None, "cannot identify image file"
    out = {"wd": {"general": {}, "character": {}, "rating": {}},
           "pixai": {"general": {}, "character": {}, "copyright": {}, "rating": {}}}
    where = {"wd": ("wd", "general"), "char": ("wd", "character"), "rating": ("wd", "rating"),
             "pixai": ("pixai", "general"), "pchar": ("pixai", "character"), "copy": ("pixai", "copyright"),
             "prating": ("pixai", "rating")}
    for part in filter(None, text.split("|")):
        key, _, rest = part.partition(":")
        model, category = where[key]
        out[model][category] = {k: float(v) for k, v in (kv.split("=") for kv in rest.split(",") if kv)}
    return out, None


def raw_of(*pictures: str) -> dict:
    """The stored ``raw`` for an asset whose captures are these pictures."""
    got = [read_picture(p.encode()) for p in pictures]
    return at.raw_from_results([g[0] for g in got], [g[1] for g in got])


def S(**changes) -> dict:
    """Default settings with some changes (not validated: for the pure functions)."""
    return {**json.loads(json.dumps(at.DEFAULTS)), **changes}


def rule(**kw) -> dict:
    return {"if_all": [], "if_any": [], "unless": [], "add": [], "remove": [], **kw}


def tags_of(result: dict) -> list[str]:
    return [t["tag"] for t in result["tags"]]


class FakeServices:
    """Stands in for aitagger.Services: no docker, no GPU."""

    def __init__(self):
        self.tag_calls: list[int] = []           # pictures per /tag request that was answered
        self.oom_calls: list[int] = []           # pictures per /tag request that ran out of memory
        self.vlm_calls: list[dict] = []
        self.ready_calls: list[dict] = []
        self.loads = self.unloads = 0
        self.down = False                        # the tagger / containers are not answering
        self.vlm_down = False
        self.oom_above: int | None = None        # a /tag request with more pictures than this is out of memory
        self.answer = lambda tags, rating, settings: {"description": "A nice picture.", "add_tags": [],
                                                      "remove_tags": [], "note": ""}
        self._lock = threading.Lock()

    def ensure_ready(self, need_tagger=True, need_vlm=True, wait=0, progress=None, stop=None):
        self.ready_calls.append({"tagger": need_tagger, "vlm": need_vlm})
        if self.down:
            raise at.ServiceDown("down")

    def tag(self, images):
        if self.down:
            raise at.ServiceDown("down")
        if self.oom_above is not None and len(images) > self.oom_above:
            with self._lock:
                self.oom_calls.append(len(images))
            raise at.GpuOOM("CUDA out of memory")
        with self._lock:
            self.tag_calls.append(len(images))
        got = [read_picture(b) for b in images]
        return [g[0] for g in got], [g[1] for g in got]

    def describe(self, tags, rating, settings, kind="IMAGE", terms=None):
        if self.vlm_down:
            raise at.ServiceDown("the language model is not answering")
        with self._lock:
            self.vlm_calls.append({"tags": tags, "rating": rating, "kind": kind, "terms": terms, "settings": settings})
        return self.answer(tags, rating, settings)

    def load(self, need_tagger=True, need_vlm=True):
        self.loads += 1
        return ["immich_aitagger", "immich_aitagger_vlm"]

    def unload(self):
        self.unloads += 1
        return ["immich_aitagger_vlm", "immich_aitagger"]

    def touch(self):
        pass

    def idle_check(self, now=None):
        return False

    def status(self):
        return {"tagger": {"container": "stopped", "status": "down", "error": None},
                "vlm": {"container": "stopped", "status": "down", "error": None},
                "gpu": {"totalGb": 24, "usedGb": 1.0}, "searchplusRunning": False}


def fake_frames(item, n):
    if item["preview"] == "MISSING":
        raise FileNotFoundError("no preview image")
    if item["preview"] == "NOTMEDIA":
        raise ValueError("not a picture")
    return [p.encode() for p in item["preview"].split(";")], "video" if item["type"] == "VIDEO" else "image"


def catalog_for(ids: list[str]) -> list[dict]:
    rows = [("IMAGE", PHOTO, "2026-01-05 10:00:00"), ("IMAGE", DOG, "2026-01-04 10:00:00"),
            ("VIDEO", CLIP, "2026-01-03 10:00:00"), ("IMAGE", "MISSING", "2025-12-01 10:00:00"),
            ("IMAGE", "broken", "2025-11-01 10:00:00")]
    return [{"id": ids[i], "type": t, "taken": taken, "name": f"IMG_{i:04d}.jpg", "preview": pic, "original": "",
             "duration_ms": 9000 if t == "VIDEO" else 0} for i, (t, pic, taken) in enumerate(rows)]


class Base(unittest.TestCase):
    """A store in a temp folder, the fake Immich, the fake model containers and a real Indexer on top."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"IMMICH_ORGANIZER_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fake = FakeImmich().start()
        self.addCleanup(self.fake.stop)
        self.client = ImmichClient(self.fake.url, API_KEY, retries=0)
        self.ids = [a["id"] for a in self.fake.assets]
        self.store = at.Store(Path(self.tmp.name) / "at")
        self.services = FakeServices()
        self.catalog = catalog_for(self.ids)
        self.indexer = at.Indexer(self.store, self.services, client=self.client, catalog=lambda: self.catalog,
                                  frames=fake_frames)
        self.indexer.DROP_WAIT = 0.01
        self.addCleanup(self.store.conn.close)
        self.addCleanup(self.indexer.stop, 5)

    def set(self, **changes) -> list[str]:
        return at.apply_settings(changes, self.store)[1]

    def run_indexer(self, **settings):
        """Tag everything that is waiting, then stop (``keep_updated`` off makes the thread end by itself)."""
        self.set(keep_updated=False, indexing=True, **settings)
        self.indexer.start()
        self.indexer.thread.join(30)
        self.assertFalse(self.indexer.running(), "the indexer did not finish")

    def description(self, i: int) -> str:
        return (self.fake.by_id[self.ids[i]].get("exifInfo") or {}).get("description") or ""

    def put_description(self, i: int, text: str) -> None:
        self.fake.by_id[self.ids[i]].setdefault("exifInfo", {})["description"] = text


# ---------------------------------------------------------------- names, vocabulary, settings

class TestNames(unittest.TestCase):
    def test_tags_are_normalised(self):
        self.assertEqual(at.norm_tag("Long_Hair"), "long hair")
        self.assertEqual(at.norm_tag("hatsune_miku_(vocaloid)"), "hatsune miku (vocaloid)")
        self.assertEqual(at.norm_tag("  A  Dog. "), "a dog")
        self.assertEqual(at.norm_tag("black/white, ok"), "black white ok")
        self.assertEqual(at.norm_tag("[/AI Tagger]"), "( ai tagger)")          # a tag can't forge the block markers
        self.assertEqual(at.norm_tag("_"), "")
        self.assertLessEqual(len(at.norm_tag("x" * 200)), 60)

    def test_vocabulary_renames_and_preferred_terms(self):
        v = at.Vocabulary("1girl -> Girl\n  solo   -> alone \nhorse\n\n-> nothing\nbad ->\nsame -> same\nA_B -> c_d")
        self.assertEqual(v.renames, {"1girl": "girl", "solo": "alone", "a b": "c d"})
        self.assertEqual(v.terms, ["girl", "alone", "horse", "c d"])      # half a rename is ignored
        self.assertEqual(v.rename("1girl"), "girl")
        self.assertEqual(v.rename("dog"), "dog")
        self.assertEqual(at.Vocabulary("a → b").renames, {"a": "b"})


class TestSettings(Base):
    def test_defaults_match_the_contract(self):
        s = at.load_settings()
        self.assertEqual(s, at.DEFAULTS)
        self.assertEqual((s["video_frames"], s["batch_size"], s["vlm_parallel"], s["vram_gb"]),
                         (6, 8, 16, at.VRAM_GB_DEFAULT))
        self.assertEqual((s["use_wd"], s["use_pixai"], s["wd_strictness"], s["pixai_strictness"]), (True, True, 0.5, 0.5))
        self.assertNotIn("use_ram", s)
        self.assertNotIn("ram_strictness", s)
        self.assertEqual((s["indexing"], s["keep_updated"], s["write_tags"], s["language"]), (False, True, False, "English"))
        s["blocked"].append("x")                       # callers can't change the defaults by accident
        self.assertEqual(at.load_settings()["blocked"], [])

    def test_types_are_checked_explicitly(self):
        for key, bad in [("describe", "false"), ("describe", 0), ("describe", None), ("video_frames", "6"),
                         ("video_frames", True), ("video_frames", 2.5), ("wd_strictness", "0.5"),
                         ("wd_strictness", True), ("wd_strictness", float("nan")), ("instructions", 5),
                         ("language", ""), ("language", "x" * 41), ("instructions", "x" * 4001),
                         ("vocabulary", ["a"]), ("blocked", "cat"), ("blocked", [1]), ("rules", {}), ("nope", 1)]:
            with self.assertRaises(ValueError, msg=f"{key}={bad!r}"):
                at.save_settings({key: bad}, self.store)
        self.assertEqual(at.load_settings(), at.DEFAULTS)         # nothing was saved
        s = at.save_settings({"video_frames": 2.0, "wd_strictness": 0.5, "pixai_strictness": 1 - 0.1, "describe": False},
                             self.store)
        self.assertEqual((s["video_frames"], s["wd_strictness"], s["pixai_strictness"], s["describe"]), (2, 0.5, 0.9, False))
        self.assertIsInstance(s["video_frames"], int)
        self.assertEqual(at.save_settings({"wd_strictness": 1 - 0.5}, self.store)["wd_strictness"], 0.5)

    def test_limits(self):
        for key, lo, hi in [("video_frames", 1, 8), ("batch_size", 1, 64), ("vlm_parallel", 1, 32),
                            ("vram_gb", *at.VRAM_GB_LIMITS), ("max_tags", 5, 100), ("wd_strictness", 0.05, 0.95),
                            ("pixai_strictness", 0.05, 0.95)]:
            self.assertEqual(at.LIMITS[key], (lo, hi))
            for ok in (lo, hi):
                self.assertEqual(at.save_settings({key: ok}, self.store)[key], ok)
            for bad in (lo - 0.5 if isinstance(lo, float) else lo - 1, hi + 1):
                with self.assertRaises(ValueError):
                    at.save_settings({key: bad}, self.store)

    def test_one_bad_change_saves_nothing(self):
        with self.assertRaises(ValueError):
            at.save_settings({"max_tags": 10, "video_frames": 99}, self.store)
        self.assertEqual(at.load_settings()["max_tags"], 30)
        self.assertEqual(self.store.settings_version, 1)

    def test_text_is_kept_as_typed_but_newlines_are_unix(self):
        s = at.save_settings({"instructions": "Be brief.\r\nNo names.", "language": "  German "}, self.store)
        self.assertEqual((s["instructions"], s["language"]), ("Be brief.\nNo names.", "German"))

    def test_the_version_goes_up_only_for_content_settings(self):
        self.assertEqual(self.store.settings_version, 1)
        for changes in ({"indexing": True}, {"keep_updated": False}, {"batch_size": 4}, {"vlm_parallel": 4},
                        {"vram_gb": at.VRAM_GB_LIMITS[0] + 1}):
            self.assertEqual(self.set(**changes), [])
        self.assertEqual(self.store.settings_version, 1)
        self.assertEqual(self.set(max_tags=12), ["max_tags"])
        self.assertEqual(self.store.settings_version, 2)
        self.assertEqual(self.set(max_tags=12), [])                    # same value: not a change
        self.assertEqual(self.store.settings_version, 2)
        self.assertEqual(sorted(self.set(instructions="x", language="French", describe=False)),
                         ["describe", "instructions", "language"])
        self.assertEqual(self.store.settings_version, 3)               # one bump per save
        for key, value in [("video_frames", 2), ("use_wd", False), ("use_pixai", False), ("wd_strictness", 0.6),
                           ("pixai_strictness", 0.6), ("character_tags", False), ("rating_tag", False),
                           ("vocabulary", "a -> b"), ("blocked", ["cat"]),
                           ("rules", [rule(if_all=["a"], add=["b"])]), ("write_tags", True)]:
            before = self.store.settings_version
            self.assertEqual(self.set(**{key: value}), [key])
            self.assertEqual(self.store.settings_version, before + 1, key)
        self.assertEqual(at.Store(self.store.folder).settings_version, self.store.settings_version)   # persisted

    def test_blocked_and_rules_are_cleaned_up(self):
        s = at.save_settings({"blocked": ["Cat", "cat", " Long_Hair ", ""],
                              "rules": [{"if_all": ["Girl", "girl"], "add": ["Woman_"], "unless": ["Night"]}]},
                             self.store)
        self.assertEqual(s["blocked"], ["cat", "long hair"])
        self.assertEqual(s["rules"], [rule(if_all=["girl"], add=["woman"], unless=["night"])])

    def test_bad_rules_are_refused_with_the_rule_number(self):
        for bad, text in [([rule(add=["x"])], "Rule 1"), ([rule(if_all=["x"])], "Rule 1"),
                          ([rule(if_all=["x"], add=["a"]), rule(if_any=["x"], add=["a"], remove=["a"])], "Rule 2"),
                          ([{"if_all": ["x"], "add": ["a"], "then": 1}], "unknown field"),
                          (["girl"], "must be an object"), ([{"if_all": "girl", "add": ["a"]}], "list of tags"),
                          ([rule(if_all=["x"], add=["a"])] * 101, "at most 100")]:
            with self.assertRaises(ValueError) as ctx:
                at.save_settings({"rules": bad}, self.store)
            self.assertIn(text, str(ctx.exception))

    def test_a_hand_edited_bad_value_falls_back_to_the_default(self):
        at.settings_path().write_text(json.dumps({"max_tags": 9999, "describe": "no", "video_frames": 3, "zzz": 1}))
        s = at.load_settings()
        self.assertEqual((s["max_tags"], s["describe"], s["video_frames"]), (30, True, 3))
        at.settings_path().write_text("[1, 2]")
        self.assertEqual(at.load_settings(), at.DEFAULTS)

    def test_a_settings_file_from_v1_still_loads(self):
        # written by the RAM++ version: use_ram / ram_strictness are unknown now, and its vram_gb (18-21) is out of range
        at.settings_path().write_text(json.dumps({"use_ram": False, "ram_strictness": 0.9, "vram_gb": 20, "max_tags": 12,
                                                  "use_wd": False, "instructions": "Be brief."}))
        s = at.load_settings()
        self.assertEqual((s["use_wd"], s["max_tags"], s["instructions"]), (False, 12, "Be brief."))     # what still fits
        self.assertEqual((s["use_pixai"], s["pixai_strictness"], s["vram_gb"]), (True, 0.5, at.VRAM_GB_DEFAULT))
        self.assertEqual(set(s), set(at.DEFAULTS))                                  # the old keys are dropped
        saved = at.save_settings({"max_tags": 13}, self.store)                       # saving works and cleans the file up
        self.assertEqual(saved["max_tags"], 13)
        self.assertEqual(set(json.loads(at.settings_path().read_text("utf-8"))), set(at.DEFAULTS))
        for old in ("use_ram", "ram_strictness"):                                    # but they are no longer settings
            with self.assertRaises(ValueError) as err:
                at.save_settings({old: True}, self.store)
            self.assertIn("Unknown", str(err.exception))

    def test_the_cheapest_reprocess_mode_for_the_changed_keys(self):
        self.assertEqual(at.suggest_mode([]), "none")
        self.assertEqual(at.suggest_mode(["max_tags", "blocked", "rules"]), "retag")
        self.assertEqual(at.suggest_mode(["rules", "instructions"]), "describe")
        self.assertEqual(at.suggest_mode(["language"]), "describe")
        self.assertEqual(at.suggest_mode(["instructions", "video_frames"]), "full")
        self.assertEqual(at.suggest_mode(["use_pixai"]), "full")
        self.assertEqual(at.suggest_mode(["pixai_strictness"]), "retag")
        every = {k for keys in at.REPROCESS.values() for k in keys}
        self.assertEqual(every, set(at.CONTENT))            # every content setting has a mode

    def test_write_tags_is_remembered_so_the_native_tags_stay_in_step(self):
        self.assertEqual(self.store.meta("native_tags"), "")
        self.set(write_tags=True)
        self.set(write_tags=False)
        self.assertEqual(self.store.meta("native_tags"), "1")


# ---------------------------------------------------------------- scores -> tags

class TestAggregation(unittest.TestCase):
    def kept(self, raw, **settings):
        return {t: v[0] for t, v in at.detect(raw, S(rating_tag=False, **settings))["tags"].items()}

    def test_combined_is_half_median_half_max(self):
        self.assertEqual(at._combine([0.7]), 0.7)
        self.assertAlmostEqual(at._combine([0.9, 0.6, 0.0]), 0.5 * 0.6 + 0.5 * 0.9)
        self.assertAlmostEqual(at._combine([0.8, 0.0]), 0.6)                    # median of two = their mean
        self.assertAlmostEqual(at._combine([0.9, 0.0, 0.0]), 0.45)

    def test_one_capture_keeps_the_calibrated_score_and_the_threshold(self):
        raw = raw_of(PHOTO)
        got = self.kept(raw)
        self.assertEqual(got, {"girl": 0.9, "beach": 0.8, "solo": 0.8, "miku": 0.8, "sea": 0.7})
        self.assertNotIn("hat", got)                                         # 0.3 < 0.5
        self.assertNotIn("wave", got)
        self.assertEqual(self.kept(raw, wd_strictness=0.25)["hat"], 0.3)
        self.assertNotIn("wave", self.kept(raw, wd_strictness=0.25))        # strictness is per model
        self.assertEqual(self.kept(raw, pixai_strictness=0.35)["wave"], 0.4)
        self.assertEqual(self.kept(raw, wd_strictness=0.95).keys(), {"beach", "sea"})        # PixAI ones only

    def test_a_tag_missing_from_a_capture_counts_as_zero(self):
        raw = raw_of(*CLIP.split(";"))
        got = self.kept(raw)
        self.assertAlmostEqual(got["dog"], 0.9)                  # wd: 0.9, 0.2, 0.9 -> median 0.9, max 0.9
        self.assertAlmostEqual(got["car"], 0.75)                 # pixai: 0.6, 0.9, 0.3 -> median 0.6, max 0.9
        self.assertNotIn("grass", got)
        # a tag seen in one capture only: (0 + 0.9) / 2 = 0.45 -> dropped at 0.5, kept at 0.4
        one_of_three = raw_of("pixai:cat=0.9", "pixai:dog=0.2", "pixai:dog=0.2")
        self.assertNotIn("cat", self.kept(one_of_three))
        self.assertAlmostEqual(self.kept(one_of_three, pixai_strictness=0.4)["cat"], 0.45)

    def test_the_rating_is_the_mean_probability_then_the_best(self):
        found = at.detect(raw_of(*CLIP.split(";")), S())
        self.assertEqual(found["tags"]["rating: general"][1], "wd")
        self.assertAlmostEqual(found["tags"]["rating: general"][0], (0.9 + 0.9 + 0.1) / 3)
        self.assertAlmostEqual(found["display"]["rating"]["sensitive"], 0.3)
        flip = at.detect(raw_of("wd:a=0.9|rating:general=0.4,explicit=0.6", "wd:a=0.9|rating:general=0.5,explicit=0.3"), S())
        self.assertIn("rating: general", flip["tags"])           # (0.9 vs 0.45)
        self.assertNotIn("rating: general", at.detect(raw_of(PHOTO), S(rating_tag=False))["tags"])

    def test_the_rating_is_the_mean_of_wd_and_pixai_each_averaged_over_the_captures(self):
        # WD: general .8 and .6 -> .7, explicit .2 and .4 -> .3.  PixAI: general .1 and .3 -> .2, explicit .9 and .7 -> .8
        captures = ("wd:a=0.9|rating:general=0.8,explicit=0.2|prating:general=0.1,explicit=0.9",
                    "wd:a=0.9|rating:general=0.6,explicit=0.4|prating:general=0.3,explicit=0.7")
        found = at.detect(raw_of(*captures), S())
        shown = found["display"]["rating"]
        self.assertAlmostEqual(shown["general"], (0.7 + 0.2) / 2, places=3)                # .45
        self.assertAlmostEqual(shown["explicit"], (0.3 + 0.8) / 2, places=3)               # .55
        score, source = found["tags"]["rating: explicit"]                                    # the argmax of the mean
        self.assertAlmostEqual(score, 0.55)
        self.assertNotIn("rating: general", found["tags"])
        self.assertEqual(source, "pixai")                                                    # the model surest of the winner
        # one model alone decides when the other is switched off
        wd_only = at.detect(raw_of(*captures), S(use_pixai=False))
        self.assertAlmostEqual(wd_only["tags"]["rating: general"][0], 0.7)
        self.assertEqual(wd_only["tags"]["rating: general"][1], "wd")
        self.assertEqual(wd_only["display"]["rating"], {"general": 0.7, "explicit": 0.3})
        pixai_only = at.detect(raw_of(*captures), S(use_wd=False))
        self.assertAlmostEqual(pixai_only["tags"]["rating: explicit"][0], 0.8)
        self.assertEqual(pixai_only["tags"]["rating: explicit"][1], "pixai")
        # a model that gave no rating does not dilute the other one's
        self.assertEqual(at.detect(raw_of(PHOTO), S())["tags"]["rating: general"], (0.9, "wd"))
        self.assertEqual(at.detect(raw_of("wd:a=0.9|pixai:b=0.9|prating:sensitive=0.7,general=0.3"), S())["tags"]["rating: sensitive"],
                         (0.7, "pixai"))
        # an exact tie goes to WD, and the rating tag can still be switched off
        tie = at.detect(raw_of("wd:a=0.9|rating:general=0.6,sensitive=0.4|prating:general=0.6,sensitive=0.4"), S())
        self.assertEqual(tie["tags"]["rating: general"], (0.6, "wd"))
        self.assertNotIn("rating: general", at.detect(raw_of(*captures), S(rating_tag=False))["tags"])

    def test_the_rating_of_each_model_is_averaged_over_its_own_readable_captures(self):
        raw = at.raw_from_results([read_picture(b"wd:a=0.9|rating:general=0.2,explicit=0.8|prating:general=0.6,explicit=0.4")[0],
                                   None], [None, "cannot identify image file"])
        found = at.detect(raw, S())
        self.assertAlmostEqual(found["display"]["rating"]["general"], 0.4)                  # (.2 + .6) / 2; the bad capture is no vote
        self.assertAlmostEqual(found["tags"]["rating: explicit"][0], 0.6)                   # (.8 + .4) / 2
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.2, "explicit": 0.8}, "pixai": {"general": 0.6, "explicit": 0.4}}])

    def test_models_and_character_tags_can_be_switched_off(self):
        raw = raw_of(PHOTO)
        self.assertIn("miku", self.kept(raw))
        self.assertNotIn("miku", self.kept(raw, character_tags=False))
        no_wd = self.kept(raw, use_wd=False)
        self.assertEqual(set(no_wd), {"beach", "sea"})
        no_pixai = self.kept(raw, use_pixai=False)
        self.assertEqual(no_pixai["beach"], 0.6)
        self.assertNotIn("sea", no_pixai)
        # PHOTO's PixAI gave no rating, so without WD there is none
        self.assertEqual(at.detect(raw, S(use_wd=False))["tags"].keys() & {"rating: general"}, set())

    def test_character_tags_gate_wd_characters_pixai_characters_and_pixai_copyright_tags(self):
        raw = raw_of("wd:girl=0.9|char:miku=0.8,luka=0.3|pixai:smile=0.9|pchar:hatsune_miku=0.9,kagamine_rin=0.4|"
                     "copy:vocaloid=0.8,project_sekai=0.3|rating:general=0.9")
        got = self.kept(raw)
        self.assertEqual(got, {"girl": 0.9, "miku": 0.8, "smile": 0.9, "hatsune miku": 0.9, "vocaloid": 0.8})
        self.assertEqual(self.kept(raw, character_tags=False), {"girl": 0.9, "smile": 0.9})
        # each model's own strictness applies to its characters and series
        self.assertEqual(self.kept(raw, pixai_strictness=0.85), {"girl": 0.9, "miku": 0.8, "smile": 0.9, "hatsune miku": 0.9})
        self.assertEqual(self.kept(raw, wd_strictness=0.85, pixai_strictness=0.35)["kagamine rin"], 0.4)
        self.assertNotIn("miku", self.kept(raw, wd_strictness=0.85))
        # switching PixAI off takes its characters and its series away, switching WD off only WD's characters
        self.assertEqual(self.kept(raw, use_pixai=False), {"girl": 0.9, "miku": 0.8})
        self.assertEqual(self.kept(raw, use_wd=False), {"smile": 0.9, "hatsune miku": 0.9, "vocaloid": 0.8})
        # the source says which model it came from, and the series tag shows on the Test card with the others
        found = at.detect(raw, S())
        self.assertEqual(found["tags"]["vocaloid"], (0.8, "pixai"))
        self.assertEqual(found["tags"]["miku"], (0.8, "wd"))
        shown = {t["tag"]: t for t in found["display"]["pixai"]}
        self.assertEqual((shown["vocaloid"]["kept"], shown["kagamine rin"]["kept"]), (True, False))
        self.assertNotIn("vocaloid", {t["tag"] for t in at.detect(raw, S(character_tags=False))["display"]["pixai"]})

    def test_a_tag_both_models_know_keeps_the_higher_score_and_its_source(self):
        raw = raw_of("wd:smile=0.7|char:hatsune_miku=0.8|pixai:smile=0.9|pchar:hatsune_miku=0.6")
        tags = at.detect(raw, S(rating_tag=False))["tags"]
        self.assertEqual(tags, {"smile": (0.9, "pixai"), "hatsune miku": (0.8, "wd")})
        # a series tag that is also a general tag keeps the higher of the two within PixAI
        both = at.detect(raw_of("pixai:vocaloid=0.6|copy:vocaloid=0.8"), S(rating_tag=False))["tags"]
        self.assertEqual(both, {"vocaloid": (0.8, "pixai")})
        self.assertEqual(at.detect(raw_of("pixai:vocaloid=0.6|copy:vocaloid=0.8"), S(rating_tag=False, character_tags=False))["tags"],
                         {"vocaloid": (0.6, "pixai")})

    def test_the_test_card_lists_scores_from_0_2_and_marks_the_kept_ones(self):
        shown = at.detect(raw_of(PHOTO), S())["display"]
        wd = {t["tag"]: t for t in shown["wd"]}
        self.assertEqual((wd["girl"]["kept"], wd["hat"]["kept"]), (True, False))
        self.assertEqual(wd["hat"]["score"], 0.3)
        self.assertEqual([t["score"] for t in shown["wd"]], sorted((t["score"] for t in shown["wd"]), reverse=True))
        self.assertIn("wave", {t["tag"] for t in shown["pixai"]})
        self.assertEqual(set(shown), {"wd", "pixai", "rating"})
        self.assertEqual(shown["rating"]["general"], 0.9)

    def test_a_capture_the_tagger_could_not_read_is_not_a_vote(self):
        raw = at.raw_from_results([read_picture(b"pixai:dog=0.9")[0], None], [None, "cannot identify image file"])
        self.assertEqual(raw["captures"], 1)
        self.assertEqual(self.kept(raw), {"dog": 0.9})
        self.assertEqual(at.raw_from_results([None], ["cannot identify image file"]), "cannot identify image file")
        self.assertIn("could not read", at.raw_from_results([None], [None]))


class TestNormalisationAndVocabulary(unittest.TestCase):
    def test_names_are_normalised_renamed_merged_and_blocked(self):
        raw = raw_of("wd:long_hair=0.9,1girl=0.8,Smile=0.7|char:hatsune_miku_(vocaloid)=0.8|pixai:long_hair=0.95,1girl=0.6,"
                     "cat=0.9|rating:general=0.9")
        s = S(vocabulary="1girl -> girl\nlong hair -> hair long", blocked=["cat", "smile"])
        tags = at.detect(raw, s)["tags"]
        self.assertEqual(set(tags), {"hatsune miku (vocaloid)", "rating: general", "girl", "hair long"})
        # "girl" came twice (renamed 1girl 0.8 from WD, 1girl 0.6 from PixAI): the highest score wins, with its source
        self.assertEqual(tags["girl"], (0.8, "wd"))
        self.assertEqual(tags["hair long"], (0.95, "pixai"))                  # both write long_hair: one tag, the higher score

    def test_a_blocked_tag_is_never_output_before_or_after_a_rename(self):
        raw = raw_of("wd:1girl=0.9,solo=0.9|pixai:cat=0.9")
        self.assertEqual(set(at.detect(raw, S(rating_tag=False, blocked=["1girl"], vocabulary="1girl -> girl"))["tags"]),
                         {"solo", "cat"})
        self.assertEqual(set(at.detect(raw, S(rating_tag=False, blocked=["girl"], vocabulary="1girl -> girl"))["tags"]),
                         {"solo", "cat"})

    def test_the_rating_tag_can_be_renamed_or_blocked(self):
        raw = raw_of(PHOTO)
        self.assertIn("safe", at.detect(raw, S(vocabulary="rating: general -> safe"))["tags"])
        self.assertNotIn("rating: general", at.detect(raw, S(blocked=["rating: general"]))["tags"])


# ---------------------------------------------------------------- rules

class TestRules(unittest.TestCase):
    def run_rules(self, rules, *tags):
        out, fired = at.apply_rules({t: (0.6, "wd") for t in tags}, rules)
        return sorted(out), fired, out

    def test_all_any_unless(self):
        r = [rule(if_all=["girl", "beach"], unless=["night"], add=["summer"])]
        self.assertEqual(self.run_rules(r, "girl", "beach")[0], ["beach", "girl", "summer"])
        self.assertEqual(self.run_rules(r, "girl")[0], ["girl"])                    # one of the two is missing
        self.assertEqual(self.run_rules(r, "girl", "beach", "night")[0], ["beach", "girl", "night"])   # unless
        anyr = [rule(if_any=["cat", "dog"], add=["pet"])]
        self.assertEqual(self.run_rules(anyr, "dog")[0], ["dog", "pet"])
        self.assertEqual(self.run_rules(anyr, "cat", "dog")[0], ["cat", "dog", "pet"])
        self.assertEqual(self.run_rules(anyr, "fish")[0], ["fish"])
        both = [rule(if_all=["girl"], if_any=["cat", "dog"], add=["pet owner"])]    # all of if_all AND one of if_any
        self.assertEqual(self.run_rules(both, "girl", "dog")[0], ["dog", "girl", "pet owner"])
        self.assertEqual(self.run_rules(both, "girl")[0], ["girl"])
        self.assertEqual(self.run_rules(both, "dog")[0], ["dog"])

    def test_added_tags_score_1_and_say_where_they_came_from(self):
        _, fired, out = self.run_rules([rule(if_all=["a"], add=["b"], remove=["a"])], "a")
        self.assertEqual(out, {"b": (1.0, "rule")})
        self.assertEqual(fired, [{"rule": 0, "added": ["b"], "removed": ["a"]}])

    def test_rules_run_in_order_and_repeat_until_nothing_changes(self):
        chain = [rule(if_all=["y"], add=["z"]), rule(if_all=["x"], add=["y"])]       # rule 1 needs what rule 2 adds
        names, fired, _ = self.run_rules(chain, "x")
        self.assertEqual(names, ["x", "y", "z"])                                    # needed a second pass
        self.assertEqual([f["rule"] for f in fired], [0, 1])
        long_chain = [rule(if_all=[f"t{i + 1}"], add=[f"t{i}"]) for i in range(8)]  # t8 -> t7 -> ... backwards
        self.assertEqual(self.run_rules(long_chain, "t8")[0], sorted(["t8", "t7", "t6", "t5", "t4", "t3"]))   # a tag a pass: 5 passes, no more

    def test_a_rule_that_changes_nothing_is_not_reported(self):
        _, fired, _ = self.run_rules([rule(if_all=["a"], add=["a"]), rule(if_all=["a"], remove=["zzz"])], "a")
        self.assertEqual(fired, [])

    def test_rules_that_undo_each_other_still_stop(self):
        flip = [rule(if_all=["a"], add=["b"], remove=["a"]), rule(if_all=["b"], add=["a"], remove=["b"])]
        names, fired, _ = self.run_rules(flip, "a")
        self.assertIn(names, (["a"], ["b"]))                                         # ends after 5 passes
        self.assertTrue(fired)

    def test_a_removed_tag_stays_removed_for_later_rules_in_the_pass(self):
        r = [rule(if_all=["girl"], remove=["girl"], add=["woman"]), rule(if_all=["girl"], add=["never"])]
        self.assertEqual(self.run_rules(r, "girl")[0], ["woman"])


# ---------------------------------------------------------------- the VLM's words, the cap, the final list

class TestFinalTags(unittest.TestCase):
    def build(self, vlm=None, pictures=(PHOTO,), **settings):
        return at.build(raw_of(*pictures), vlm, S(**settings))

    def test_vlm_adds_and_removes(self):
        vlm = {"description": "A girl.", "add_tags": ["Smile", "Sea Shore"], "remove_tags": ["solo", "nothing"]}
        got = self.build(vlm)
        by = {t["tag"]: t for t in got["tags"]}
        self.assertEqual(by["smile"], {"tag": "smile", "score": 0.7, "source": "vlm"})
        self.assertEqual(by["sea shore"]["score"], 0.7)
        self.assertNotIn("solo", by)
        self.assertEqual(by["girl"]["source"], "wd")
        self.assertEqual(got["description"], "A girl.")

    def test_a_tag_the_vlm_adds_that_was_found_already_keeps_its_source_and_score(self):
        got = self.build({"description": "", "add_tags": ["sea", "girl"], "remove_tags": []})
        by = {t["tag"]: t for t in got["tags"]}
        self.assertEqual(by["sea"], {"tag": "sea", "score": 0.7, "source": "pixai"})
        self.assertEqual(by["girl"], {"tag": "girl", "score": 0.9, "source": "wd"})       # never lowered

    def test_a_tag_the_vlm_both_adds_and_removes_is_ignored(self):
        got = self.build({"description": "", "add_tags": ["smile", "solo"], "remove_tags": ["smile", "solo"]})
        names = tags_of(got)
        self.assertNotIn("smile", names)                    # not added
        self.assertIn("solo", names)                        # not removed either

    def test_the_vlm_cannot_remove_a_sure_tag_or_the_rating(self):
        vlm = {"description": "", "add_tags": [], "remove_tags": ["girl", "beach", "rating: general"]}
        names = tags_of(self.build(vlm))
        self.assertIn("girl", names)                        # 0.9: a tagger is sure
        self.assertNotIn("beach", names)                    # 0.8: the VLM may overrule it
        self.assertIn("rating: general", names)

    def test_vlm_tags_are_renamed_and_blocked_too(self):
        vlm = {"description": "", "add_tags": ["1girl", "cat", "Hair_Bow"], "remove_tags": ["sea"]}
        got = self.build(vlm, vocabulary="1girl -> woman\nhair bow -> bow", blocked=["cat"])
        names = tags_of(got)
        self.assertIn("woman", names)
        self.assertIn("bow", names)
        self.assertNotIn("cat", names)
        self.assertNotIn("sea", names)
        # the VLM may name a tag the way the panel showed it (already renamed)
        got = self.build({"description": "", "add_tags": [], "remove_tags": ["woman"]}, vocabulary="girl -> woman",
                         pictures=("wd:girl=0.6",))
        self.assertEqual(tags_of(got), [r for r in tags_of(got) if r.startswith("rating")])

    def test_without_describe_the_vlm_is_ignored(self):
        got = self.build({"description": "A girl.", "add_tags": ["smile"], "remove_tags": ["girl"]}, describe=False)
        self.assertIn("girl", tags_of(got))
        self.assertNotIn("smile", tags_of(got))
        self.assertEqual(got["description"], "")
        self.assertNotIn("Description:", got["block"])

    def test_rules_see_what_the_vlm_did_and_blocked_wins_over_everything(self):
        rules = [rule(if_all=["smile"], add=["happy", "cat"]), rule(if_all=["happy"], remove=["solo"])]
        got = self.build({"description": "", "add_tags": ["smile"], "remove_tags": []}, rules=rules, blocked=["cat"])
        names = tags_of(got)
        self.assertIn("happy", names)
        self.assertNotIn("cat", names)                           # added by a rule, blocked afterwards
        self.assertNotIn("solo", names)
        self.assertEqual(got["rules"], [{"rule": 0, "added": ["happy", "cat"], "removed": []},     # what the rule did, before "blocked"
                                        {"rule": 1, "added": [], "removed": ["solo"]}])

    def test_the_cap_keeps_the_best_and_always_the_rating(self):
        pic = "wd:" + ",".join(f"t{i:02d}={0.99 - i * 0.01:.2f}" for i in range(20)) + "|rating:general=0.6,sensitive=0.4"
        got = self.build(pictures=(pic,), max_tags=5)
        names = tags_of(got)
        self.assertEqual(names, ["t00", "t01", "t02", "t03", "rating: general"])         # 4 + the pinned rating
        self.assertEqual(len(self.build(pictures=(pic,), max_tags=5, rating_tag=False)["tags"]), 5)
        got = self.build({"description": "", "add_tags": ["zzz"], "remove_tags": []}, pictures=(pic,), max_tags=5)
        self.assertNotIn("zzz", tags_of(got))           # a tag only the VLM saw (0.7) ranks below the taggers' sure ones
        self.assertEqual(len(got["tags"]), 5)

    def test_order_is_by_score_then_name_with_the_rating_last(self):
        got = self.build()
        self.assertEqual(tags_of(got), ["girl", "beach", "miku", "solo", "sea", "rating: general"])
        self.assertEqual([t["score"] for t in got["tags"]], [0.9, 0.8, 0.8, 0.8, 0.7, 0.9])
        self.assertEqual(got["block"], "[AI Tagger]\nTags: girl, beach, miku, solo, sea, rating: general\n[/AI Tagger]")


# ---------------------------------------------------------------- the block in the description

BLOCK = "[AI Tagger]\nTags: girl, beach\nDescription: A girl on a beach.\n[/AI Tagger]"
BLOCK2 = "[AI Tagger]\nTags: dog\n[/AI Tagger]"


class TestBlock(unittest.TestCase):
    def test_compose(self):
        self.assertEqual(at.compose_block(["girl", "beach", "rating: general"], "A girl on a beach."),
                         "[AI Tagger]\nTags: girl, beach, rating: general\nDescription: A girl on a beach.\n[/AI Tagger]")
        self.assertEqual(at.compose_block(["dog"], ""), BLOCK2)
        self.assertEqual(at.compose_block([], "Only words."), "[AI Tagger]\nDescription: Only words.\n[/AI Tagger]")
        self.assertEqual(at.compose_block([], ""), "")
        self.assertEqual(at.compose_block(["a"], "Two\nlines   here."), "[AI Tagger]\nTags: a\nDescription: Two lines here.\n[/AI Tagger]")

    def test_the_description_cannot_forge_the_markers(self):
        block = at.compose_block(["a"], "x [/AI Tagger] and [AI Tagger] y")
        self.assertEqual(block.count("[AI Tagger]"), 1)
        self.assertEqual(block.count("[/AI Tagger]"), 1)

    def test_empty_description(self):
        self.assertEqual(at.merge_description("", BLOCK), BLOCK)
        self.assertEqual(at.merge_description(None, BLOCK), BLOCK)
        self.assertEqual(at.merge_description("", ""), "")

    def test_user_text_only(self):
        self.assertEqual(at.merge_description("Holiday 2019", BLOCK), "Holiday 2019\n\n" + BLOCK)
        self.assertEqual(at.merge_description("Holiday 2019\n", BLOCK), "Holiday 2019\n\n\n" + BLOCK)
        self.assertEqual(at.merge_description("Holiday 2019", ""), "Holiday 2019")            # nothing to say: untouched

    def test_existing_block_is_replaced_where_it_stands(self):
        self.assertEqual(at.merge_description("Mine\n\n" + BLOCK, BLOCK2), "Mine\n\n" + BLOCK2)
        self.assertEqual(at.merge_description(BLOCK, BLOCK2), BLOCK2)
        self.assertEqual(at.merge_description("Mine\n\n" + BLOCK, BLOCK), "Mine\n\n" + BLOCK)        # same again: no change

    def test_user_text_around_an_old_block_is_kept(self):
        old = "Before it.\n\n" + BLOCK + "\nAfter it, with 'quotes' and é."
        self.assertEqual(at.merge_description(old, BLOCK2), "Before it.\n\n" + BLOCK2 + "\nAfter it, with 'quotes' and é.")
        self.assertEqual(at.merge_description(BLOCK + "\n\nAfter", BLOCK2), BLOCK2 + "\n\nAfter")
        inline = "a[AI Tagger]\nTags: x\n[/AI Tagger]b"
        self.assertEqual(at.merge_description(inline, BLOCK2), "a" + BLOCK2 + "b")

    def test_removing_the_block_gives_the_owners_text_back_exactly(self):
        for user in ["", "Holiday 2019", "Holiday 2019\n", "Two\n\nparagraphs\n\n", "  spaces  ", "line1\nline2", "éè \U0001F600"]:
            with self.subTest(user=user):
                written = at.merge_description(user, BLOCK)
                self.assertEqual(at.strip_block(written), user)
                self.assertEqual(at.merge_description(written, ""), user)
                self.assertEqual(at.merge_description(written, BLOCK), written)                   # idempotent
                self.assertEqual(at.merge_description(at.merge_description(written, BLOCK2), BLOCK), written)
                self.assertTrue(written.startswith(user))                                         # never changed

    def test_removing_a_block_with_text_around_it(self):
        self.assertEqual(at.strip_block("Before.\n\n" + BLOCK + "\nAfter."), "Before.\n\nAfter.")
        self.assertEqual(at.strip_block(BLOCK + "\nAfter."), "After.")
        self.assertEqual(at.strip_block("Before.\n" + BLOCK + "\nAfter."), "Before.\nAfter.")
        self.assertEqual(at.strip_block(BLOCK + "\n"), "")                    # only a newline was after it

    def test_a_second_copy_of_the_block_is_leftover_and_goes(self):
        twice = "Mine\n\n" + BLOCK + "\n\n" + BLOCK
        self.assertEqual(at.merge_description(twice, BLOCK2), "Mine\n\n" + BLOCK2)
        self.assertEqual(at.strip_block(twice), "Mine")

    def test_stray_markers_are_the_owners_text(self):
        stray = "see [AI Tagger] in the docs"
        self.assertEqual(at.block_spans(stray), [])
        written = at.merge_description(stray, BLOCK)
        self.assertEqual(written, stray + "\n\n" + BLOCK)
        self.assertEqual(at.merge_description(written, BLOCK2), stray + "\n\n" + BLOCK2)         # only our block is replaced
        self.assertEqual(at.strip_block(written), stray)
        closing = "a [/AI Tagger] b"
        self.assertEqual(at.merge_description(closing, BLOCK), closing + "\n\n" + BLOCK)
        self.assertEqual(at.strip_block(at.merge_description(closing, BLOCK)), closing)

    def test_same_text_ignores_what_immich_may_trim(self):
        self.assertTrue(at.same_text("a\nb", "a\nb\n"))
        self.assertTrue(at.same_text("a\r\nb", "a\nb"))
        self.assertTrue(at.same_text(None, ""))
        self.assertFalse(at.same_text("a", "b"))


# ---------------------------------------------------------------- captures

class TestCaptures(Base):
    def test_which_parts_of_a_video_are_captured(self):
        self.assertEqual(at.segment_positions(6), [(k - 0.5) / 8 for k in range(2, 8)])      # the middles of segments 2-7
        self.assertEqual(at.segment_positions(2), [2.5 / 8, 5.5 / 8])                       # segments 3 and 6
        self.assertEqual(len(at.segment_positions(1)), 1)
        self.assertEqual(at.segment_positions(3), [2.5 / 8, 4.5 / 8, 6.5 / 8])
        for n in range(1, 9):
            got = at.segment_positions(n)
            self.assertEqual(len(got), min(n, 6))
            self.assertEqual(got, sorted(set(got)))
            self.assertTrue(all(1 / 8 < p < 7 / 8 for p in got))        # never the first or the last segment

    def test_a_video_is_cut_with_ffmpeg_at_those_spots(self):
        orig = Path(self.tmp.name) / "v.mp4"
        orig.write_bytes(b"x")
        item = {"id": "v", "type": "VIDEO", "preview": "", "original": str(orig), "duration_ms": 80000}
        with mock.patch.object(sp, "video_frames", return_value=[b"f1", b"f2"]) as cut:
            frames, kind = at.prepare_captures(item, 2)
        self.assertEqual((frames, kind), ([b"f1", b"f2"], "video"))
        args, kwargs = cut.call_args
        self.assertEqual(args[:2], (str(orig), 80000))
        self.assertEqual(args[3], 1024)
        self.assertEqual(kwargs["positions"], [2.5 / 8, 5.5 / 8])

    def test_ffmpeg_gives_nothing_so_the_preview_stands_in(self):
        from PIL import Image
        orig, prev = Path(self.tmp.name) / "v.mp4", Path(self.tmp.name) / "p.jpg"
        orig.write_bytes(b"not a video")
        Image.new("RGB", (800, 450), "blue").save(prev)
        item = {"id": "v", "type": "VIDEO", "preview": str(prev), "original": str(orig), "duration_ms": 1000}
        with mock.patch.object(sp, "video_frames", return_value=[]):
            frames, kind = at.prepare_captures(item, 6)
        self.assertEqual((len(frames), kind), (1, "video"))

    def test_a_photo_is_its_preview_shrunk_to_1024(self):
        from PIL import Image
        prev = Path(self.tmp.name) / "p.jpg"
        Image.new("RGB", (2000, 1500), "red").save(prev)
        frames, kind = at.prepare_captures({"id": "x", "type": "IMAGE", "preview": str(prev), "original": ""}, 6)
        self.assertEqual((kind, len(frames)), ("image", 1))
        with Image.open(io.BytesIO(frames[0])) as im:
            self.assertEqual(max(im.size), 1024)

    def test_an_animated_image_gives_frames_at_the_segment_spots(self):
        from PIL import Image
        path = Path(self.tmp.name) / "a.gif"
        colours = ["red", "green", "blue", "white", "black", "yellow", "pink", "gray"] * 2
        frames = [Image.new("RGB", (100, 100), c) for c in colours]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=100)
        got, kind = at.prepare_captures({"id": "g", "type": "IMAGE", "preview": "", "original": str(path)}, 2)
        self.assertEqual((kind, len(got)), ("animation", 2))
        got, _ = at.prepare_captures({"id": "g", "type": "IMAGE", "preview": "", "original": str(path)}, 6)
        self.assertEqual(len(got), 6)

    def test_no_preview_means_the_file_itself_or_a_clear_reason(self):
        from PIL import Image
        big = Path(self.tmp.name) / "big.png"
        Image.new("RGB", (1080, 1920), "blue").save(big)
        frames, kind = at.prepare_captures({"id": "x", "type": "IMAGE", "preview": "", "original": str(big)}, 6)
        self.assertEqual(kind, "image")
        with Image.open(io.BytesIO(frames[0])) as im:
            self.assertEqual(max(im.size), 1024)
        code = Path(self.tmp.name) / "x.ts"
        code.write_text("import {a} from './b';\n")
        with self.assertRaises(ValueError) as err:
            at.prepare_captures({"id": "y", "type": "VIDEO", "preview": "", "original": str(code), "duration_ms": 0}, 6)
        self.assertIn("TypeScript", str(err.exception))
        with self.assertRaises(FileNotFoundError):
            at.prepare_captures({"id": "z", "type": "IMAGE", "preview": "", "original": "/nope.jpg"}, 6)


# ---------------------------------------------------------------- the two model servers over HTTP

class StubServer:
    """A tiny HTTP server: routes maps (method, path) to a function (json body) -> (status, json answer)."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.requests.append((self.command, self.path, body))
                fn = outer.routes.get((self.command, self.path))
                status, payload = fn(body) if fn else (404, {"error": "no such route"})
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = handle_any

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestTaggerClient(unittest.TestCase):
    def serve(self, routes):
        server = StubServer(routes)
        self.addCleanup(server.close)
        return server

    def test_tag_sends_pictures_and_the_floor(self):
        def tag(body):
            return 200, {"results": [{"wd": {"general": {"a": 0.9}, "character": {}, "rating": {"general": 1.0}},
                                      "pixai": {"general": {"b": 0.8}, "character": {"c": 0.7}, "copyright": {"d": 0.6},
                                                "rating": {"general": 0.9, "explicit": 0.1}}}, None],
                         "errors": [None, "cannot identify image file"], "tookMs": 5}

        server = self.serve({("POST", "/tag"): tag, ("GET", "/health"): lambda b: (200, {"status": "ok"})})
        client = at.Tagger(server.url)
        results, errors = client.tag([b"one", b"two"])
        self.assertEqual(errors, [None, "cannot identify image file"])
        self.assertEqual(results[0]["pixai"]["general"], {"b": 0.8})
        self.assertEqual(results[0]["pixai"]["copyright"], {"d": 0.6})
        self.assertIsNone(results[1])
        (_, path, body), = [r for r in server.requests if r[1] == "/tag"]
        self.assertEqual([base64.b64decode(i) for i in body["images"]], [b"one", b"two"])
        self.assertEqual(body["floor"], 0.05)
        self.assertEqual(client.health()["status"], "ok")

    def test_at_most_64_pictures_per_request(self):
        with self.assertRaises(ValueError):
            at.Tagger("http://127.0.0.1:1").tag([b"x"] * 65)

    def test_loading_or_unreachable_is_service_down(self):
        server = self.serve({("POST", "/tag"): lambda b: (503, {"error": "models are loading", "status": "loading"})})
        with self.assertRaises(sp.ServiceDown):
            at.Tagger(server.url).tag([b"x"])
        with self.assertRaises(sp.ServiceDown):
            at.Tagger("http://127.0.0.1:1").tag([b"x"])
        self.assertIsNone(at.Tagger("http://127.0.0.1:1").health())

    def test_out_of_memory_is_told_apart(self):
        server = self.serve({("POST", "/tag"): lambda b: (500, {"error": "CUDA out of memory. Tried to allocate 2 GiB"})})
        with self.assertRaises(at.GpuOOM):
            at.Tagger(server.url).tag([b"x"])
        server = self.serve({("POST", "/tag"): lambda b: (200, {"results": [None], "errors": ["torch.OutOfMemoryError: CUDA out of memory"]})})
        with self.assertRaises(at.GpuOOM):
            at.Tagger(server.url).tag([b"x"])
        server = self.serve({("POST", "/tag"): lambda b: (500, {"error": "boom"})})
        with self.assertRaises(sp.ServiceDown):
            at.Tagger(server.url).tag([b"x"])
        server = self.serve({("POST", "/tag"): lambda b: (400, {"error": "bad request"})})
        with self.assertRaises(RuntimeError):
            at.Tagger(server.url).tag([b"x"])


class TestVlmClient(unittest.TestCase):
    def answer(self, content, **extra):
        return {"choices": [{"message": {"content": content, **extra}, "finish_reason": "stop"}]}

    def serve(self, replies):
        """A VLM server that answers with the next of ``replies`` (a dict = 200, a tuple = (status, body))."""
        replies = list(replies)

        def chat(body):
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            return reply if isinstance(reply, tuple) else (200, reply)

        server = StubServer({("POST", "/v1/chat/completions"): chat, ("GET", "/health"): lambda b: (200, {})})
        self.addCleanup(server.close)
        return server

    GOOD = json.dumps({"description": "A dog on grass.", "add_tags": ["puppy"], "remove_tags": ["cat"]})

    def test_the_request_has_the_shape_vllm_wants(self):
        server = self.serve([self.answer(self.GOOD)])
        vlm = at.VLM(server.url)
        got = vlm.describe([{"tag": "dog", "score": 0.9}], {"general": 0.95},
                           S(instructions="Name the breed.", language="German"), "IMAGE", ["puppy"])
        self.assertEqual(got, {"description": "A dog on grass.", "add_tags": ["puppy"], "remove_tags": ["cat"], "note": ""})
        (_, _, body), = [r for r in server.requests if r[1] == "/v1/chat/completions"]
        self.assertEqual(body["model"], "tagger-vlm")
        self.assertEqual((body["temperature"], body["max_tokens"]), (0.2, 400))
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        rf = body["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertEqual(rf["json_schema"]["schema"]["required"], ["description", "add_tags", "remove_tags"])
        for key in ("add_tags", "remove_tags"):
            self.assertEqual(rf["json_schema"]["schema"]["properties"][key]["maxItems"], 12)
        system, user = body["messages"]
        self.assertEqual(system["role"], "system")
        for word in ("private", "neutral", "nudity", "never refuse", "never see the pictures"):
            self.assertIn(word, system["content"])
        self.assertEqual(user["role"], "user")
        text = user["content"]
        self.assertIsInstance(text, str)                  # plain text: no parts, so no picture can be in it
        for needle in ("dog 0.90", "general 0.95", "Name the breed.", "puppy", "German", "JSON"):
            self.assertIn(needle, text)
        self.assertTrue(vlm.health())
        self.assertFalse(at.VLM("http://127.0.0.1:1").health())

    def test_the_request_never_holds_a_picture(self):
        server = self.serve([self.answer(self.GOOD)])
        at.VLM(server.url).describe([{"tag": "dog", "score": 0.9}], {"general": 0.95}, S(), "VIDEO", [])
        (_, _, body), = [r for r in server.requests if r[1] == "/v1/chat/completions"]
        wire = json.dumps(body)
        for forbidden in ("image_url", "data:image", "base64", '"type": "image'):
            self.assertNotIn(forbidden, wire)
        self.assertTrue(all(isinstance(m["content"], str) for m in body["messages"]))
        self.assertFalse(hasattr(at, "VLM_SIDE"))         # the picture-shrinking for the VLM is gone
        self.assertEqual(list(inspect.signature(at.VLM.describe).parameters)[1:3], ["tags", "rating"])

    def test_a_refusal_is_asked_once_more(self):
        refusal = json.dumps({"description": "I'm sorry, but I can't describe this image.", "add_tags": [], "remove_tags": []})
        server = self.serve([self.answer(refusal), self.answer(self.GOOD)])
        got = at.VLM(server.url).describe([], {}, S())
        self.assertEqual(got["description"], "A dog on grass.")
        self.assertEqual(got["note"], "")
        self.assertEqual(len([r for r in server.requests if r[0] == "POST"]), 2)

    def test_two_bad_answers_give_an_empty_description_with_a_note_not_an_error(self):
        for replies, expect in [([self.answer("this is not json")], "not valid JSON"),
                                ([self.answer("", refusal="I cannot help")], "refused"),
                                ([self.answer('{"description": 3}')], "no description"),
                                ([self.answer(self.GOOD, **{})["choices"] and {"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]}], "cut short"),
                                ([(400, {"error": "image too large"})], "400")]:
            server = self.serve(replies)
            got = at.VLM(server.url).describe([], {}, S())
            self.assertEqual((got["description"], got["add_tags"], got["remove_tags"]), ("", [], []))
            self.assertIn("no description", got["note"])
            self.assertIn(expect, got["note"])
            self.assertEqual(len([r for r in server.requests if r[0] == "POST"]), 2)         # tried twice

    def test_unreachable_or_broken_server_is_service_down_not_a_note(self):
        with self.assertRaises(sp.ServiceDown):
            at.VLM("http://127.0.0.1:1").describe([], {}, S())
        server = self.serve([(503, {"error": "loading"})])
        with self.assertRaises(sp.ServiceDown):
            at.VLM(server.url).describe([], {}, S())
        server = self.serve([(500, {"error": "engine dead"})])
        with self.assertRaises(sp.ServiceDown):
            at.VLM(server.url).describe([], {}, S())

    def test_parse_vlm_answer(self):
        self.assertEqual(at.parse_vlm_answer(self.GOOD)["add_tags"], ["puppy"])
        self.assertEqual(at.parse_vlm_answer("```json\n" + self.GOOD + "\n```")["description"], "A dog on grass.")
        self.assertEqual(at.parse_vlm_answer('{"description": "  Two\\nlines  "}'),
                         {"description": "Two lines", "add_tags": [], "remove_tags": []})
        for bad in (None, "", "  ", "[]", '{"description": 1}', '{"description": "x", "add_tags": "a"}',
                    '{"description": "x", "remove_tags": [1]}', '{"description": "I cannot assist with that."}',
                    '{"description": "As an AI model, I do not describe"}'):
            with self.assertRaises(at.VLMRejected, msg=bad):
                at.parse_vlm_answer(bad)
        self.assertEqual(at.parse_vlm_answer('{"description": ""}')["description"], "")        # empty is allowed

    def test_the_prompt_lists_tags_rating_instructions_and_terms(self):
        text = at.vlm_prompt([{"tag": "dog", "score": 0.91}, {"tag": "grass", "score": 0.7}], {"general": 0.9, "explicit": 0.02},
                             S(instructions="  Be brief.  ", language="Dutch"), ["puppy", "garden"], "VIDEO")
        for needle in ("in one video", "dog 0.91, grass 0.70", "explicit 0.02, general 0.90", "Be brief.",
                       "puppy; garden", "in Dutch", "1-2 sentences", "at most 8", "do not add a place", "instructions ask for it"):
            self.assertIn(needle, text)
        self.assertNotIn("image(s)", text)               # there is no picture to refer to
        bare = at.vlm_prompt([], {}, S(), [], "IMAGE")
        self.assertIn("(none)", bare)
        self.assertIn("in one picture", bare)
        self.assertNotIn("Instructions", bare)
        self.assertNotIn("Preferred", bare)


# ---------------------------------------------------------------- the store

class TestStore(Base):
    def test_work_is_the_queue_first_then_the_newest_untagged(self):
        self.store.sync_catalog(self.catalog)
        self.assertEqual([w["id"] for w in self.store.work(10)], self.ids[:5])
        self.assertEqual({w["mode"] for w in self.store.work(10)}, {"full"})
        self.store.enqueue([self.ids[3]], "retag")
        got = self.store.work(10)
        self.assertEqual([w["id"] for w in got][:2], [self.ids[3], self.ids[0]])
        self.assertEqual(got[0]["mode"], "retag")
        self.assertEqual(len(got), 5)                                  # not twice
        self.assertEqual([w["id"] for w in self.store.work(2)], [self.ids[3], self.ids[0]])
        self.assertEqual([w["id"] for w in self.store.work(10, skip={self.ids[0], self.ids[3]})], self.ids[1:3] + self.ids[4:5])
        self.assertEqual(self.store.work(10)[0]["name"], "IMG_0003.jpg")

    def test_queue_order_is_first_asked_first_done(self):
        self.store.sync_catalog(self.catalog)
        self.store.enqueue([self.ids[4]], "describe")
        self.store.enqueue([self.ids[2]], "full")
        self.assertEqual([w["id"] for w in self.store.work(2)], [self.ids[4], self.ids[2]])

    def test_a_stronger_mode_replaces_a_weaker_one_for_the_same_asset(self):
        a = self.ids[0]
        self.store.enqueue([a], "retag")
        self.store.enqueue([a], "describe")
        self.assertEqual(self.store.queue()[0]["mode"], "describe")
        self.store.enqueue([a], "retag")                               # weaker: ignored
        self.assertEqual(self.store.queue()[0]["mode"], "describe")
        self.store.enqueue([a], "full")
        self.assertEqual([q["mode"] for q in self.store.queue()], ["full"])
        self.store.enqueue([a], "describe")
        self.assertEqual([q["mode"] for q in self.store.queue()], ["full"])
        self.assertEqual(self.store.enqueue([self.ids[1], self.ids[1], self.ids[2]], "retag"), 2)
        with self.assertRaises(ValueError):
            self.store.enqueue([a], "everything")

    def test_dequeue_only_when_the_work_done_covers_what_was_asked(self):
        a = self.ids[0]
        self.store.enqueue([a], "full")
        self.store.dequeue(a, "describe")
        self.assertEqual(len(self.store.queue()), 1)
        self.store.dequeue(a, "full")
        self.assertEqual(self.store.queue(), [])
        self.store.enqueue([a], "retag")
        self.store.dequeue(a, "describe")
        self.assertEqual(self.store.queue(), [])

    def test_asking_again_forgets_earlier_failures_and_an_exclude(self):
        self.store.sync_catalog(self.catalog)
        a = self.ids[0]
        self.store.fail(a, "x", final=True)
        self.store.exclude([a])
        self.assertNotIn(a, [w["id"] for w in self.store.work(10)])
        self.store.enqueue([a], "full")
        self.assertEqual(self.store.work(10)[0]["id"], a)
        self.assertEqual(self.store.counts()["excluded"], 0)

    def test_excluded_and_gone_assets_are_skipped(self):
        self.store.sync_catalog(self.catalog)
        self.store.exclude([self.ids[0]])
        self.assertNotIn(self.ids[0], [w["id"] for w in self.store.work(10)])
        self.store.enqueue([self.ids[1]], "full")
        self.store.exclude([self.ids[1]])                              # excluding also takes it off the queue
        self.assertEqual(self.store.queue(), [])
        self.store.sync_catalog(self.catalog[2:])
        self.assertEqual([w["id"] for w in self.store.work(10)], self.ids[2:5])
        self.assertIsNone(self.store.asset(self.ids[0]))

    def test_failures_are_retried_later_three_times_like_search_plus(self):
        self.store.sync_catalog(self.catalog)
        a = self.ids[0]
        self.store.fail(a, "no preview")
        self.assertNotIn(a, [w["id"] for w in self.store.work(10)])           # not again right away
        c = self.store.counts()
        self.assertEqual((c["failed"], c["retrying"], c["pending"]), (1, 1, 4))
        with mock.patch.object(at, "RETRY_AFTER", -5):
            self.assertEqual(self.store.work(10)[0]["id"], a)
            self.store.fail(a, "no preview")
            self.store.fail(a, "no preview")
            self.assertNotIn(a, [w["id"] for w in self.store.work(10)])       # three strikes
        self.assertEqual(self.store.counts()["retrying"], 0)
        self.assertEqual(self.store.failures()[0]["error"], "no preview")
        self.assertEqual(self.store.failures()[0]["name"], "IMG_0000.jpg")
        self.store.clear_failed()
        c = self.store.counts()
        self.assertEqual((c["failed"], c["cleared"], c["pending"]), (0, 1, 4))
        self.assertEqual(self.store.failures(), [])
        self.store.retry_failed()
        self.assertEqual(self.store.counts()["pending"], 5)

    def test_a_final_failure_leaves_the_queue(self):
        self.store.sync_catalog(self.catalog)
        self.store.enqueue([self.ids[0]], "full")
        self.store.fail(self.ids[0], "not a picture", final=True)
        self.assertEqual(self.store.queue(), [])

    def test_raw_scores_round_trip(self):
        raw = raw_of(PHOTO, DOG)
        self.store.save_raw("a", raw)
        self.assertEqual(self.store.raw("a"), raw)
        self.assertEqual(raw["models"], ["pixai", "wd"])
        self.assertIsNone(self.store.raw("b"))

    def test_raw_keeps_the_pixai_categories_and_both_ratings(self):
        raw = raw_of("wd:girl=0.9|char:miku=0.8|rating:general=0.9|pixai:smile=0.7|pchar:rin=0.6|copy:vocaloid=0.8|"
                     "prating:general=0.8,explicit=0.2")
        self.assertEqual(raw["scores"], [{"wd": {"general": {"girl": 0.9}, "character": {"miku": 0.8}},
                                          "pixai": {"general": {"smile": 0.7}, "character": {"rin": 0.6},
                                                    "copyright": {"vocaloid": 0.8}}}])
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.9}, "pixai": {"general": 0.8, "explicit": 0.2}}])
        self.store.save_raw("a", raw)
        self.assertEqual(self.store.raw("a"), raw)
        self.assertTrue(at.has_pixai(raw))
        self.assertFalse(at.has_pixai(self.store.raw("nothing")))
        only_wd = at.raw_from_results([{"wd": {"general": {"a": 0.9}, "character": {}, "rating": {"general": 1.0}}}], [None])
        self.assertEqual((only_wd["models"], at.has_pixai(only_wd)), (["wd"], False))

    def v1_store(self, ids):
        """A store as the RAM++ panel left it: written results with stored scores that have no PixAI entry."""
        folder = Path(self.tmp.name) / "v1"
        old = at.Store(folder)
        old.sync_catalog(self.catalog)
        v1_scores = [{"wd": {"general": {"girl": 0.9}, "character": {}}, "ram": {"beach": 0.8}}]
        for i in ids:
            old.conn.execute("insert into raw values (?,?,?,?,?,?)", (self.ids[i], 1, json.dumps(v1_scores),
                                                                       json.dumps([{"general": 0.9}]), "2026-09-30T10:00:00+00:00", "ram,wd"))
            old.save_result(self.ids[i], tags=[{"tag": "girl", "score": 0.9, "source": "wd"}], vlm=None, description="x",
                            block="B", version=1)
            old.mark_written(self.ids[i])
        old.conn.execute("delete from meta where key='raw_format'")           # what a store from before v2 looks like
        old.conn.commit()
        old.conn.close()
        return folder

    def test_results_made_from_ram_scores_count_as_outdated_once(self):
        folder = self.v1_store([0, 1])
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual(store.settings_version, 2)                         # bumped: version 1 results are older
        self.assertEqual(store.counts()["outdated"], 2)
        self.assertEqual(set(store.scope_ids("outdated")), {self.ids[0], self.ids[1]})
        self.assertFalse(at.has_pixai(store.raw(self.ids[0])))
        store.conn.close()
        again = at.Store(folder)                                            # opening it again does not bump it again
        self.addCleanup(again.conn.close)
        self.assertEqual(again.settings_version, 2)
        self.assertEqual(again.meta("raw_format"), at.RAW_FORMAT)

    def test_a_new_store_or_one_with_pixai_scores_is_not_bumped(self):
        self.assertEqual(self.store.settings_version, 1)
        self.assertEqual(self.store.meta("raw_format"), at.RAW_FORMAT)
        folder = Path(self.tmp.name) / "v2"
        store = at.Store(folder)
        store.save_raw("a", raw_of(PHOTO))
        store.conn.execute("delete from meta where key='raw_format'")
        store.conn.commit()
        store.conn.close()
        again = at.Store(folder)
        self.addCleanup(again.conn.close)
        self.assertEqual(again.settings_version, 1)

    def test_results_history_and_forget(self):
        self.store.sync_catalog(self.catalog)
        a = self.ids[0]
        tags = [{"tag": "girl", "score": 0.9, "source": "wd"}, {"tag": "beach", "score": 0.8, "source": "pixai"}]
        self.store.save_result(a, tags=tags, vlm={"description": "d"}, description="d", block="B", version=3, note="n")
        got = self.store.result(a)
        self.assertEqual((got["tags"], got["vlm"], got["description"], got["block"], got["settings_version"], got["note"]),
                         (tags, {"description": "d"}, "d", "B", 3, "n"))
        self.assertIsNone(got["written_at"])
        self.assertEqual(self.store.counts()["processed"], 0)                  # not written yet: still waiting
        self.assertEqual(self.store.work(1)[0]["mode"], "retag")               # ... and what is left is just the write
        self.store.mark_written(a)
        self.assertEqual(self.store.counts()["processed"], 1)
        for i in range(25):
            self.store.add_history(a, f"old{i}", f"new{i}")
        self.assertEqual(len(self.store.history(a)), at.HISTORY_KEEP)
        self.assertEqual(self.store.history(a)[-1]["new"], "new24")
        self.store.save_raw(a, raw_of(PHOTO))
        self.store.forget(a)
        self.assertEqual((self.store.result(a), self.store.raw(a)), (None, None))
        self.assertEqual(len(self.store.history(a)), at.HISTORY_KEEP)           # the history is kept
        self.assertEqual(self.store.list_assets()["total"], 0)

    def test_scopes(self):
        self.store.sync_catalog(self.catalog)
        for i, (version, tags) in enumerate([(1, ["girl", "beach"]), (2, ["dog"]), (2, ["girl"])]):
            self.store.save_result(self.ids[i], tags=[{"tag": t, "score": 1.0, "source": "wd"} for t in tags], vlm=None,
                                   description="", block="", version=version)
            self.store.mark_written(self.ids[i])
        self.set(max_tags=10)                                                  # version 2
        self.set(max_tags=11)                                                  # version 3
        self.assertEqual(set(self.store.scope_ids("all")), set(self.ids[:3]))
        self.assertEqual(set(self.store.scope_ids("outdated")), set(self.ids[:3]))
        self.assertEqual(set(self.store.scope_ids("tag", tag="Girl")), {self.ids[0], self.ids[2]})
        self.assertEqual(self.store.scope_ids("ids", ids=["x", "y", "x"]), ["x", "y"])
        self.assertEqual(self.store.scope_ids("tag", tag="nothing"), [])
        self.store.save_result(self.ids[0], tags=[], vlm=None, description="", block="", version=3)
        self.store.mark_written(self.ids[0])
        self.assertEqual(set(self.store.scope_ids("outdated")), set(self.ids[1:3]))
        with self.assertRaises(ValueError):
            self.store.scope_ids("some")

    def test_reprocess_validates_and_queues(self):
        self.store.sync_catalog(self.catalog)
        self.assertEqual(at.reprocess(self.store, "ids", "retag", ids=self.ids[:2]), 2)
        self.assertEqual(at.reprocess(self.store, "all", "full"), 0)           # nothing processed yet
        for bad in [("ids", "retag", None, ""), ("ids", "retag", [], ""), ("ids", "retag", [1], ""), ("tag", "full", None, " "),
                    ("tag", "full", None, 5), ("some", "full", None, ""), ("all", "again", None, "")]:
            with self.assertRaises(ValueError, msg=str(bad)):
                at.reprocess(self.store, bad[0], bad[1], ids=bad[2], tag=bad[3])

    def test_listing_and_top_tags(self):
        self.store.sync_catalog(self.catalog)
        data = [(0, ["girl", "beach"], "A girl on a beach."), (1, ["dog", "grass"], "A dog."), (2, ["dog", "car"], "A 100%_real clip.")]
        for i, tags, text in data:
            self.store.save_result(self.ids[i], tags=[{"tag": t, "score": 1.0, "source": "wd"} for t in tags], vlm=None,
                                   description=text, block="", version=1)
            self.store.mark_written(self.ids[i])
        self.set(max_tags=7)
        everything = self.store.list_assets()
        self.assertEqual(everything["total"], 3)
        self.assertEqual([i["id"] for i in everything["items"]], self.ids[:3])                  # newest first
        self.assertEqual(everything["items"][0]["tags"], ["girl", "beach"])
        self.assertEqual(everything["items"][0]["description"], "A girl on a beach.")
        self.assertEqual(everything["items"][0]["settingsVersion"], 1)
        self.assertEqual(everything["tags"][0], {"tag": "dog", "count": 2})
        self.assertEqual(len(everything["tags"]), 5)
        self.assertEqual([i["id"] for i in self.store.list_assets(tag="dog")["items"]], self.ids[1:3])
        self.assertEqual([i["id"] for i in self.store.list_assets(q="BEACH")["items"]], [self.ids[0]])     # in the tags
        self.assertEqual([i["id"] for i in self.store.list_assets(q="a dog")["items"]], [self.ids[1]])     # in the description
        self.assertEqual([i["id"] for i in self.store.list_assets(q="IMG_0002")["items"]], [self.ids[2]])  # in the name
        self.assertEqual([i["id"] for i in self.store.list_assets(q="100%")["items"]], [self.ids[2]])      # % is not a wildcard
        self.assertEqual(self.store.list_assets(q="%")["total"], 1)
        self.assertEqual(self.store.list_assets(outdated=True)["total"], 3)
        paged = self.store.list_assets(size=2, page=2)
        self.assertEqual((paged["total"], len(paged["items"]), paged["page"]), (3, 1, 2))
        self.assertEqual(self.store.list_assets(tag="zzz")["total"], 0)

    def test_counts(self):
        self.store.sync_catalog(self.catalog)
        c = self.store.counts()
        self.assertEqual((c["assets"], c["images"], c["videos"], c["processed"], c["pending"], c["queued"]), (5, 4, 1, 0, 5, 0))
        self.store.exclude([self.ids[4]])
        self.store.enqueue([self.ids[0]], "retag")
        self.assertEqual((self.store.counts()["pending"], self.store.counts()["queued"], self.store.counts()["excluded"]), (4, 1, 1))

    def test_many_threads_can_use_the_store(self):
        self.store.sync_catalog(self.catalog)
        stop, errors = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    self.store.counts()
                    self.store.failures()
                    self.store.work(3)
                    self.store.list_assets()
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(150):
            a = self.ids[i % 5]
            self.store.save_raw(a, raw_of(DOG))
            self.store.save_result(a, tags=[{"tag": "dog", "score": 1.0, "source": "wd"}], vlm=None, description="", block="", version=1)
            self.store.mark_written(a)
            self.store.enqueue([a], "retag")
            self.store.fail(self.ids[(i + 1) % 5], "x")
        stop.set()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


# ---------------------------------------------------------------- writing to Immich

class TestWriteBack(Base):
    def setUp(self):
        super().setUp()
        self.store.sync_catalog(self.catalog)
        self.pipe = self.indexer.pipe
        self.settings = at.load_settings()

    def write(self, i=0, block=BLOCK, tags=("girl", "beach"), **settings):
        return self.pipe.write_back(self.ids[i], block, list(tags), {**self.settings, **settings})

    def test_an_empty_description_gets_the_block(self):
        got = self.write()
        self.assertEqual(self.description(0), BLOCK)
        self.assertEqual((got["old"], got["new"], got["changed"]), ("", BLOCK, True))
        self.assertEqual(self.store.history(self.ids[0]), [{"at": mock.ANY, "old": "", "new": BLOCK}])

    def test_the_owners_text_survives_every_write(self):
        self.put_description(0, "My holiday.\nSecond line.")
        self.write()
        self.assertEqual(self.description(0), "My holiday.\nSecond line.\n\n" + BLOCK)
        self.write(block=BLOCK2)
        self.assertEqual(self.description(0), "My holiday.\nSecond line.\n\n" + BLOCK2)
        self.put_description(0, "Intro.\n\n" + BLOCK2 + "\nOutro, edited by hand.")
        self.write(block=BLOCK)
        self.assertEqual(self.description(0), "Intro.\n\n" + BLOCK + "\nOutro, edited by hand.")
        self.write(block="")
        self.assertEqual(self.description(0), "Intro.\n\nOutro, edited by hand.")
        history = self.store.history(self.ids[0])
        self.assertEqual([h["old"] for h in history][0], "My holiday.\nSecond line.")           # the previous text is kept
        self.assertEqual(len(history), 4)

    def test_nothing_is_sent_when_nothing_changes(self):
        self.write()
        puts = len(self.fake.asset_puts)
        got = self.write()
        self.assertFalse(got["changed"])
        self.assertEqual(len(self.fake.asset_puts), puts)
        self.assertEqual(len(self.store.history(self.ids[0])), 1)
        self.assertEqual(self.fake.asset_puts[0], (self.ids[0], {"description": BLOCK}))      # only the description is sent

    def test_it_is_read_back_and_a_mismatch_is_an_error(self):
        self.fake.mangle_description = lambda d: d[:-4]
        with self.assertRaises(at.WriteMismatch):
            self.write()
        self.assertEqual(self.store.history(self.ids[0]), [])
        self.fake.mangle_description = lambda d: d + "\n  "        # Immich trimming the end is fine
        self.write()
        self.assertEqual(len(self.store.history(self.ids[0])), 1)

    def test_immich_errors_are_told_apart(self):
        a = self.ids[0]
        self.fake.fail_next[f"/api/assets/{a}"] = 1
        with self.assertRaises(at.ImmichDown):                     # a 500: Immich is struggling, not the asset's fault
            self.write()
        self.assertTrue(issubclass(at.ImmichDown, sp.ServiceDown))
        with self.assertRaises(at.AssetGone):
            self.pipe.write_back("00000000-0000-0000-0000-0000000000ff", BLOCK, [], self.settings)
        self.assertTrue(issubclass(at.AssetGone, ValueError))     # so it is a final failure
        broken = ImmichClient("http://127.0.0.1:1", API_KEY, retries=0)
        with self.assertRaises(at.ImmichDown):
            at.Pipeline(self.store, self.services, lambda: broken).write_back(a, BLOCK, [], self.settings)

    def test_native_tags_are_upserted_attached_and_detached(self):
        a = self.ids[0]
        self.set(write_tags=True)                                               # (this makes the panel manage AI/ tags)
        self.write(tags=("girl", "beach", "rating: general"), write_tags=True)
        named = lambda: sorted(self.fake.tags[t]["value"] for t in self.fake.asset_tags.get(a, []))  # noqa: E731
        self.assertEqual(named(), ["AI/beach", "AI/girl", "AI/rating: general"])
        self.assertIn(("PUT", "/api/tags"), self.fake.requests)
        self.assertIn(("PUT", "/api/tags/assets"), self.fake.requests)
        self.write(tags=("girl", "dog"), write_tags=True)                       # beach and rating are stale
        self.assertEqual(named(), ["AI/dog", "AI/girl"])
        mine = self.fake._upsert_tags(["Holiday"])[0]                           # the owner's own tag is left alone
        self.fake.asset_tags[a].append(mine["id"])
        self.write(tags=("girl",), write_tags=True)
        self.assertEqual(named(), ["AI/girl", "Holiday"])
        requests = len(self.fake.requests)
        self.write(tags=("girl",), write_tags=True)                             # nothing to do: no tag calls
        self.assertEqual([r for r in self.fake.requests[requests:] if "tags" in r[1]], [])
        self.write(tags=("girl",), write_tags=False)                            # switched off: the AI/ tags go (native flag set)
        self.assertEqual(named(), ["Holiday"])
        self.assertEqual(self.fake.tags[mine["id"]]["value"], "Holiday")

    def test_native_tags_are_not_touched_when_they_were_never_on(self):
        a = self.ids[0]
        stranger = self.fake._upsert_tags(["AI/by hand"])[-1]
        self.fake.asset_tags[a] = [stranger["id"]]
        self.write(tags=("girl",), write_tags=False)
        self.assertEqual(self.fake.asset_tags[a], [stranger["id"]])

    def test_a_tag_deleted_in_immich_is_made_again(self):
        a = self.ids[0]
        self.write(tags=("girl",), write_tags=True)
        self.fake.tags.clear()                                                   # deleted behind our back
        self.fake.asset_tags.clear()
        self.write(tags=("girl", "dog"), write_tags=True)
        self.assertEqual(sorted(self.fake.tags[t]["value"] for t in self.fake.asset_tags[a]), ["AI/dog", "AI/girl"])

    def test_remove_strips_the_block_and_forgets(self):
        self.put_description(0, "Mine.")
        self.put_description(1, BLOCK)
        for i in (0, 1):
            self.store.save_result(self.ids[i], tags=[], vlm=None, description="", block=BLOCK, version=1)
            self.store.mark_written(self.ids[i])
        self.write(0)
        self.assertEqual(self.description(0), "Mine.\n\n" + BLOCK)
        got = self.pipe.remove([self.ids[0], self.ids[1], self.ids[2]], exclude=False)
        self.assertEqual(got, {"removed": 3, "excluded": 0, "failed": []})
        self.assertEqual((self.description(0), self.description(1), self.description(2)), ("Mine.", "", ""))
        self.assertIsNone(self.store.result(self.ids[0]))
        self.assertEqual(self.store.counts()["processed"], 0)
        self.assertEqual(self.store.history(self.ids[1])[-1]["new"], "")
        self.assertEqual(self.store.counts()["excluded"], 0)

    def test_remove_and_exclude(self):
        self.write(0)
        got = self.pipe.remove([self.ids[0]], exclude=True)
        self.assertEqual((got["removed"], got["excluded"]), (1, 1))
        self.assertNotIn(self.ids[0], [w["id"] for w in self.store.work(10)])
        self.assertEqual(self.store.counts()["excluded"], 1)

    def test_remove_takes_the_ai_tags_off_too(self):
        a = self.ids[0]
        self.set(write_tags=True)
        self.write(tags=("girl",), write_tags=True)
        self.assertTrue(self.fake.asset_tags[a])
        self.pipe.remove([a], exclude=False)
        self.assertEqual(self.fake.asset_tags[a], [])

    def test_remove_reports_what_failed_and_keeps_those_results(self):
        a, b = self.ids[0], self.ids[1]
        for i in (a, b):
            self.store.save_result(i, tags=[], vlm=None, description="", block="", version=1)
        self.put_description(0, BLOCK)
        self.fake.fail_next[f"/api/assets/{a}"] = 1
        got = self.pipe.remove([a, b], exclude=True)
        self.assertEqual((got["removed"], got["excluded"], [f["id"] for f in got["failed"]]), (1, 2, [a]))
        self.assertIsNotNone(self.store.result(a))
        self.assertIsNone(self.store.result(b))


# ---------------------------------------------------------------- the indexer, end to end

class TestIndexer(Base):
    def test_tags_describes_and_writes_everything(self):
        self.run_indexer()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        c = self.store.counts()
        self.assertEqual((c["assets"], c["processed"], c["pending"], c["failed"]), (5, 3, 0, 2))
        self.assertEqual(self.description(0),
                         "[AI Tagger]\nTags: girl, beach, miku, solo, sea, rating: general\nDescription: A nice picture.\n[/AI Tagger]")
        self.assertEqual(self.description(1), "[AI Tagger]\nTags: dog, grass, rating: general\nDescription: A nice picture.\n[/AI Tagger]")
        self.assertEqual(self.description(2), "[AI Tagger]\nTags: dog, car, rating: general\nDescription: A nice picture.\n[/AI Tagger]")
        self.assertEqual(self.description(3), "")
        res = self.store.result(self.ids[0])
        self.assertEqual((res["settings_version"], res["note"]), (1, ""))
        self.assertIsNotNone(res["written_at"])
        self.assertEqual(res["tags"][0], {"tag": "girl", "score": 0.9, "source": "wd"})
        self.assertEqual(self.store.raw(self.ids[2])["captures"], 3)
        self.assertEqual(self.services.ready_calls[0], {"tagger": True, "vlm": True})
        self.assertEqual(sum(self.services.tag_calls), 1 + 1 + 3 + 1)                # the unreadable file never got to the tagger
        self.assertEqual(len(self.services.vlm_calls), 3)

    def test_the_vlm_sees_the_detected_tags_the_rating_and_the_terms_but_no_pictures(self):
        self.run_indexer(vocabulary="1girl -> girl\nsunny", instructions="Be brief.")
        call = next(c for c in self.services.vlm_calls if c["kind"] == "VIDEO")
        self.assertNotIn("images", call)                    # the describer is text only
        self.assertEqual(len(self.services.vlm_calls), 3)
        self.assertEqual([t["tag"] for t in call["tags"]][:2], ["dog", "car"])
        self.assertEqual(call["rating"]["general"], round((0.9 + 0.9 + 0.1) / 3, 3))
        self.assertEqual(call["terms"], ["girl", "sunny"])
        self.assertEqual(call["settings"]["instructions"], "Be brief.")

    def test_failures_are_noted_and_the_rest_goes_on(self):
        self.run_indexer()
        failures = {f["name"]: f["error"] for f in self.store.failures()}
        self.assertIn("no preview", failures["IMG_0003.jpg"])
        self.assertIn("cannot identify", failures["IMG_0004.jpg"])
        self.assertEqual(self.store.counts()["retrying"], 2)

    def test_files_that_are_not_media_are_not_retried(self):
        self.catalog[3]["preview"] = "NOTMEDIA"
        self.run_indexer()
        c = self.store.counts()
        self.assertEqual((c["failed"], c["retrying"]), (2, 1))

    def test_a_refusal_is_not_an_asset_failure(self):
        self.services.answer = lambda *a: {"description": "", "add_tags": [], "remove_tags": [],
                                           "note": "no description: the model refused"}
        self.run_indexer()
        self.assertEqual(self.store.counts()["processed"], 3)
        self.assertEqual(self.store.counts()["failed"], 2)                       # only the two real failures
        self.assertEqual(self.description(0), "[AI Tagger]\nTags: girl, beach, miku, solo, sea, rating: general\n[/AI Tagger]")
        self.assertEqual(self.store.result(self.ids[0])["note"], "no description: the model refused")

    def test_describe_off_never_touches_the_vlm(self):
        self.run_indexer(describe=False)
        self.assertEqual(self.services.vlm_calls, [])
        self.assertEqual(self.services.ready_calls[0], {"tagger": True, "vlm": False})
        self.assertNotIn("Description:", self.description(0))

    def test_the_owners_text_is_kept_while_tagging(self):
        self.put_description(0, "Holiday in Spain.")
        self.run_indexer()
        self.assertEqual(self.description(0).split("\n\n")[0], "Holiday in Spain.")
        self.assertIn("[AI Tagger]", self.description(0))

    def test_service_down_is_not_the_assets_fault(self):
        self.services.down = True
        self.run_indexer()
        self.assertEqual(self.indexer.state, "error")
        self.assertIn("down", self.indexer.error)
        self.assertEqual(self.store.conn.execute("select count(*) from failed").fetchone()[0], 0)
        self.assertEqual(self.store.counts()["pending"], 5)
        self.services.down = False                                                # and it picks up where it was
        self.run_indexer()
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_a_restarting_server_is_waited_for(self):
        real = self.services.ensure_ready
        calls = []

        def flaky(**kw):
            calls.append(1)
            if len(calls) <= 2:
                raise at.ServiceDown("restarting")
            return real(**kw)

        self.services.ensure_ready = flaky
        self.run_indexer()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_the_vlm_going_away_is_not_the_assets_fault_either(self):
        self.services.vlm_down = True
        self.run_indexer()
        self.assertEqual(self.indexer.state, "error")
        self.assertEqual(self.store.conn.execute("select count(*) from failed where error not like '%IMG%'").fetchone()[0], 2)
        c = self.store.counts()
        self.assertEqual(c["processed"], 0)
        self.assertEqual([self.description(i) for i in range(3)], ["", "", ""])
        self.services.vlm_down = False
        self.run_indexer()
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_immich_not_answering_keeps_the_work_for_later_without_the_gpu(self):
        self.catalog = self.catalog[:1]
        self.fake.fail_next[f"/api/assets/{self.ids[0]}"] = 99
        self.run_indexer()
        self.assertEqual(self.indexer.state, "error")
        res = self.store.result(self.ids[0])
        self.assertIsNotNone(res)                                                  # the work was kept ...
        self.assertIsNone(res["written_at"])                                        # ... but is not written yet
        c = self.store.counts()
        self.assertEqual((c["processed"], c["pending"], c["failed"]), (0, 1, 0))
        self.fake.fail_next.clear()
        tagged, described = list(self.services.tag_calls), len(self.services.vlm_calls)
        self.run_indexer()
        self.assertEqual(self.store.counts()["processed"], 1)
        self.assertEqual((self.services.tag_calls, len(self.services.vlm_calls)), (tagged, described))   # no second GPU pass
        self.assertIn("[AI Tagger]", self.description(0))

    def test_a_bad_api_key_is_an_error_not_a_failure_of_every_photo(self):
        self.catalog = self.catalog[:3]
        self.indexer.client = ImmichClient(self.fake.url, "wrong", retries=0)
        self.run_indexer()
        self.assertEqual(self.indexer.state, "error")
        self.assertIn("API key", self.indexer.error)
        self.assertEqual(self.store.counts()["failed"], 0)

    def test_a_description_immich_does_not_keep_is_a_failure_of_that_asset(self):
        self.fake.mangle_description = lambda d: d[:-4]
        self.run_indexer()
        self.assertEqual(self.indexer.state, "done")
        self.assertEqual(self.store.counts()["processed"], 0)
        self.assertIn("WriteMismatch", self.store.failures()[0]["error"])

    def test_out_of_memory_halves_the_batch_and_goes_on(self):
        self.catalog = [{**self.catalog[0], "id": self.ids[i], "name": f"IMG_{i:04d}.jpg", "taken": f"2026-01-0{9 - i} 10:00:00"}
                        for i in range(6)]
        self.services.oom_above = 2
        self.run_indexer(batch_size=8)
        self.assertEqual(self.store.counts()["processed"], 6)
        self.assertEqual(self.store.counts()["failed"], 0)
        self.assertEqual(self.indexer.batch_cap, 1)                                  # 6 -> 3 -> 1 ... works with 2
        self.assertEqual(self.services.oom_calls[:2], [6, 3])
        self.assertTrue(all(n <= 2 for n in self.services.tag_calls))
        self.assertEqual(sum(self.services.tag_calls), 6)
        self.assertEqual(self.indexer.batch_size(at.load_settings()), 1)
        self.set(batch_size=4)                                                       # the owner's new choice wins
        self.assertEqual(self.indexer.batch_size(at.load_settings()), 4)

    def test_out_of_memory_for_a_single_picture_fails_that_asset_only(self):
        self.services.oom_above = 0
        self.run_indexer()
        self.assertEqual(self.store.counts()["processed"], 0)
        self.assertIn("out of graphics memory", self.store.failures()[0]["error"])
        self.assertEqual(self.indexer.state, "done")

    def test_batches_have_at_most_batch_size_assets_and_64_pictures(self):
        self.catalog = [{**self.catalog[2], "id": self.ids[i], "name": f"v{i}.mp4", "taken": f"2026-01-{20 - i:02d} 10:00:00",
                         "preview": ";".join([DOG] * 8)} for i in range(10)]
        self.run_indexer(batch_size=64, video_frames=8)
        self.assertEqual(self.store.counts()["processed"], 10)
        self.assertEqual(sum(self.services.tag_calls), 80)
        self.assertTrue(all(n <= 64 for n in self.services.tag_calls))
        self.services.tag_calls.clear()
        self.store.enqueue(self.ids[:10], "full")
        self.run_indexer(batch_size=2)
        self.assertEqual(self.services.tag_calls, [16] * 5)

    def test_no_tagger_means_no_tags_to_describe_from_so_the_vlm_is_not_used(self):
        self.run_indexer(use_wd=False, use_pixai=False)
        self.assertEqual(self.services.tag_calls, [])
        self.assertEqual(self.services.vlm_calls, [])
        self.assertEqual(self.services.ready_calls[0], {"tagger": False, "vlm": False})      # not even started
        self.assertEqual(self.store.counts()["processed"], 4)               # no tagger to say "broken" about the last one
        self.assertEqual(self.description(0), "")                           # nothing to say: nothing written
        self.assertIn("no tags", self.store.result(self.ids[0])["note"])

    def test_a_picture_with_no_tags_is_not_sent_to_the_describer(self):
        self.catalog = [{**self.catalog[0], "preview": "wd:dull=0.1", "name": "dull.jpg"}]
        self.run_indexer(rating_tag=False)
        self.assertEqual(self.services.vlm_calls, [])
        res = self.store.result(self.ids[0])
        self.assertEqual((res["tags"], res["description"], res["block"]), ([], "", ""))
        self.assertIn("no tags to describe from", res["note"])
        self.assertEqual(self.description(0), "")

    def test_pause_stops_the_thread_and_resumes_later(self):
        gate = threading.Event()
        slow = self.indexer.frames

        def frames(item, n):
            gate.wait(10)
            return slow(item, n)

        self.indexer.frames = frames
        self.set(keep_updated=False)
        self.indexer.start()
        time.sleep(0.3)
        self.indexer.stop()
        gate.set()
        self.indexer.thread.join(15)
        self.assertFalse(self.indexer.running())
        self.assertEqual(self.indexer.state, "stopped")
        self.assertEqual(self.store.counts()["failed"], 0)
        self.indexer.frames = slow
        self.run_indexer()
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_it_waits_for_new_photos_and_wakes_when_asked(self):
        self.set(keep_updated=True)
        self.indexer.start()
        for _ in range(200):
            if self.indexer.state == "done":
                break
            time.sleep(0.05)
        self.assertEqual(self.indexer.state, "done")
        self.assertIn("new photos", self.indexer.detail)
        self.catalog.append({**self.catalog[1], "id": self.ids[10], "name": "new.jpg", "taken": "2026-02-01 10:00:00"})
        self.indexer.last_sync = None                         # as if ten minutes had passed
        self.indexer.start()                                  # a second start only wakes it
        for _ in range(200):
            if self.store.counts()["processed"] == 4:
                break
            time.sleep(0.05)
        self.assertEqual(self.store.counts()["processed"], 4)
        self.indexer.stop(5)
        self.assertFalse(self.indexer.running())

    def test_the_idle_clock_is_checked_while_waiting(self):
        self.services.idle_check = mock.Mock(return_value=False)
        self.indexer.IDLE_POLL = 0.05
        self.set(keep_updated=True)
        self.indexer.start()
        time.sleep(0.5)
        self.indexer.stop(5)
        self.assertGreaterEqual(self.services.idle_check.call_count, 2)

    def test_status_has_a_rate_and_a_time_left(self):
        now = [1000.0]
        self.indexer.clock = lambda: now[0]
        self.store.sync_catalog(self.catalog)
        self.indexer.state = "running"
        self.assertEqual(self.indexer.status()["ratePerMin"], None)
        for i in range(10):                                   # 10 assets in the last minute
            self.indexer.done_times.append((940.0 + i * 6, 1))
        st = self.indexer.status()
        self.assertAlmostEqual(st["ratePerMin"], 10.0, delta=2)
        self.assertEqual(st["etaMinutes"], round(5 / st["ratePerMin"]))
        self.assertEqual(set(st), {"state", "detail", "error", "running", "ratePerMin", "etaMinutes"})
        self.indexer.state = "stopped"
        self.assertEqual(self.indexer.status()["etaMinutes"], None)


class TestReprocess(Base):
    def setUp(self):
        super().setUp()
        self.run_indexer()
        self.services.tag_calls.clear()
        self.services.vlm_calls.clear()
        self.services.ready_calls.clear()

    def test_retag_needs_no_gpu_and_no_models(self):
        self.assertEqual(self.set(wd_strictness=0.85, blocked=["solo"], max_tags=5), ["wd_strictness", "blocked", "max_tags"])
        self.assertEqual(self.store.counts()["outdated"], 3)
        self.assertEqual(at.reprocess(self.store, "outdated", "retag"), 3)
        self.run_indexer()
        self.assertEqual((self.services.tag_calls, self.services.vlm_calls, self.services.ready_calls), ([], [], []))
        # girl .9 stays; WD's beach .6 and miku .8 are below .85, but PixAI still says beach; solo is blocked
        self.assertEqual(self.description(0), "[AI Tagger]\nTags: girl, beach, sea, rating: general\n"
                                              "Description: A nice picture.\n[/AI Tagger]")
        self.assertEqual(self.store.counts()["outdated"], 0)
        self.assertEqual(self.store.result(self.ids[0])["settings_version"], 2)
        self.assertEqual(self.store.queue(), [])
        self.assertEqual(len(self.store.history(self.ids[0])), 2)

    def test_retag_uses_the_stored_vlm_answer(self):
        self.services.answer = lambda *a: {"description": "SECOND", "add_tags": [], "remove_tags": [], "note": ""}
        self.set(rules=[rule(if_all=["dog"], add=["pet"])])
        at.reprocess(self.store, "all", "retag")
        self.run_indexer()
        self.assertIn("pet", self.description(1))
        self.assertIn("A nice picture.", self.description(1))                # the answer from the first run
        self.assertNotIn("SECOND", self.description(1))
        self.assertEqual(self.services.vlm_calls, [])

    def test_retag_applies_the_stored_vlm_tag_changes_again(self):
        self.services.answer = lambda *a: {"description": "d", "add_tags": ["smile"], "remove_tags": ["solo"], "note": ""}
        at.reprocess(self.store, "all", "full")
        self.run_indexer()
        self.assertIn("smile", self.description(0))
        self.assertNotIn("solo", self.description(0))
        self.set(blocked=["smile"])
        at.reprocess(self.store, "all", "retag")
        calls = len(self.services.vlm_calls)
        self.run_indexer()
        self.assertNotIn("smile", self.description(0))
        self.assertEqual(len(self.services.vlm_calls), calls)

    def test_retag_with_describe_off_drops_the_description(self):
        self.set(describe=False)
        at.reprocess(self.store, "all", "retag")
        self.run_indexer()
        self.assertNotIn("Description:", self.description(0))
        self.assertEqual(self.services.vlm_calls, [])
        self.assertEqual(self.services.ready_calls, [])

    def test_describe_runs_the_vlm_again_but_not_the_taggers(self):
        self.services.answer = lambda *a: {"description": "Brand new words.", "add_tags": [], "remove_tags": [], "note": ""}
        self.set(instructions="Write like a pirate.")
        at.reprocess(self.store, "all", "describe")
        cut = []
        slow = self.indexer.frames
        self.indexer.frames = lambda item, n: cut.append(item["id"]) or slow(item, n)
        self.run_indexer()
        self.assertEqual(cut, [])                                          # the describer needs no pictures: none were cut
        self.assertEqual(self.services.tag_calls, [])
        self.assertEqual(len(self.services.vlm_calls), 3)
        self.assertEqual(self.services.ready_calls[0], {"tagger": False, "vlm": True})
        self.assertIn("Brand new words.", self.description(0))
        self.assertEqual(self.services.vlm_calls[0]["settings"]["instructions"], "Write like a pirate.")
        self.assertEqual(self.store.result(self.ids[0])["vlm"]["description"], "Brand new words.")

    def test_describe_with_describe_off_is_just_a_retag(self):
        self.set(describe=False)
        at.reprocess(self.store, "all", "describe")
        self.run_indexer()
        self.assertEqual((self.services.vlm_calls, self.services.ready_calls), ([], []))

    def test_full_runs_everything_again(self):
        self.set(video_frames=2)
        at.reprocess(self.store, "outdated", "full")
        self.run_indexer()
        self.assertEqual(sum(self.services.tag_calls), 1 + 1 + 3)          # the video's three captures
        self.assertEqual(len(self.services.vlm_calls), 3)

    def test_without_stored_scores_retag_becomes_full(self):
        self.store.conn.execute("delete from raw where id=?", (self.ids[1],))
        self.store.conn.commit()
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])

    def make_v1(self, i):
        """Turn asset i's stored scores into what the RAM++ service left (WD scores plus a "ram" entry, no PixAI)."""
        scores = [{"wd": {"general": {"girl": 0.9}, "character": {}}, "ram": {"beach": 0.8}}]
        self.store.conn.execute("update raw set scores_json=?, rating_json=?, models=? where id=?",
                                (json.dumps(scores), json.dumps([{"general": 0.9}]), "ram,wd", self.ids[i]))
        self.store.conn.commit()
        self.assertFalse(at.has_pixai(self.store.raw(self.ids[i])))

    def test_stored_scores_without_pixai_make_retag_and_describe_a_full_reprocess(self):
        self.make_v1(1)
        self.set(max_tags=11)
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])                    # the taggers ran again for it, and only for it
        self.assertEqual(self.services.ready_calls[0], {"tagger": True, "vlm": True})
        self.assertEqual(len(self.services.vlm_calls), 1)
        self.assertTrue(at.has_pixai(self.store.raw(self.ids[1])))        # the stored scores are v2 now
        self.assertIn("grass", self.description(1))                       # PixAI's tag is in the result
        self.assertEqual(self.store.queue(), [])
        self.services.tag_calls.clear()
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])        # and the next retag is the cheap kind again
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])
        self.make_v1(0)
        at.reprocess(self.store, "ids", "describe", ids=[self.ids[0]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])                    # "describe" needs PixAI's scores too
        self.assertTrue(at.has_pixai(self.store.raw(self.ids[0])))

    def test_a_result_that_was_never_written_is_finished_as_full_when_its_scores_are_from_v1(self):
        self.make_v1(1)
        self.store.conn.execute("update results set written_at=null where id=?", (self.ids[1],))      # stored, not written
        self.store.conn.commit()
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_scores_without_pixai_are_fine_while_pixai_is_off(self):
        self.make_v1(1)
        self.set(use_pixai=False)                                          # nothing is lost: PixAI is not used
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual((self.services.tag_calls, self.services.ready_calls), ([], []))
        self.assertNotIn("grass", self.description(1))
        self.assertIn("girl", self.description(1))
        self.set(use_pixai=True)                                           # switching it on is a "full" change, as before
        self.assertEqual(at.suggest_mode(["use_pixai"]), "full")
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])

    def test_the_outdated_scope_reaches_old_ram_results_and_redoes_them_as_full(self):
        self.make_v1(0)
        self.store.conn.execute("delete from meta where key='raw_format'")
        self.store.conn.commit()
        self.store.conn.close()
        self.store = at.Store(self.store.folder)                           # the panel restarts with the v2 code
        self.addCleanup(self.store.conn.close)
        self.indexer = at.Indexer(self.store, self.services, client=self.client, catalog=lambda: self.catalog, frames=fake_frames)
        self.indexer.DROP_WAIT = 0.01
        self.assertEqual(self.store.counts()["outdated"], 3)               # the first open bumped the version once
        self.assertEqual(at.reprocess(self.store, "outdated", "retag"), 3)
        self.run_indexer()
        self.assertEqual(self.store.counts()["outdated"], 0)
        self.assertEqual(self.services.tag_calls, [1])                    # only the asset with RAM++ scores needed the GPU

    def test_the_queue_goes_before_new_photos(self):
        self.catalog.append({**self.catalog[0], "id": self.ids[10], "name": "new.jpg", "taken": "2026-03-01 10:00:00"})
        self.indexer.last_sync = None
        self.store.enqueue([self.ids[2]], "describe")
        order = []
        real = self.services.describe

        def spy(tags, rating, settings, kind="IMAGE", terms=None):
            order.append(kind)
            return real(tags, rating, settings, kind, terms)

        self.services.describe = spy
        self.run_indexer(batch_size=1)
        self.assertEqual(order[0], "VIDEO")                              # the queued video, then the new photo
        self.assertEqual(len(order), 2)

    def test_a_strong_request_beats_a_weak_one_in_the_run(self):
        a = self.ids[0]
        self.store.enqueue([a], "retag")
        self.store.enqueue([a], "full")
        self.store.enqueue([a], "describe")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])

    def test_remove_with_exclude_means_it_is_not_tagged_again(self):
        self.indexer.remove([self.ids[0]], exclude=True)
        self.assertEqual(self.description(0), "")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])
        self.assertEqual(self.description(0), "")
        self.assertIsNone(self.store.result(self.ids[0]))
        # asking for it explicitly brings it back
        self.store.enqueue([self.ids[0]], "full")
        self.run_indexer()
        self.assertIn("[AI Tagger]", self.description(0))

    def test_remove_without_exclude_tags_it_again(self):
        self.put_description(0, "Mine.\n\n" + self.description(0))
        self.indexer.remove([self.ids[0]], exclude=False)
        self.assertEqual(self.description(0), "Mine.")
        self.run_indexer()
        self.assertTrue(self.description(0).startswith("Mine.\n\n[AI Tagger]"))
        self.assertEqual(sum(self.services.tag_calls), 1)

    def test_native_tags_follow_a_retag(self):
        self.set(write_tags=True)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        a = self.ids[0]
        self.assertEqual(sorted(self.fake.tags[t]["value"] for t in self.fake.asset_tags[a]),
                         sorted("AI/" + t["tag"] for t in self.store.result(a)["tags"]))
        self.set(blocked=["girl"])
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertNotIn("AI/girl", [self.fake.tags[t]["value"] for t in self.fake.asset_tags[a]])
        self.set(write_tags=False)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.fake.asset_tags[a], [])


class TestTestCard(Base):
    def test_preview_shows_everything_and_writes_nothing(self):
        self.put_description(0, "Mine.")
        self.set(rules=[rule(if_all=["girl", "beach"], add=["summer"])])
        self.services.answer = lambda *a: {"description": "A girl.", "add_tags": ["smile"], "remove_tags": ["solo"], "note": ""}
        got = self.indexer.test(self.ids[0])
        self.assertEqual((got["id"], got["name"], got["type"], got["captures"]), (self.ids[0], "IMG_0000.jpg", "IMAGE", 1))
        self.assertEqual(got["frames"], [at.data_url(PHOTO.encode())])
        self.assertTrue({t["tag"] for t in got["models"]["wd"]} >= {"girl", "hat"})
        self.assertEqual(set(got["models"]), {"wd", "pixai", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in got["models"]["pixai"]}, {"beach": True, "sea": True, "wave": False})
        self.assertEqual(got["models"]["rating"]["general"], 0.9)
        self.assertEqual(got["vlm"], {"description": "A girl.", "add_tags": ["smile"], "remove_tags": ["solo"], "note": ""})
        self.assertEqual(got["rules"], [{"rule": 0, "added": ["summer"], "removed": []}])
        sources = {t["tag"]: t["source"] for t in got["tags"]}
        self.assertEqual((sources["girl"], sources["smile"], sources["summer"]), ("wd", "vlm", "rule"))
        self.assertNotIn("solo", sources)
        self.assertEqual(got["description"], "A girl.")
        self.assertEqual(got["currentDescription"], "Mine.")
        self.assertEqual(got["newDescription"], "Mine.\n\n" + got["block"])
        self.assertFalse(got["written"])
        self.assertEqual(self.description(0), "Mine.")                                 # nothing written
        self.assertIsNone(self.store.result(self.ids[0]))
        self.assertIsNone(self.store.raw(self.ids[0]))
        self.assertEqual(self.fake.asset_puts, [])
        self.assertEqual(self.services.ready_calls[0], {"tagger": True, "vlm": True})

    def test_apply_writes_and_remembers(self):
        got = self.indexer.test(self.ids[0], write=True)
        self.assertTrue(got["written"])
        self.assertEqual(self.description(0), got["newDescription"])
        res = self.store.result(self.ids[0])
        self.assertIsNotNone(res["written_at"])
        self.assertEqual(res["block"], got["block"])
        self.assertIsNotNone(self.store.raw(self.ids[0]))
        self.assertEqual(self.store.counts()["processed"], 1)

    def test_a_video_shows_all_its_captures(self):
        got = self.indexer.test(self.ids[2])
        self.assertEqual((got["captures"], len(got["frames"]), got["type"]), (3, 3, "VIDEO"))

    def test_an_unknown_asset_is_not_found_and_a_catalog_is_read_on_demand(self):
        self.assertEqual(self.store.meta("catalog_at"), "")
        self.assertEqual(self.indexer.test(self.ids[0])["id"], self.ids[0])             # the library list was read first
        self.assertNotEqual(self.store.meta("catalog_at"), "")
        with self.assertRaises(at.NotFound):
            self.indexer.test("00000000-0000-0000-0000-0000000000ee")

    def test_problems_with_the_picture_or_the_models_are_clear(self):
        with self.assertRaises(ValueError) as err:
            self.indexer.test(self.ids[3])                                              # no preview
        self.assertIn("Could not read", str(err.exception))
        with self.assertRaises(ValueError) as err:
            self.indexer.test(self.ids[4])                                              # the tagger can't read it
        self.assertIn("cannot identify", str(err.exception))
        self.services.down = True
        with self.assertRaises(sp.ServiceDown):
            self.indexer.test(self.ids[0])

    def test_it_waits_at_most_90_seconds_for_the_models(self):
        seen = []
        self.services.ensure_ready = lambda **kw: seen.append(kw)
        self.indexer.test(self.ids[0])
        self.assertEqual(seen[0]["wait"], at.PREVIEW_WAIT)
        self.assertEqual(at.PREVIEW_WAIT, 90)

    def test_no_tagger_models_selected(self):
        self.set(use_wd=False, use_pixai=False)
        got = self.indexer.test(self.ids[0])
        self.assertEqual(self.services.tag_calls, [])
        self.assertEqual(tags_of(got), [])
        self.assertEqual((self.services.vlm_calls, self.services.ready_calls[0]), ([], {"tagger": False, "vlm": False}))
        self.assertEqual(got["description"], "")                        # a text model with no tags would make things up
        self.assertIn("no tags", got["vlm"]["note"])


# ---------------------------------------------------------------- the model containers

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeRunner:
    """Stands in for the docker / nvidia-smi runner: containers have a state, compose up starts them."""

    SERVICES = {"tagger": "immich_aitagger", "vlm": "immich_aitagger_vlm"}

    def __init__(self, gpu="24576, 1024"):
        self.calls: list[tuple] = []
        self.state: dict[str, str] = {}              # absent = missing
        self.gpu = gpu
        self.up_fails = False

    def __call__(self, cmd, env=None, timeout=60):
        self.calls.append((list(cmd), env))
        if cmd[:2] == ["docker", "inspect"]:
            state = self.state.get(cmd[-1])
            if state is None:
                return at._Done(1, "", f"Error: No such object: {cmd[-1]}")
            return at._Done(0, "true\n" if state == "running" else "false\n")
        if cmd[:2] == ["docker", "compose"]:
            if self.up_fails:
                return at._Done(1, "", "no such image")
            self.state[self.SERVICES[cmd[-1]]] = "running"
            return at._Done(0)
        if cmd[:2] == ["docker", "stop"]:
            self.state[cmd[-1]] = "stopped"
            return at._Done(0)
        if os.path.basename(cmd[0]) == "nvidia-smi":
            return at._Done(0, self.gpu + "\n") if self.gpu else at._Done(127, "", "not found")
        raise AssertionError(f"unexpected command {cmd}")

    def commands(self, kind):
        return [c for c, _ in self.calls if c[:2] == ["docker", kind]]

    def ups(self):
        return [c for c in self.commands("compose")]


class FakeHealth:
    """A model server that becomes ready ``after`` seconds (on the fake clock) once the container runs."""

    def __init__(self, clock, runner, container, after=10, error=None, vlm=False):
        self.clock, self.runner, self.container, self.after, self.error, self.vlm = clock, runner, container, after, error, vlm
        self.started = None

    def health(self, timeout=3):
        if self.runner.state.get(self.container) != "running":
            self.started = None
            return False if self.vlm else None
        if self.started is None:
            self.started = self.clock()
        if self.error:
            return {"status": "error", "error": self.error}
        ready = self.clock() - self.started >= self.after
        if self.vlm:
            return ready
        return {"status": "ok" if ready else "loading", "error": None}


class TestServices(Base):
    def setUp(self):
        super().setUp()
        self.clock, self.runner = Clock(), FakeRunner()
        self.stopped_search = []
        self.make()

    def make(self, vlm_after=40, tagger_after=10, **kw):
        self.tagger = FakeHealth(self.clock, self.runner, "immich_aitagger", tagger_after)
        self.vlm = FakeHealth(self.clock, self.runner, "immich_aitagger_vlm", vlm_after, vlm=True)
        self.svc = at.Services(self.tagger, self.vlm, runner=self.runner, store=self.store, clock=self.clock,
                               sleep=self.clock.sleep, search_stop=lambda: self.stopped_search.append(len(self.runner.calls)), **kw)

    def test_vram_gb_is_the_taggers_cap_and_the_describers_share_is_a_constant(self):
        default = {"AITAGGER_VRAM_GB": str(at.VRAM_GB_DEFAULT), "AITAGGER_VLM_UTIL": str(at.VLM_UTIL), "AITAGGER_VLM_SEQS": "16"}
        self.assertEqual(self.svc.env(), default)
        lo, hi = at.VRAM_GB_LIMITS
        for gb in (lo, hi):                                                 # AITAGGER_VRAM_GB = vram_gb, nothing else moves
            self.assertEqual(self.svc.env(S(vram_gb=gb, vlm_parallel=4)),
                             {"AITAGGER_VRAM_GB": str(gb), "AITAGGER_VLM_UTIL": str(at.VLM_UTIL), "AITAGGER_VLM_SEQS": "4"})
        self.runner.gpu = "12288, 100"                                      # not derived from the card either
        self.make()
        self.assertEqual(self.svc.env(S(vram_gb=hi))["AITAGGER_VLM_UTIL"], str(at.VLM_UTIL))
        with mock.patch.object(at, "VLM_UTIL", 0.2):                        # one constant to change
            self.assertEqual(self.svc.env()["AITAGGER_VLM_UTIL"], "0.2")
        self.assertEqual(self.svc.env()["AITAGGER_VRAM_GB"], str(at.VRAM_GB_DEFAULT))
        self.assertTrue(0 < at.VLM_UTIL < 0.95)

    def test_the_card_size_is_still_reported_and_taken_to_be_24_gb_without_nvidia_smi(self):
        self.runner.gpu = ""
        self.make()
        self.assertEqual(self.svc.gpu(), {"totalGb": 24, "usedGb": None})

    def test_load_starts_both_containers_through_compose(self):
        self.assertEqual(self.svc.load(), ["immich_aitagger", "immich_aitagger_vlm"])
        cmds = self.runner.ups()
        compose = str(at.COMPOSE)
        self.assertTrue(compose.replace("\\", "/").endswith("deploy/aitagger/docker-compose.yml"))
        self.assertEqual(cmds[0], ["docker", "compose", "-p", "immich-aitagger", "-f", compose, "up", "-d", "tagger"])
        self.assertEqual(cmds[1], ["docker", "compose", "-p", "immich-aitagger", "-f", compose, "up", "-d", "vlm"])
        for _, env in [c for c in self.runner.calls if c[0][:2] == ["docker", "compose"]]:
            self.assertEqual(env, {"AITAGGER_VRAM_GB": str(at.VRAM_GB_DEFAULT), "AITAGGER_VLM_UTIL": str(at.VLM_UTIL),
                                   "AITAGGER_VLM_SEQS": "16"})
        self.assertEqual(self.svc.load(), [])                               # already running: nothing to do
        self.assertEqual(len(self.runner.ups()), 2)

    def test_only_what_is_needed_is_started(self):
        self.assertEqual(self.svc.load(need_vlm=False), ["immich_aitagger"])
        self.assertEqual(self.runner.state, {"immich_aitagger": "running"})
        self.assertEqual(self.svc.load(need_tagger=False), ["immich_aitagger_vlm"])

    def test_the_containers_are_recreated_only_when_the_derived_env_changed(self):
        self.svc.load()
        self.assertFalse(any("--force-recreate" in c for c in self.runner.ups()))
        self.assertEqual(json.loads(self.store.meta("services_env"))["immich_aitagger_vlm"],
                         {"AITAGGER_VLM_UTIL": str(at.VLM_UTIL), "AITAGGER_VLM_SEQS": "16"})
        self.svc.unload()
        self.runner.calls.clear()
        self.svc.load()                                                     # same settings: a plain start
        self.assertFalse(any("--force-recreate" in c for c in self.runner.ups()))
        self.svc.unload()
        self.set(vlm_parallel=4)
        self.runner.calls.clear()
        self.svc.load()
        recreated = [c[-1] for c in self.runner.ups() if "--force-recreate" in c]
        self.assertEqual(recreated, ["vlm"])                               # the tagger's env did not change
        self.svc.unload()
        self.set(vram_gb=at.VRAM_GB_LIMITS[1])                              # the taggers' cap: only the tagger is recreated
        self.runner.calls.clear()
        self.svc.load()
        self.assertEqual([c[-1] for c in self.runner.ups() if "--force-recreate" in c], ["tagger"])
        self.svc.unload()
        with mock.patch.object(at, "VLM_UTIL", at.VLM_UTIL + 0.05):         # the describer's share changed in the code
            self.runner.calls.clear()
            self.svc.load()
            self.assertEqual([c[-1] for c in self.runner.ups() if "--force-recreate" in c], ["vlm"])
        self.svc.unload()
        self.runner.calls.clear()
        self.svc.load()                                                     # back to the old share: recreated once more
        self.assertEqual([c[-1] for c in self.runner.ups() if "--force-recreate" in c], ["vlm"])
        self.svc.unload()
        self.runner.calls.clear()
        self.svc.load()                                                     # remembered: no more recreating
        self.assertFalse(any("--force-recreate" in c for c in self.runner.ups()))

    def test_a_container_that_exists_with_an_unknown_env_is_recreated(self):
        self.runner.state["immich_aitagger_vlm"] = "stopped"               # made by hand, before the panel remembered anything
        self.svc.load()
        self.assertEqual([c[-1] for c in self.runner.ups() if "--force-recreate" in c], ["vlm"])

    @mock.patch.object(sp, "AITAGGER_EXCLUSIVE", True)
    def test_search_plus_is_stopped_before_anything_starts(self):
        self.runner.state["immich_searchplus"] = "running"
        self.svc.load()
        self.assertEqual(len(self.stopped_search), 1)
        first_up = next(i for i, (c, _) in enumerate(self.runner.calls) if c[:2] == ["docker", "compose"])
        self.assertLessEqual(self.stopped_search[0], first_up)              # before the first start
        self.runner.state["immich_searchplus"] = "stopped"
        self.svc.unload()
        self.stopped_search.clear()
        self.svc.load()
        self.assertEqual(self.stopped_search, [])

    def test_the_exclusive_switch_decides_whether_search_plus_is_stopped(self):
        self.assertFalse(sp.AITAGGER_EXCLUSIVE)     # shipped: they share the card (measured 17.3 GB with everything)
        for exclusive in (True, False):
            with self.subTest(exclusive=exclusive), mock.patch.object(sp, "AITAGGER_EXCLUSIVE", exclusive):
                self.runner.state = {"immich_searchplus": "running"}
                self.stopped_search.clear()
                self.assertEqual(self.svc.load(), ["immich_aitagger", "immich_aitagger_vlm"])
                self.assertEqual(len(self.stopped_search), 1 if exclusive else 0)       # False: Search+ is left running
                self.assertEqual(self.runner.state["immich_searchplus"], "running")     # (the fake stop is only a record)
                self.svc.unload()

    def test_search_plus_is_left_alone_when_the_models_are_already_up(self):
        self.svc.load()
        self.runner.state["immich_searchplus"] = "running"
        self.svc.load()
        self.assertEqual(self.stopped_search, [])

    def test_a_failed_start_is_a_service_down_with_the_reason(self):
        self.runner.up_fails = True
        with self.assertRaises(sp.ServiceDown) as err:
            self.svc.load()
        self.assertIn("no such image", str(err.exception))

    def test_ensure_ready_starts_and_waits_for_both(self):
        progress = []
        self.svc.ensure_ready(wait=600, progress=progress.append)
        self.assertEqual(self.clock.t, 1000 + 40)                           # the language model takes longest
        self.assertTrue(progress)
        self.assertIn("loading the", progress[0])
        before = len(self.runner.calls)
        self.svc.ensure_ready()                                             # ready: no work
        self.assertEqual(len(self.runner.ups()), 2)
        self.assertEqual(self.clock.t, 1040)
        self.assertTrue(len(self.runner.calls) >= before)

    def test_ensure_ready_without_the_vlm_does_not_start_it(self):
        self.svc.ensure_ready(need_vlm=False, wait=600)
        self.assertEqual(self.runner.state, {"immich_aitagger": "running"})
        self.assertEqual(self.clock.t, 1010)

    def test_ensure_ready_gives_up_after_the_wait(self):
        self.make(vlm_after=10_000)
        with self.assertRaises(sp.ServiceDown) as err:
            self.svc.ensure_ready(wait=90)
        self.assertIn("still loading", str(err.exception))
        self.assertGreaterEqual(self.clock.t, 1090)
        self.assertLess(self.clock.t, 1100)
        self.assertEqual(len(self.runner.ups()), 2)                         # it was started, so a retry finds it further on

    def test_ensure_ready_stops_when_asked(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(sp.ServiceDown):
            self.svc.ensure_ready(stop=stop)

    def test_a_model_that_fails_to_load_is_an_error_with_the_reason(self):
        self.tagger.error = "CUDA error: no kernel image"
        with self.assertRaises(RuntimeError) as err:
            self.svc.ensure_ready(wait=60)
        self.assertIn("no kernel image", str(err.exception))

    def test_a_container_that_dies_while_loading_is_reported(self):
        self.make(vlm_after=10_000)
        real_sleep = self.clock.sleep

        def sleep(s):
            real_sleep(s)
            if self.clock.t > 1020:
                self.runner.state["immich_aitagger_vlm"] = "stopped"

        self.svc.sleep = sleep
        with self.assertRaises(RuntimeError) as err:
            self.svc.ensure_ready(wait=600)
        self.assertIn("docker logs immich_aitagger_vlm", str(err.exception))

    def test_unload_stops_both(self):
        self.svc.load()
        self.assertEqual(self.svc.unload(), ["immich_aitagger_vlm", "immich_aitagger"])
        self.assertEqual(self.runner.state, {"immich_aitagger": "stopped", "immich_aitagger_vlm": "stopped"})
        self.assertEqual(self.svc.unload(), [])

    def test_the_vlm_container_is_stopped_after_20_idle_minutes(self):
        self.svc.ensure_ready(wait=600)
        self.assertFalse(self.svc.idle_check())
        self.clock.t += 19 * 60
        self.assertFalse(self.svc.idle_check())
        self.assertEqual(self.runner.state["immich_aitagger_vlm"], "running")
        self.svc.touch()                                                    # work was done
        self.clock.t += 19 * 60
        self.assertFalse(self.svc.idle_check())
        self.clock.t += 2 * 60
        self.assertTrue(self.svc.idle_check())
        self.assertEqual(self.runner.state["immich_aitagger_vlm"], "stopped")
        self.assertEqual(self.runner.state["immich_aitagger"], "running")   # the tagger stops by itself
        self.assertFalse(self.svc.idle_check())                             # nothing left to stop
        self.assertEqual(at.IDLE_EXIT_MINUTES, 20)

    def test_a_vlm_that_is_still_loading_gets_three_times_as_long(self):
        self.make(vlm_after=10 ** 9)                                         # the first start downloads the model
        self.svc.load()
        self.clock.t += 30 * 60
        self.assertFalse(self.svc.idle_check())
        self.assertEqual(self.runner.state["immich_aitagger_vlm"], "running")
        self.clock.t += 31 * 60
        self.assertTrue(self.svc.idle_check())                               # an hour and more: given up
        self.assertEqual(self.runner.state["immich_aitagger_vlm"], "stopped")

    def test_waiting_for_the_models_is_using_them(self):
        self.make(vlm_after=40 * 60)
        with self.assertRaises(sp.ServiceDown):
            self.svc.ensure_ready(wait=30 * 60)
        self.assertGreater(self.svc.last_used, self.clock.t - 5)
        self.assertFalse(self.svc.idle_check())

    def test_using_the_models_counts_as_work(self):
        self.svc.tagger = mock.Mock(tag=mock.Mock(return_value=([], [])))
        self.svc.vlm = mock.Mock(describe=mock.Mock(return_value={}))
        self.clock.t += 30 * 60
        self.svc.tag([b"x"])
        self.assertFalse(self.svc.idle_check())
        self.clock.t += 30 * 60
        self.svc.describe([], {}, S())
        self.assertFalse(self.svc.idle_check())

    def test_container_state_is_remembered_for_five_seconds(self):
        self.svc.tagger_box.container_state()
        self.svc.tagger_box.container_state()
        self.assertEqual(len(self.runner.commands("inspect")), 1)
        self.clock.t += 6
        self.svc.tagger_box.container_state()
        self.assertEqual(len(self.runner.commands("inspect")), 2)
        self.svc.tagger_box.container_state(fresh=True)
        self.assertEqual(len(self.runner.commands("inspect")), 3)
        self.runner.state["immich_aitagger"] = "running"
        self.assertEqual(self.svc.tagger_box.container_state(), "missing")            # still remembered
        self.svc.tagger_box.invalidate()
        self.assertEqual(self.svc.tagger_box.container_state(), "running")

    def test_status_is_cheap_and_has_the_contract_shape(self):
        st = self.svc.status()
        self.assertEqual(st, {"tagger": {"container": "missing", "status": "down", "error": None},
                              "vlm": {"container": "missing", "status": "down", "error": None},
                              "gpu": {"totalGb": 24.0, "usedGb": 1.0}, "searchplusRunning": False})
        n = len(self.runner.calls)
        for _ in range(5):
            self.svc.status()
        self.assertEqual(len(self.runner.calls), n)                          # all from memory
        self.svc.load(need_vlm=False)
        self.svc.tagger_box.invalidate()
        self.svc.vlm_box.invalidate()
        st = self.svc.status()
        self.assertEqual(st["tagger"], {"container": "running", "status": "loading", "error": None})
        self.runner.state["immich_aitagger_vlm"] = "running"
        self.svc.vlm_box.invalidate()
        self.assertEqual(self.svc.status()["vlm"]["status"], "loading")
        self.clock.t += 60
        self.runner.state["immich_searchplus"] = "running"
        st = self.svc.status()
        self.assertEqual((st["tagger"]["status"], st["vlm"]["status"], st["searchplusRunning"]), ("ok", "ok", True))

    def test_docker_missing_is_unknown_not_a_crash(self):
        runner = mock.Mock(return_value=at._Done(127, "", "docker not found"))
        box = at.ComposeService("c", "s", runner)
        self.assertEqual(box.container_state(), "unknown")
        runner.return_value = at._Done(1, "", "Cannot connect to the Docker daemon")
        self.assertEqual(box.container_state(fresh=True), "unknown")

    def test_the_default_runner_never_raises(self):
        self.assertEqual(at.run_command(["definitely-not-a-command-xyz"]).returncode, 127)


class TestWithRealClients(Base):
    """The Indexer on the real Services / Tagger / VLM classes, talking HTTP to stand-in model servers."""

    def test_everything_end_to_end(self):
        def tag(body):
            got = [read_picture(base64.b64decode(i)) for i in body["images"]]
            return 200, {"results": [g[0] for g in got], "errors": [g[1] for g in got], "tookMs": 1}

        def chat(body):
            text = body["messages"][1]["content"]
            tags = text.split("confidence: ", 1)[1].splitlines()[0]
            return 200, {"choices": [{"message": {"content": json.dumps(
                {"description": f"Seen: {tags.split(',')[0].rsplit(' ', 1)[0]}.", "add_tags": ["extra"],
                 "remove_tags": []})}, "finish_reason": "stop"}]}

        tagger = StubServer({("POST", "/tag"): tag, ("GET", "/health"): lambda b: (200, {"status": "ok"})})
        vlm = StubServer({("POST", "/v1/chat/completions"): chat, ("GET", "/health"): lambda b: (200, {})})
        self.addCleanup(tagger.close)
        self.addCleanup(vlm.close)
        runner = FakeRunner()
        runner.state = {"immich_aitagger": "running", "immich_aitagger_vlm": "running"}
        services = at.Services(at.Tagger(tagger.url), at.VLM(vlm.url), runner=runner, store=self.store)
        self.indexer = at.Indexer(self.store, services, client=self.client, catalog=lambda: self.catalog, frames=fake_frames)
        self.indexer.DROP_WAIT = 0.01
        self.run_indexer()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 3)
        self.assertEqual(self.description(1),
                         "[AI Tagger]\nTags: dog, extra, grass, rating: general\nDescription: Seen: dog.\n[/AI Tagger]")
        self.assertEqual(len([r for r in tagger.requests if r[1] == "/tag"]), 1)                   # one batch
        self.assertEqual(len([r for r in vlm.requests if r[1] == "/v1/chat/completions"]), 3)
        for _method, _path, body in [r for r in vlm.requests if r[1] == "/v1/chat/completions"]:
            self.assertTrue(all(isinstance(m["content"], str) for m in body["messages"]))           # text only, no picture
            self.assertNotIn("image_url", json.dumps(body))
        self.assertEqual(runner.commands("compose"), [])                                            # they were running already
        status = services.status()
        self.assertEqual((status["tagger"]["status"], status["vlm"]["status"]), ("ok", "ok"))
        # and a retag afterwards needs no request to either server
        before = (len(tagger.requests), len(vlm.requests))
        self.set(blocked=["extra"])
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual((len(tagger.requests), len(vlm.requests)), before)
        self.assertNotIn("extra", self.description(1))
        self.assertIn("Seen: dog.", self.description(1))


class TestInstance(Base):
    def test_one_per_home_and_autostart_resumes_only_when_it_was_on(self):
        with mock.patch.object(at.Indexer, "start") as start, mock.patch.object(at, "_watch"):
            at.autostart(self.client)
            start.assert_not_called()
            store, services, indexer = at.instance()
            self.assertIs(at.instance()[2], indexer)
            self.assertIs(indexer.client, self.client)
            at.apply_settings({"indexing": True})
            at.autostart(self.client)
            start.assert_called_once()
        self.assertIsInstance(services, at.Services)
        self.assertEqual(store.folder, at.home())

    def test_autostart_never_breaks_the_panel(self):
        with mock.patch.object(at, "instance", side_effect=RuntimeError("boom")):
            at.autostart()


if __name__ == "__main__":
    unittest.main()
