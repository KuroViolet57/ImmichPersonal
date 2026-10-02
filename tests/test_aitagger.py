import base64
import collections
import io
import itertools
import json
import os
import shutil
import sqlite3
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
# A "picture" is bytes like  b"wd:girl=0.9,beach=0.6|char:miku=0.8|pixai:beach=0.8|ram:sea=0.7|e621:anthro=0.9|rating:general=0.9":
# what the tagger would answer for it. An asset's captures are separated by ";" in the catalogue's `preview`.
# Parts: wd (WD general), char (WD character), rating (WD rating), pixai (PixAI general), pchar (PixAI character),
# copy (PixAI copyright), prating (PixAI rating; left out, PixAI says nothing about the rating), ram (RAM++, the real
# third tagger: ``{"general": {...}}``, no characters, no rating), e621 (Hydra 3.5, the real fourth tagger: general),
# e6species, e6char, e6copy (its species, character and copyright categories; it has no rating) and, for the fake FIFTH
# tagger ``EXTRA`` below (it is registered only by ``with_extra``): extra (general), echar (character), erating (rating).
# A picture that has no ``ram:`` part is one RAM++ names nothing in (as on most illustration); the same goes for
# ``e621:`` and the other e621 parts: Hydra answers (with four empty categories) but names nothing.
# The real service is asked for ``at.FLOOR`` (0.2) and never sends a tag score below it; this fake does not drop
# them, so a fixture score below 0.2 stands for scores stored when the floor was 0.05: nothing under 0.2 is kept or
# listed today either way (the strictness limits start at 0.2).

PHOTO = ("wd:girl=0.9,beach=0.6,solo=0.8,hat=0.3|char:miku=0.8|pixai:beach=0.8,sea=0.7,wave=0.4|"
         "rating:general=0.9,sensitive=0.08,questionable=0.01,explicit=0.01")
DOG = "wd:dog=0.9|pixai:dog=0.95,grass=0.7|rating:general=0.95,sensitive=0.05"
CLIP = ";".join(["wd:dog=0.9|pixai:dog=0.9,car=0.6|rating:general=0.9",
                "wd:dog=0.2|pixai:car=0.9|rating:general=0.9",
                "wd:dog=0.9|pixai:car=0.3|rating:sensitive=0.9,general=0.1"])
# PHOTO as RAM++ sees it: it names the sea again and adds what the anime taggers have no word for. "image" and "catch"
# are two of its noise words (never kept); "wave" is 0.3: shown on the Test card, not kept.
RAM_PHOTO = PHOTO + "|ram:sea=0.95,lake=0.9,screenshot=0.6,wave=0.3,image=0.9,catch=0.8"
# What Hydra (e621) says about a furry picture: its general tags (``mammal`` is its noise word: it lands on every human
# and animal), species, one character and one series (copyright). "tail" (0.3), "fox" (0.25) and "loki" (0.3) are below
# 0.5: listed on the Test card, not kept.
E621_PARTS = ("e621:anthro=0.95,mammal=0.99,fur=0.6,tail=0.3|e6species:wolf=0.9,canine=0.6,fox=0.25|"
              "e6char:fenrir=0.8,loki=0.3|e6copy:norse mythology=0.7")
E621_PHOTO = PHOTO + "|" + E621_PARTS             # PHOTO as Hydra sees it
ALL_PHOTO = RAM_PHOTO + "|" + E621_PARTS          # ... and as all four taggers see it

# A fifth tagger that exists only in the tests, to prove the registry can grow: off by default, like a new tagger would
# be. ``with_extra(self)`` registers it on top of the real four for one test and restores the registry afterwards.
EXTRA = at.TaggerKind("extra", "extra-tagger-test", ("general", "character"), ("character",), default_on=False)
EXTRA_ON = at.TaggerKind("extra", "extra-tagger-test", ("general", "character"), ("character",))      # on by default
EXTRA_NO_RATING = at.TaggerKind("extra", "extra-tagger-test", ("general", "character"), ("character",),
                                has_rating=False, default_on=False)
EXTRA_NOISY = at.TaggerKind("extra", "extra-tagger-test", ("general", "character"), ("character",),
                            noise=frozenset({"filler"}))
REAL = ["wd", "pixai", "ram", "e621"]       # the registry as it ships (hard-coded here on purpose: a new tagger is a decision)


def with_extra(test, kind=EXTRA) -> None:
    """Register the fake fifth tagger for the duration of ``test`` (call it first in setUp)."""
    saved = list(at.TAGGERS)
    at.configure_taggers([*saved, kind])
    test.addCleanup(at.configure_taggers, saved)


def read_picture(data: bytes, models=None):
    """(the tagger's answer for this picture, None) or (None, why). ``models`` are the tagger keys that were asked for
    (None: all of them); the real service answers only for those. RAM++ and Hydra answer like the real service does:
    RAM++'s tags under ``general`` and no rating, Hydra's under ``general``, ``species``, ``character`` and
    ``copyright`` and no rating."""
    text = data.decode()
    if text == "broken":
        return None, "cannot identify image file"
    out = {"wd": {"general": {}, "character": {}, "rating": {}},
           "pixai": {"general": {}, "character": {}, "copyright": {}, "rating": {}},
           "ram": {"general": {}},
           "e621": {"general": {}, "species": {}, "character": {}, "copyright": {}},
           "extra": {"general": {}, "character": {}, "rating": {}}}
    where = {"wd": ("wd", "general"), "char": ("wd", "character"), "rating": ("wd", "rating"),
             "pixai": ("pixai", "general"), "pchar": ("pixai", "character"), "copy": ("pixai", "copyright"),
             "prating": ("pixai", "rating"), "ram": ("ram", "general"),
             "e621": ("e621", "general"), "e6species": ("e621", "species"), "e6char": ("e621", "character"),
             "e6copy": ("e621", "copyright"),
             "extra": ("extra", "general"), "echar": ("extra", "character"), "erating": ("extra", "rating")}
    for part in filter(None, text.split("|")):
        key, _, rest = part.partition(":")
        model, category = where[key]
        out[model][category] = {k: float(v) for k, v in (kv.split("=") for kv in rest.split(",") if kv)}
    if models is not None:
        out = {k: v for k, v in out.items() if k in models}
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
        self.tag_models: list = []               # the ``models`` asked for in each of those requests
        self.oom_calls: list[int] = []           # pictures per /tag request that ran out of memory
        self.ready_calls: list[dict] = []
        self.loads = self.unloads = 0
        self.down = False                        # the tagger / container is not answering
        self.oom_above: int | None = None        # a /tag request with more pictures than this is out of memory
        self._lock = threading.Lock()

    def ensure_ready(self, need_tagger=True, wait=0, progress=None, stop=None):
        self.ready_calls.append({"tagger": need_tagger})
        if self.down:
            raise at.ServiceDown("down")

    def tag(self, images, models=None):
        if self.down:
            raise at.ServiceDown("down")
        if self.oom_above is not None and len(images) > self.oom_above:
            with self._lock:
                self.oom_calls.append(len(images))
            raise at.GpuOOM("CUDA out of memory")
        with self._lock:
            self.tag_calls.append(len(images))
            self.tag_models.append(None if models is None else list(models))
        got = [read_picture(b, models) for b in images]
        return [g[0] for g in got], [g[1] for g in got]

    def load(self):
        self.loads += 1
        return ["immich_aitagger"]

    def unload(self):
        self.unloads += 1
        return ["immich_aitagger"]

    def status(self):
        return {"tagger": {"container": "stopped", "status": "down", "error": None},
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
    """A store in a temp folder, the fake Immich, the fake model container and a real Indexer on top."""

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

    def test_vocabulary_only_renames(self):
        v = at.Vocabulary("1girl -> Girl\n  solo   -> alone \nhorse\n\n-> nothing\nbad ->\nsame -> same\nA_B -> c_d")
        self.assertEqual(v.renames, {"1girl": "girl", "solo": "alone", "a b": "c d"})      # other lines are ignored
        self.assertFalse(hasattr(v, "terms"))                                              # no preferred terms any more
        self.assertEqual(v.rename("1girl"), "girl")
        self.assertEqual(v.rename("dog"), "dog")
        self.assertEqual(at.Vocabulary("a → b").renames, {"a": "b"})
        self.assertEqual(at.Vocabulary("just a term\nanother").renames, {})
        self.assertEqual(at.Vocabulary("").renames, {})


def combos(text: str, **settings) -> dict:
    """The tags that ``at.build`` leaves for PHOTO-like scores when the vocabulary is ``text``."""
    return at.build(raw_of(settings.pop("picture", PHOTO)), S(vocabulary=text, **settings))


class TestVocabularySyntax(unittest.TestCase):
    """The text of the Vocabulary setting: renames and combinations (``a + b -> c``), one entry per line."""

    def parse(self, text):
        v = at.Vocabulary(text)
        self.assertEqual(v.errors, [], v.problems())
        return v

    def only_rule(self, text):
        rules = self.parse(text).rules
        self.assertEqual(len(rules), 1, text)
        return {k: v for k, v in rules[0].items() if k != "label"}

    def error_of(self, text):
        v = at.Vocabulary(text)
        self.assertEqual(len(v.errors), 1, (text, v.errors))
        return v.errors[0]

    def test_a_plain_arrow_is_a_rename(self):
        v = self.parse("1girl -> girl\nlong hair -> hair long\nrating: general -> safe")
        self.assertEqual(v.renames, {"1girl": "girl", "long hair": "hair long", "rating: general": "safe"})
        self.assertEqual(v.rules, [])
        self.assertEqual(self.parse("a → b").renames, {"a": "b"})                  # the arrow character works too
        self.assertEqual(self.parse("a->b").renames, {"a": "b"})                    # no spaces around the arrow
        self.assertEqual(self.parse("a -> a\nb -> b").renames, {})                  # a rename to itself changes nothing

    def test_and(self):
        self.assertEqual(self.only_rule("a + b -> c"),
                         rule(if_all=["a", "b"], add=["c"]))
        self.assertEqual(self.only_rule("a + b + c -> d"), rule(if_all=["a", "b", "c"], add=["d"]))
        self.assertEqual(self.parse("a + b -> c").renames, {})

    def test_or(self):
        self.assertEqual(self.only_rule("a | b -> c"), rule(if_any=["a", "b"], add=["c"]))
        self.assertEqual(self.only_rule("a | b | c -> d"), rule(if_any=["a", "b", "c"], add=["d"]))
        self.assertEqual(self.only_rule("a | a -> b"), rule(if_any=["a"], add=["b"]))

    def test_not(self):
        self.assertEqual(self.only_rule("a + !b -> c"), rule(if_all=["a"], unless=["b"], add=["c"]))
        self.assertEqual(self.only_rule("!b + a -> c"), rule(if_all=["a"], unless=["b"], add=["c"]))     # any position
        self.assertEqual(self.only_rule("a + !b + !c + d -> e"),
                         rule(if_all=["a", "d"], unless=["b", "c"], add=["e"]))
        self.assertEqual(self.only_rule("a + ! b -> c"), rule(if_all=["a"], unless=["b"], add=["c"]))   # a space after ! is fine

    def test_the_right_side_has_several_targets(self):
        self.assertEqual(self.only_rule("a + b -> c, -d"), rule(if_all=["a", "b"], add=["c"], remove=["d"]))
        self.assertEqual(self.only_rule("a + b -> -d, +c, e"), rule(if_all=["a", "b"], add=["c", "e"], remove=["d"]))
        self.assertEqual(self.only_rule("a | b -> - d , + c"), rule(if_any=["a", "b"], add=["c"], remove=["d"]))
        self.assertEqual(self.only_rule("a + b -> c, c, +c"), rule(if_all=["a", "b"], add=["c"]))        # no repeats
        self.assertEqual(self.only_rule("a + b -> c,"), rule(if_all=["a", "b"], add=["c"]))              # an empty piece is skipped

    def test_one_condition_with_a_sign_or_several_results_is_a_rule_not_a_rename(self):
        v = self.parse("a -> +b")
        self.assertEqual(v.renames, {})                                              # a is kept
        self.assertEqual(v.rules, [{**rule(if_all=["a"], add=["b"]), "label": "line 1"}])
        self.assertEqual(self.only_rule("a -> -b"), rule(if_all=["a"], remove=["b"]))
        self.assertEqual(self.only_rule("a -> b, c"), rule(if_all=["a"], add=["b", "c"]))
        self.assertEqual(self.only_rule("a -> b, -a"), rule(if_all=["a"], add=["b"], remove=["a"]))     # the long way to rename
        self.assertEqual(self.only_rule("a -> +a_b"), rule(if_all=["a"], add=["a b"]))
        self.assertEqual(self.parse("a -> b").renames, {"a": "b"})                  # and the plain form is the rename
        self.assertEqual(self.parse("a -> b").rules, [])
        v = self.parse("a -> b\na -> +c")                                           # both on one tag
        self.assertEqual((v.renames, [r["add"] for r in v.rules]), ({"a": "b"}, [["c"]]))

    def test_blank_lines_and_comments_are_ignored_and_lines_are_counted_anyway(self):
        v = self.parse("# my tags\n\n   \n  # indented note -> still a note\na + b -> c\r\n\r\n# end\nd -> e")
        self.assertEqual(v.renames, {"d": "e"})
        self.assertEqual([r["label"] for r in v.rules], ["line 5"])                  # CRLF is one break; blanks count
        self.assertEqual(at.Vocabulary("").rules, [])
        self.assertEqual(at.Vocabulary(None).renames, {})

    def test_tags_are_normalised_like_everywhere_else(self):
        self.assertEqual(self.only_rule("Long_Hair + Hatsune_Miku_(Vocaloid) -> Two_Girls, -SOLO"),
                         rule(if_all=["long hair", "hatsune miku (vocaloid)"], add=["two girls"], remove=["solo"]))
        self.assertEqual(self.only_rule("  A   B +  !C_D  ->   E  "), rule(if_all=["a b"], unless=["c d"], add=["e"]))
        self.assertEqual(self.parse("1Girl -> Woman_").renames, {"1girl": "woman"})
        self.assertEqual(self.only_rule("a + a -> b"), rule(if_all=["a"], add=["b"]))              # the same tag twice

    def test_tags_that_contain_signs_are_not_operators(self):
        self.assertEqual(self.only_rule("c++ + x -> y"), rule(if_all=["c++", "x"], add=["y"]))
        self.assertEqual(self.only_rule("+_+ | ^_^ -> eyes"), rule(if_any=["+ +", "^ ^"], add=["eyes"]))
        self.assertEqual(self.parse("non-furry -> human").renames, {"non-furry": "human"})
        self.assertEqual(self.parse("furry with non-furry -> human on anthro").renames,
                         {"furry with non-furry": "human on anthro"})
        self.assertEqual(self.only_rule("a -> ++_+"), rule(if_all=["a"], add=["+ +"]))            # a tag that starts with a sign

    def test_the_rules_carry_the_line_they_came_from(self):
        v = self.parse("a -> b\n# note\nx + y -> z\n\nx | y -> w")
        self.assertEqual([r["label"] for r in v.rules], ["line 3", "line 5"])

    def test_every_kind_of_mistake_names_its_line_and_the_problem(self):
        for text, problem in [
                ("a + b | c -> d", "use + or |, not both"),
                ("a | b + c -> d", "use + or |, not both"),
                ("a + !b | c -> d", "use + or |, not both"),
                ("a | !b -> c", "! can't be used with |"),
                ("!a -> b", "at least one tag that must be present"),
                ("!a + !b -> c", "at least one tag that must be present"),
                ("a + !a -> b", "both required and excluded"),
                ("just a term", 'write it as "tags -> result"'),
                ("a + b", 'write it as "tags -> result"'),
                ("a -> b -> c", "only one ->"),
                ("a -> b → c", "only one ->"),
                ("-> b", "nothing before ->"),
                ("a ->", "nothing after ->"),
                ("a -> ,", "nothing after ->"),
                ("a + b -> -", "needs a tag after it"),
                ("a + b -> c, +", "needs a tag after it"),
                ("a + b -> c, -c", "both added and removed"),
                ("a + b -> c, -c, d", "both added and removed"),
                ("a + b -> !c", "! only works on the left"),
                ("a+b -> c", "spaces around it"),
                ("a|b -> c", "spaces around it"),
                ("a +b -> c", "spaces around it"),
                ("a | b+c -> d", "spaces around it"),
                ("a + -> c", "spaces around it"),
                ("+ b -> c", "spaces around it"),
                ("a + _ -> b", "a tag is missing"),
                ("! -> b", "a tag is missing"),
                (" + ".join(f"t{i}" for i in range(51)) + " -> x", "at most 50 tags before"),
                ("x -> " + ", ".join(f"t{i}" for i in range(51)), "at most 50 tags after"),
        ]:
            with self.subTest(text=text):
                number, message = self.error_of("# first\n\n" + text)
                self.assertEqual(number, 3)                                           # blank and comment lines count
                self.assertIn(problem, message)

    def test_the_message_for_the_person_names_the_lines(self):
        v = at.Vocabulary("ok -> fine\na + b | c -> d\nx -> y\nnonsense\na ->")
        self.assertEqual(v.problems(), 'Line 2: use + or |, not both; Line 4: write it as "tags -> result", for example '
                                       'a + b -> c; Line 5: there is nothing after ->')
        self.assertEqual(at.Vocabulary("a + b -> c").problems(), "")
        many = at.Vocabulary("\n".join(f"bad {i}" for i in range(8))).problems()
        self.assertTrue(many.startswith("Line 1: ") and "Line 5: " in many and "Line 6" not in many)
        self.assertTrue(many.endswith("3 more lines have problems"))

    def test_reading_skips_a_bad_line_and_keeps_the_good_ones(self):
        v = at.Vocabulary("1girl -> girl\nthe lake house\na + b | c -> d\nx + y -> z")
        self.assertEqual(v.renames, {"1girl": "girl"})
        self.assertEqual([(r["add"], r["label"]) for r in v.rules], [(["z"], "line 4")])
        self.assertEqual([n for n, _ in v.errors], [2, 3])


class TestSettings(Base):
    def test_defaults_match_the_contract(self):
        s = at.load_settings()
        self.assertEqual(s, at.DEFAULTS)
        self.assertEqual((s["video_frames"], s["batch_size"], s["vram_gb"]), (6, 8, at.VRAM_GB_DEFAULT))
        self.assertEqual((s["use_wd"], s["use_pixai"], s["use_ram"], s["use_e621"]), (True,) * 4)       # the four, all on
        self.assertEqual((s["wd_strictness"], s["pixai_strictness"], s["ram_strictness"], s["e621_strictness"]),
                         (0.5,) * 4)
        self.assertEqual(s["vram_gb"], 6)                                 # four taggers: 6 GB is full speed
        self.assertEqual(at.VRAM_GB_LIMITS, (5, 8))                       # four taggers do not load under ~5 GB
        self.assertTrue(at.VRAM_GB_LIMITS[0] <= s["vram_gb"] <= at.VRAM_GB_LIMITS[1])
        self.assertEqual({k for k in s if k.startswith("use_")}, {f"use_{key}" for key in REAL})
        for gone in ("describe", "instructions", "language", "vlm_parallel"):
            self.assertNotIn(gone, s)
        self.assertEqual((s["indexing"], s["keep_updated"], s["write_tags"], s["vocabulary"]), (False, True, False, ""))
        s["blocked"].append("x")                       # callers can't change the defaults by accident
        self.assertEqual(at.load_settings()["blocked"], [])

    def test_types_are_checked_explicitly(self):
        for key, bad in [("use_wd", "false"), ("use_pixai", 0), ("use_wd", None), ("character_tags", "no"),
                         ("video_frames", "6"), ("video_frames", True), ("video_frames", 2.5), ("wd_strictness", "0.5"),
                         ("wd_strictness", True), ("wd_strictness", float("nan")), ("pixai_strictness", None),
                         ("use_ram", "true"), ("use_ram", 1), ("ram_strictness", "0.5"), ("ram_strictness", True),
                         ("ram_strictness", float("inf")),
                         ("use_e621", "true"), ("use_e621", 1), ("use_e621", None), ("e621_strictness", "0.5"),
                         ("e621_strictness", True), ("e621_strictness", None), ("e621_strictness", float("nan")),
                         ("e621_strictness", float("inf")),
                         ("vocabulary", 5), ("vocabulary", "a -> b\n" * 3000), ("vocabulary", ["a"]), ("blocked", "cat"),
                         ("blocked", [1]), ("rules", {}), ("nope", 1)]:
            with self.assertRaises(ValueError, msg=f"{key}={bad!r}"):
                at.save_settings({key: bad}, self.store)
        self.assertEqual(at.load_settings(), at.DEFAULTS)         # nothing was saved
        s = at.save_settings({"video_frames": 2.0, "wd_strictness": 0.5, "pixai_strictness": 1 - 0.1, "use_pixai": False,
                              "ram_strictness": 1 - 0.3, "use_ram": False}, self.store)
        self.assertEqual((s["video_frames"], s["wd_strictness"], s["pixai_strictness"], s["use_pixai"]), (2, 0.5, 0.9, False))
        self.assertEqual((s["ram_strictness"], s["use_ram"]), (0.7, False))
        s = at.save_settings({"e621_strictness": 1 - 0.3, "use_e621": False}, self.store)
        self.assertEqual((s["e621_strictness"], s["use_e621"]), (0.7, False))
        self.assertIsInstance(s["video_frames"], int)
        self.assertEqual(at.save_settings({"wd_strictness": 1 - 0.5}, self.store)["wd_strictness"], 0.5)

    def test_limits(self):
        for key, lo, hi in [("video_frames", 1, 8), ("batch_size", 1, 64), ("vram_gb", 5, 8),
                            ("max_tags", 5, 100), ("wd_strictness", 0.2, 0.95), ("pixai_strictness", 0.2, 0.95),
                            ("ram_strictness", 0.2, 0.95), ("e621_strictness", 0.2, 0.95)]:
            self.assertEqual(at.LIMITS[key], (lo, hi))
            for ok in (lo, hi):
                self.assertEqual(at.save_settings({key: ok}, self.store)[key], ok)
            for bad in (lo - 0.01 if isinstance(lo, float) else lo - 1, hi + 0.01 if isinstance(hi, float) else hi + 1):
                with self.assertRaises(ValueError, msg=f"{key}={bad}"):
                    at.save_settings({key: bad}, self.store)
        self.assertEqual(at.VRAM_GB_LIMITS, (5, 8))
        for key in ("wd_strictness", "e621_strictness"):                           # 0.05 and 0.1 were fine before the floor rose
            for bad in (0.05, 0.1, 0.19):
                with self.assertRaises(ValueError, msg=f"{key}={bad}"):
                    at.save_settings({key: bad}, self.store)
        self.assertNotIn("vlm_parallel", at.LIMITS)

    def test_the_floor_the_tagger_is_asked_for_is_the_lowest_a_threshold_can_be(self):
        # at 0.05 RAM++ alone sent ~4,400 tags per picture and every one was stored: nothing under 0.2 is asked for,
        # kept or listed, so no strictness can be lower than what the service sends
        self.assertEqual((at.FLOOR, at.DISPLAY_FLOOR, at.STRICTNESS), (0.2, 0.2, (0.2, 0.95)))
        self.assertGreaterEqual(at.STRICTNESS[0], at.FLOOR)
        self.assertGreaterEqual(at.STRICTNESS[0], at.DISPLAY_FLOOR)
        for kind in at.TAGGERS:
            self.assertEqual(at.LIMITS[f"{kind.key}_strictness"], at.STRICTNESS)
            self.assertGreaterEqual(at.DEFAULTS[f"{kind.key}_strictness"], at.FLOOR)

    def test_one_bad_change_saves_nothing(self):
        with self.assertRaises(ValueError):
            at.save_settings({"max_tags": 10, "video_frames": 99}, self.store)
        self.assertEqual(at.load_settings()["max_tags"], 30)
        self.assertEqual(self.store.settings_version, 1)

    def test_text_is_kept_as_typed_but_newlines_are_unix(self):
        s = at.save_settings({"vocabulary": "1girl -> woman\r\nsolo -> alone\rx -> y"}, self.store)
        self.assertEqual(s["vocabulary"], "1girl -> woman\nsolo -> alone\nx -> y")

    def test_the_version_goes_up_only_for_content_settings(self):
        self.assertEqual(self.store.settings_version, 1)
        for changes in ({"indexing": True}, {"keep_updated": False}, {"batch_size": 4},
                        {"vram_gb": at.VRAM_GB_LIMITS[1]}):
            self.assertEqual(self.set(**changes), [])
        self.assertEqual(self.store.settings_version, 1)
        self.assertEqual(self.set(max_tags=12), ["max_tags"])
        self.assertEqual(self.store.settings_version, 2)
        self.assertEqual(self.set(max_tags=12), [])                    # same value: not a change
        self.assertEqual(self.store.settings_version, 2)
        self.assertEqual(sorted(self.set(vocabulary="a -> b", wd_strictness=0.6, use_pixai=False)),
                         ["use_pixai", "vocabulary", "wd_strictness"])
        self.assertEqual(self.store.settings_version, 3)               # one bump per save
        for key, value in [("video_frames", 2), ("use_wd", False), ("use_pixai", True), ("wd_strictness", 0.7),
                           ("pixai_strictness", 0.6), ("use_ram", False), ("ram_strictness", 0.6),
                           ("use_e621", False), ("e621_strictness", 0.6), ("character_tags", False), ("rating_tag", False),
                           ("vocabulary", "a -> c"), ("blocked", ["cat"]),
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

    def test_the_vocabulary_is_checked_line_by_line_when_it_is_saved(self):
        good = ("# my tags\n1girl -> woman\nfurry + human -> human on anthro\nanthro | furry -> furry art\n\n"
                "1girl + 1boy -> couple, -solo\na + !b -> c\na -> +b")
        s = at.save_settings({"vocabulary": good + "\r\nx -> y"}, self.store)
        self.assertEqual(s["vocabulary"], good + "\nx -> y")                  # kept as typed: comments and all
        self.assertEqual(at.load_settings()["vocabulary"], s["vocabulary"])
        version = self.store.settings_version
        for bad, message in [("a + b | c -> d", "Line 1: use + or |, not both"),
                             ("ok -> fine\n\n# note\nx + -> y", "Line 4: put a tag on each side of + or |"),
                             ("a -> b\nthe lake house", 'Line 2: write it as "tags -> result"'),
                             ("a + b -> c, -c", "Line 1: a tag can't be both added and removed"),
                             ("!a -> b", "Line 1: it needs at least one tag that must be present")]:
            with self.assertRaises(ValueError, msg=bad) as ctx:
                at.save_settings({"vocabulary": bad, "max_tags": 7}, self.store)
            self.assertIn(message, str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:                                  # every bad line is named
            at.save_settings({"vocabulary": "a | b + c -> d\nfine -> ok\nnonsense\na ->"}, self.store)
        self.assertEqual(str(ctx.exception).count("Line "), 3)
        for number in ("Line 1: ", "Line 3: ", "Line 4: "):
            self.assertIn(number, str(ctx.exception))
        self.assertEqual(at.load_settings()["vocabulary"], s["vocabulary"])        # nothing was saved, not even max_tags
        self.assertEqual((at.load_settings()["max_tags"], self.store.settings_version), (30, version))

    def test_the_vocabulary_may_be_long(self):
        text = "\n".join(f"tag {i} + other {i} -> result {i}" for i in range(500))
        self.assertGreater(len(text), 4000)                                         # the old limit
        self.assertLessEqual(len(text), at.TEXT_LIMITS["vocabulary"])
        self.assertEqual(at.save_settings({"vocabulary": text}, self.store)["vocabulary"], text)
        self.assertEqual(at.TEXT_LIMITS["vocabulary"], 20000)
        with self.assertRaises(ValueError) as ctx:
            at.save_settings({"vocabulary": "# " + "x" * 20000}, self.store)
        self.assertIn("at most 20000 characters", str(ctx.exception))

    def test_a_saved_vocabulary_with_unreadable_lines_is_kept_when_loaded(self):
        # the v2 vocabulary had preferred terms on lines without an arrow: it still loads as it was (those lines are
        # skipped when tags are made); only saving it again is refused, with the line number
        at.settings_path().write_text(json.dumps({"vocabulary": "1girl -> woman\nthe lake house\na + b | c -> d"}))
        s = at.load_settings()
        self.assertEqual(s["vocabulary"], "1girl -> woman\nthe lake house\na + b | c -> d")
        with self.assertRaises(ValueError) as ctx:
            at.save_settings({"vocabulary": s["vocabulary"]}, self.store)
        self.assertIn("Line 2: ", str(ctx.exception))
        self.assertIn("Line 3: use + or |, not both", str(ctx.exception))
        self.assertEqual(at.save_settings({"max_tags": 9}, self.store)["vocabulary"], s["vocabulary"])    # other saves are fine
        at.settings_path().write_text(json.dumps({"vocabulary": 5, "max_tags": 9}))
        self.assertEqual(at.load_settings()["vocabulary"], "")                      # a wrong type is still the default

    def test_a_hand_edited_bad_value_falls_back_to_the_default(self):
        at.settings_path().write_text(json.dumps({"max_tags": 9999, "use_wd": "no", "video_frames": 3, "zzz": 1}))
        s = at.load_settings()
        self.assertEqual((s["max_tags"], s["use_wd"], s["video_frames"]), (30, True, 3))
        at.settings_path().write_text("[1, 2]")
        self.assertEqual(at.load_settings(), at.DEFAULTS)

    def test_a_settings_file_from_v1_still_loads(self):
        # written by the RAM++ version (v1): use_ram / ram_strictness are settings again (RAM++ is the third tagger), so
        # they load with their values; only the describer's keys are unknown, and its vram_gb (18-21) is out of range
        v1 = {"indexing": True, "keep_updated": False, "video_frames": 4, "batch_size": 16, "vlm_parallel": 8, "vram_gb": 20,
              "describe": True, "use_wd": False, "use_ram": False, "wd_strictness": 0.6, "ram_strictness": 0.9,
              "character_tags": False, "rating_tag": False, "max_tags": 12, "instructions": "Be brief.",
              "vocabulary": "a -> b", "blocked": ["cat"], "rules": [], "write_tags": True, "language": "English"}
        at.settings_path().write_text(json.dumps(v1))
        s = at.load_settings()
        self.assertEqual((s["use_ram"], s["ram_strictness"], s["use_wd"], s["wd_strictness"]), (False, 0.9, False, 0.6))
        self.assertEqual((s["max_tags"], s["vocabulary"], s["blocked"], s["write_tags"]), (12, "a -> b", ["cat"], True))
        self.assertEqual((s["use_pixai"], s["pixai_strictness"]), (True, 0.5))             # v1 had no PixAI: the default
        self.assertEqual((s["use_e621"], s["e621_strictness"]), (True, 0.5))               # nor Hydra: on, as new
        self.assertEqual(s["vram_gb"], at.VRAM_GB_DEFAULT)                                 # 20 is out of range now: default
        self.assertEqual(set(s), set(at.DEFAULTS))                                         # the describer keys are dropped
        for gone in ("describe", "instructions", "language", "vlm_parallel"):
            self.assertNotIn(gone, s)
        saved = at.save_settings({"max_tags": 13}, self.store)                       # saving works and cleans the file up
        self.assertEqual((saved["max_tags"], saved["use_ram"], saved["ram_strictness"]), (13, False, 0.9))
        self.assertEqual(set(json.loads(at.settings_path().read_text("utf-8"))), set(at.DEFAULTS))
        self.assertEqual((at.load_settings()["use_ram"], at.load_settings()["ram_strictness"]), (False, 0.9))   # kept
        # and a v1 file that only has RAM++ values is read as such, the rest is the default
        at.settings_path().write_text(json.dumps({"use_ram": True, "ram_strictness": 0.25}))
        only = at.load_settings()
        self.assertEqual((only["use_ram"], only["ram_strictness"]), (True, 0.25))
        self.assertEqual({k: v for k, v in only.items() if k not in ("use_ram", "ram_strictness")},
                         {k: v for k, v in at.DEFAULTS.items() if k not in ("use_ram", "ram_strictness")})
        # a v1 value that is out of range falls back to the default like any hand-edited bad value
        at.settings_path().write_text(json.dumps({"use_ram": "no", "ram_strictness": 1.5, "max_tags": 12}))
        bad = at.load_settings()
        self.assertEqual((bad["use_ram"], bad["ram_strictness"], bad["max_tags"]), (True, 0.5, 12))

    def test_a_vram_cap_below_the_new_lower_limit_falls_back_to_the_default(self):
        # v2 allowed 3-8, v3 with three taggers 4-8 (default 5); with four taggers 4 no longer loads: 5-8, default 6
        at.settings_path().write_text(json.dumps({"vram_gb": 3, "max_tags": 12}))
        s = at.load_settings()
        self.assertEqual((s["vram_gb"], s["max_tags"]), (6, 12))
        at.settings_path().write_text(json.dumps({"vram_gb": 4}))                    # a stored v3 value
        self.assertEqual(at.load_settings()["vram_gb"], 6)
        for kept in (5, 6, 8):
            at.settings_path().write_text(json.dumps({"vram_gb": kept}))
            self.assertEqual(at.load_settings()["vram_gb"], kept)
        at.settings_path().write_text(json.dumps({"vram_gb": 9}))
        self.assertEqual(at.load_settings()["vram_gb"], 6)
        for bad in (3, 4, 9):
            with self.assertRaises(ValueError, msg=str(bad)):
                at.save_settings({"vram_gb": bad}, self.store)
        self.assertEqual(at.save_settings({"vram_gb": 5}, self.store)["vram_gb"], 5)

    def test_a_threshold_below_the_new_lower_limit_falls_back_to_the_default(self):
        # the floor went from 0.05 to 0.2: a strictness saved under the old limits (0.05-0.95) below it is not valid now
        at.settings_path().write_text(json.dumps({"wd_strictness": 0.1, "pixai_strictness": 0.2, "ram_strictness": 0.05,
                                                  "e621_strictness": 0.19, "max_tags": 12}))
        s = at.load_settings()
        self.assertEqual((s["wd_strictness"], s["pixai_strictness"], s["ram_strictness"], s["e621_strictness"]),
                         (0.5, 0.2, 0.5, 0.5))
        self.assertEqual(s["max_tags"], 12)

    def test_a_settings_file_from_v2_with_the_describer_keys_still_loads(self):
        v2 = {"indexing": True, "keep_updated": False, "video_frames": 2, "batch_size": 4, "vlm_parallel": 8, "vram_gb": 6,
              "describe": True, "use_wd": True, "use_pixai": False, "wd_strictness": 0.7, "pixai_strictness": 0.4,
              "character_tags": False, "rating_tag": True, "max_tags": 12, "instructions": "Be brief.",
              "vocabulary": "1girl -> woman\nthe lake house", "blocked": ["cat"], "rules": [], "write_tags": True,
              "language": "German"}
        at.settings_path().write_text(json.dumps(v2))
        s = at.load_settings()
        self.assertEqual(set(s), set(at.DEFAULTS))                       # the four describer keys are not there
        for gone in ("describe", "instructions", "language", "vlm_parallel"):
            self.assertNotIn(gone, s)
        self.assertEqual({k: s[k] for k in s}, {**at.DEFAULTS, **{k: v for k, v in v2.items() if k in at.DEFAULTS}})
        self.assertEqual((s["use_pixai"], s["pixai_strictness"], s["vocabulary"], s["write_tags"]),
                         (False, 0.4, "1girl -> woman\nthe lake house", True))      # everything else is read as it was
        self.assertEqual((s["use_ram"], s["ram_strictness"]), (True, 0.5))           # v2 knew no RAM++: it is on, as new
        self.assertEqual((s["use_e621"], s["e621_strictness"]), (True, 0.5))         # nor Hydra
        self.assertEqual(s["vram_gb"], 6)                                              # 6 is inside 5-8: kept
        saved = at.save_settings({"max_tags": 13}, self.store)           # saving cleans the file up
        self.assertEqual(saved["max_tags"], 13)
        self.assertEqual(set(json.loads(at.settings_path().read_text("utf-8"))), set(at.DEFAULTS))
        for key, value in (("describe", False), ("instructions", "x"), ("language", "German"), ("vlm_parallel", 4)):
            with self.assertRaises(ValueError, msg=key) as err:           # sending them is refused like any unknown key
                at.save_settings({key: value}, self.store)
            self.assertIn("Unknown", str(err.exception))
            self.assertIn(key, str(err.exception))
        self.assertEqual(at.load_settings()["max_tags"], 13)

    def test_the_cheapest_reprocess_mode_for_the_changed_keys(self):
        self.assertEqual(at.suggest_mode([]), "none")
        self.assertEqual(at.suggest_mode(["max_tags", "blocked", "rules"]), "retag")
        self.assertEqual(at.suggest_mode(["vocabulary"]), "retag")                     # renames re-apply to the stored scores
        self.assertEqual(at.suggest_mode(["rules", "video_frames"]), "full")
        self.assertEqual(at.suggest_mode(["use_pixai"]), "full")
        self.assertEqual(at.suggest_mode(["pixai_strictness"]), "retag")
        self.assertEqual(at.suggest_mode(["use_ram"]), "full")                          # which taggers run changes the scores
        self.assertEqual(at.suggest_mode(["ram_strictness"]), "retag")                  # a threshold re-applies to stored ones
        self.assertEqual(at.suggest_mode(["use_e621"]), "full")
        self.assertEqual(at.suggest_mode(["e621_strictness"]), "retag")
        self.assertEqual(at.suggest_mode(["batch_size"]), "none")
        self.assertEqual(set(at.REPROCESS), {"retag", "full"})                           # no describe mode any more
        every = {k for keys in at.REPROCESS.values() for k in keys}
        self.assertEqual(every, set(at.CONTENT))            # every content setting has a mode

    def test_describe_counts_as_retag(self):
        self.assertEqual(at.MODES, ("retag", "full"))
        self.assertEqual([at.normalize_mode(m) for m in ("retag", "full", "describe")], ["retag", "full", "retag"])
        for bad in ("", "everything", None, 3, ["retag"], "Retag"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                at.normalize_mode(bad)

    def test_write_tags_is_remembered_so_the_native_tags_stay_in_step(self):
        self.assertEqual(self.store.meta("native_tags"), "")
        self.set(write_tags=True)
        self.set(write_tags=False)
        self.assertEqual(self.store.meta("native_tags"), "1")


class TestTaggerRegistry(Base):
    """The registry ``TAGGERS``: settings, limits and modes are generated from it, nothing else names a tagger."""

    def test_wd_pixai_ram_and_e621_are_registered(self):
        self.assertEqual([k.key for k in at.TAGGERS], REAL)
        wd, pixai, ram, e621 = at.TAGGERS
        self.assertEqual((wd.label, wd.categories, wd.character_categories, wd.has_rating, wd.default_on, wd.noise),
                         ("wd-eva02-large-tagger-v3", ("general", "character"), ("character",), True, True, frozenset()))
        self.assertEqual((pixai.label, pixai.categories, pixai.character_categories, pixai.has_rating, pixai.default_on,
                          pixai.noise),
                         ("pixai-tagger-v1.0", ("general", "character", "copyright"), ("character", "copyright"), True, True,
                          frozenset()))
        # RAM++: plain-English tags only: no characters or series, no rating; on by default; a few words that say nothing
        self.assertEqual((ram.label, ram.categories, ram.character_categories, ram.has_rating, ram.default_on),
                         ("RAM++ (swin-large)", ("general",), (), False, True))
        self.assertEqual(ram.noise, frozenset({"image", "catch", "peak", "miss", "take", "wear", "label"}))
        # Hydra 3.5, the e621 vocabulary: four categories (species is its own), the series and characters are the ones
        # `character_tags` switches off; no rating; on by default; "mammal" (it lands on every human) is noise
        self.assertEqual((e621.label, e621.categories, e621.character_categories, e621.has_rating, e621.default_on,
                          e621.noise),
                         ("Hydra 3.5 (e621)", ("general", "species", "character", "copyright"), ("character", "copyright"),
                          False, True, frozenset({"mammal"})))
        self.assertEqual(at.model_labels(), {"wd": "wd-eva02-large-tagger-v3", "pixai": "pixai-tagger-v1.0",
                                             "ram": "RAM++ (swin-large)", "e621": "Hydra 3.5 (e621)"})
        self.assertEqual(list(at.model_labels()), REAL)                                  # in registry order
        self.assertEqual([k.key for k in at.enabled_kinds(at.DEFAULTS)], REAL)
        self.assertEqual([k.key for k in at.enabled_kinds({**at.DEFAULTS, "use_pixai": False})], ["wd", "ram", "e621"])
        self.assertEqual([k.key for k in at.enabled_kinds({**at.DEFAULTS, "use_e621": False})], ["wd", "pixai", "ram"])
        self.assertEqual([k.key for k in at.enabled_kinds({**at.DEFAULTS, "use_wd": False, "use_ram": False})],
                         ["pixai", "e621"])
        self.assertEqual(at.enabled_kinds({k: False if k.startswith("use_") else v for k, v in at.DEFAULTS.items()}), [])

    def test_only_ram_and_e621_have_noise_words_and_they_are_normalised(self):
        for kind in at.TAGGERS:
            for word in kind.noise:
                self.assertEqual(at.norm_tag(word), word, f"{kind.key}: noise words are compared after normalising")
        self.assertEqual([k.key for k in at.TAGGERS if k.noise], ["ram", "e621"])

    def test_only_the_taggers_that_name_a_rating_have_one(self):
        self.assertEqual([k.key for k in at.TAGGERS if k.has_rating], ["wd", "pixai"])      # ram and e621 never take part

    def test_only_hydra_has_a_species_category_and_only_the_two_character_ones_are_switched_off(self):
        self.assertEqual([k.key for k in at.TAGGERS if "species" in k.categories], ["e621"])
        self.assertNotIn("species", [c for k in at.TAGGERS for c in k.character_categories])
        self.assertEqual({k.key: k.character_categories for k in at.TAGGERS},
                         {"wd": ("character",), "pixai": ("character", "copyright"), "ram": (),
                          "e621": ("character", "copyright")})

    def test_every_tagger_has_generated_settings_limits_and_modes(self):
        for kind in at.TAGGERS:
            use, strict = f"use_{kind.key}", f"{kind.key}_strictness"
            self.assertIs(at.DEFAULTS[use], kind.default_on)
            self.assertEqual(at.DEFAULTS[strict], 0.5)
            self.assertEqual(at.LIMITS[strict], (0.2, 0.95))
            self.assertNotIn(use, at.LIMITS)
            self.assertIn(use, at.CONTENT)
            self.assertIn(strict, at.CONTENT)
            self.assertIn(use, at.REPROCESS["full"])                  # which taggers run changes the scores: full
            self.assertIn(strict, at.REPROCESS["retag"])              # a threshold re-applies to the stored scores: retag
            self.assertNotIn(use, at.REPROCESS["retag"])
            self.assertNotIn(strict, at.REPROCESS["full"])
        self.assertEqual(at.suggest_mode(["use_wd", "wd_strictness"]), "full")

    def test_a_fifth_tagger_gets_its_settings_without_any_other_change(self):
        with_extra(self)
        self.assertEqual([k.key for k in at.TAGGERS], [*REAL, "extra"])
        self.assertEqual((at.DEFAULTS["use_extra"], at.DEFAULTS["extra_strictness"]), (False, 0.5))      # off by default
        self.assertEqual(at.LIMITS["extra_strictness"], (0.2, 0.95))
        self.assertIn("use_extra", at.CONTENT)
        self.assertIn("extra_strictness", at.CONTENT)
        self.assertIn("use_extra", at.REPROCESS["full"])
        self.assertIn("extra_strictness", at.REPROCESS["retag"])
        self.assertEqual(at.model_labels()["extra"], "extra-tagger-test")
        self.assertEqual(list(at.model_labels()), [*REAL, "extra"])
        s = at.load_settings()
        self.assertEqual((s["use_extra"], s["extra_strictness"]), (False, 0.5))
        self.assertEqual(set(s), set(at.DEFAULTS))
        # validation is the same as for the others
        for key, bad in [("use_extra", "yes"), ("use_extra", 1), ("extra_strictness", "0.5"), ("extra_strictness", True),
                         ("extra_strictness", 0.19), ("extra_strictness", 0.96), ("extra_strictness", float("inf"))]:
            with self.assertRaises(ValueError, msg=f"{key}={bad!r}"):
                at.save_settings({key: bad}, self.store)
        self.assertEqual(self.set(use_extra=True, extra_strictness=0.7), ["use_extra", "extra_strictness"])
        self.assertEqual(at.suggest_mode(["use_extra"]), "full")
        self.assertEqual(at.suggest_mode(["extra_strictness"]), "retag")
        saved = at.load_settings()
        self.assertEqual((saved["use_extra"], saved["extra_strictness"]), (True, 0.7))
        self.assertEqual([k.key for k in at.enabled_kinds(saved)], [*REAL, "extra"])
        self.assertEqual([k.key for k in at.enabled_kinds({**saved, "use_pixai": False})], ["wd", "ram", "e621", "extra"])
        self.assertEqual([k.key for k in at.enabled_kinds({**saved, "use_e621": False})], ["wd", "pixai", "ram", "extra"])
        every = {k for keys in at.REPROCESS.values() for k in keys}
        self.assertEqual(every, set(at.CONTENT))

    def test_a_fifth_tagger_can_be_added_and_removed_and_the_registry_is_restored(self):
        saved = list(at.TAGGERS)
        registry = at.TAGGERS                                      # the list itself: what other code refers to
        defaults, limits, content = dict(at.DEFAULTS), dict(at.LIMITS), at.CONTENT
        reprocess = {mode: tuple(keys) for mode, keys in at.REPROCESS.items()}
        at.configure_taggers([*saved, EXTRA])                      # added ...
        self.assertIs(at.TAGGERS, registry)
        self.assertEqual([k.key for k in at.TAGGERS], [*REAL, "extra"])
        self.assertEqual(set(at.DEFAULTS) - set(defaults), {"use_extra", "extra_strictness"})
        self.assertEqual(set(at.LIMITS) - set(limits), {"extra_strictness"})
        self.assertEqual(set(at.CONTENT) - set(content), {"use_extra", "extra_strictness"})
        at.configure_taggers(saved)                                # ... and removed
        self.assertIs(at.TAGGERS, registry)
        self.assertEqual(at.TAGGERS, saved)
        self.assertEqual((dict(at.DEFAULTS), dict(at.LIMITS), at.CONTENT), (defaults, limits, content))
        self.assertEqual({mode: tuple(keys) for mode, keys in at.REPROCESS.items()}, reprocess)
        self.assertNotIn("extra_strictness", at.LIMITS)
        with self.assertRaises(ValueError):
            at.save_settings({"use_extra": True}, self.store)
        self.assertIn("use_e621", at.DEFAULTS)                     # the real four were never touched
        # rebuilding the tables without changing the registry changes nothing either
        at.configure_taggers()
        self.assertEqual((at.TAGGERS, dict(at.DEFAULTS), at.CONTENT), (saved, defaults, content))
        # settings saved for a tagger that is removed again are simply not loaded any more
        at.configure_taggers([*saved, EXTRA])
        at.save_settings({"use_extra": True, "extra_strictness": 0.9}, self.store)
        at.configure_taggers(saved)
        loaded = at.load_settings()
        self.assertEqual(loaded, at.DEFAULTS)
        self.assertNotIn("use_extra", loaded)

    def test_a_tagger_can_be_taken_out_of_the_registry_too(self):
        saved = list(at.TAGGERS)
        self.addCleanup(at.configure_taggers, saved)
        at.configure_taggers(saved[:3])                            # no Hydra
        self.assertEqual([k.key for k in at.TAGGERS], ["wd", "pixai", "ram"])
        self.assertNotIn("use_e621", at.DEFAULTS)
        self.assertNotIn("e621_strictness", at.CONTENT)
        self.assertNotIn("e621_strictness", at.LIMITS)
        self.assertEqual(list(at.model_labels()), ["wd", "pixai", "ram"])
        with self.assertRaises(ValueError):
            at.save_settings({"use_e621": False}, self.store)
        at.configure_taggers(saved[:2])                            # no RAM++ either
        self.assertEqual([k.key for k in at.TAGGERS], ["wd", "pixai"])
        self.assertNotIn("use_ram", at.DEFAULTS)
        self.assertNotIn("ram_strictness", at.CONTENT)
        self.assertEqual(list(at.model_labels()), ["wd", "pixai"])
        with self.assertRaises(ValueError):
            at.save_settings({"use_ram": False}, self.store)

    def test_a_tagger_entry_is_checked(self):
        for key in ("", "Wd", "my_tagger", "1a", "a b", "a-b"):
            with self.assertRaises(ValueError, msg=key):
                at.TaggerKind(key, "x", ("general",))
        with self.assertRaises(ValueError):
            at.TaggerKind("x", "x", ("general",), ("character",))               # character_categories not in categories
        with self.assertRaises(ValueError):
            at.TaggerKind("x", "x", ())
        saved = list(at.TAGGERS)
        with self.assertRaises(ValueError):
            at.configure_taggers([*saved, at.TAGGERS[0]])                         # the same key twice
        self.assertEqual(at.TAGGERS, saved)                                       # a refused registry changes nothing
        self.assertIn("use_wd", at.DEFAULTS)


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
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.2, "explicit": 0.8}, "pixai": {"general": 0.6, "explicit": 0.4},
                                           "ram": None, "e621": None}])

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
        self.assertEqual(set(shown), {"wd", "pixai", "ram", "e621", "rating"})  # RAM++ and Hydra have a list too: PHOTO has none
        self.assertEqual((shown["ram"], shown["e621"]), ([], []))
        self.assertEqual(shown["rating"]["general"], 0.9)

    def test_scores_stored_under_the_old_floor_are_never_kept_or_listed(self):
        # a store made when the floor was 0.05 holds scores down there (RAM++ alone sent ~4,400 tags per picture): with
        # the strictness at its lowest, 0.2, nothing below 0.2 is kept, and the Test card lists from 0.2 up
        raw = raw_of("wd:a=0.19,b=0.9,c=0.05|pixai:d=0.1|ram:e=0.05,f=0.15,g=0.2|e621:h=0.19,i=0.2|e6species:j=0.04|"
                     "rating:general=0.9")
        lowest = {f"{kind.key}_strictness": at.STRICTNESS[0] for kind in at.TAGGERS}
        self.assertEqual(set(lowest.values()), {0.2})
        found = at.detect(raw, S(rating_tag=False, **lowest))
        self.assertEqual(set(found["tags"]), {"b", "g", "i"})                  # g and i are exactly 0.2: kept
        listed = {key: {t["tag"] for t in found["display"][key]} for key in REAL}
        self.assertEqual(listed, {"wd": {"b"}, "pixai": set(), "ram": {"g"}, "e621": {"i"}})

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

    def test_a_rule_with_a_label_is_named_by_it_in_the_trace(self):
        r = [rule(if_all=["a"], add=["b"]), {**rule(if_all=["b"], add=["c"]), "label": "line 7"}]
        _, fired, _ = self.run_rules(r, "a")
        self.assertEqual(fired, [{"rule": 0, "added": ["b"], "removed": []}, {"rule": "line 7", "added": ["c"], "removed": []}])


# ---------------------------------------------------------------- combinations typed in the vocabulary

class TestVocabularyCombinations(unittest.TestCase):
    """``a + b -> c`` lines make rules that run after the Rules card's, in the order written, in the same engine."""

    def names(self, text, **settings):
        return tags_of(combos(text, **settings))

    def test_and(self):
        self.assertIn("summer", self.names("girl + beach -> summer"))
        self.assertNotIn("summer", self.names("girl + dog -> summer"))                   # one of the two is missing
        self.assertNotIn("summer", self.names("dog + girl -> summer"))

    def test_or(self):
        self.assertIn("shore", self.names("dog | beach -> shore"))
        self.assertIn("shore", self.names("beach | sea -> shore"))
        self.assertNotIn("shore", self.names("dog | cat -> shore"))

    def test_not(self):
        self.assertIn("day", self.names("girl + !night -> day"))
        self.assertNotIn("day", self.names("girl + !beach -> day"))                      # beach is there
        self.assertNotIn("day", self.names("girl + !beach + !night -> day"))
        self.assertIn("day", self.names("!night + girl + !dog -> day"))

    def test_add_and_remove(self):
        got = self.names("girl + solo -> couple, -solo, +duo")
        self.assertTrue({"couple", "duo"} <= set(got) and "solo" not in got)
        self.assertNotIn("solo", self.names("girl -> -solo"))
        self.assertIn("girl", self.names("girl -> -solo"))

    def test_a_rule_adds_with_score_1_and_the_source_rule(self):
        by = {t["tag"]: t for t in combos("girl + beach -> summer")["tags"]}
        self.assertEqual(by["summer"], {"tag": "summer", "score": 1.0, "source": "rule"})

    def test_a_plus_before_the_target_keeps_the_tag_and_a_plain_arrow_renames(self):
        kept = self.names("girl -> +woman")
        self.assertTrue({"girl", "woman"} <= set(kept))
        renamed = self.names("girl -> woman")
        self.assertIn("woman", renamed)
        self.assertNotIn("girl", renamed)

    def test_renames_happen_before_the_rules_see_the_tags(self):
        pic = "wd:1girl=0.9,beach=0.8|rating:general=0.9"
        got = combos("1girl -> girl\ngirl + beach -> summer\n1girl + beach -> never", picture=pic)
        names = tags_of(got)
        self.assertIn("summer", names)                         # the rule saw the renamed tag
        self.assertNotIn("never", names)                       # ... and not the name it had before
        self.assertNotIn("1girl", names)
        self.assertEqual(got["rules"], [{"rule": "line 2", "added": ["summer"], "removed": []}])
        # a combination can name a tag that only a rename produces, whatever the order of the lines
        self.assertIn("summer", tags_of(combos("girl + beach -> summer\n1girl -> girl", picture=pic)))

    def test_a_combination_runs_on_the_tags_before_the_cap_and_blocked_still_wins(self):
        got = combos("girl -> +cat, +dog", blocked=["cat"])
        self.assertIn("dog", tags_of(got))
        self.assertNotIn("cat", tags_of(got))                                            # added by the line, blocked afterwards
        self.assertEqual(got["rules"], [{"rule": "line 1", "added": ["cat", "dog"], "removed": []}])
        got = combos("girl -> +zzz", max_tags=5)                                           # 5 = 4 tags + the rating
        self.assertEqual(len(got["tags"]), 5)
        self.assertEqual(tags_of(got)[-1], "rating: general")
        self.assertIn("zzz", tags_of(got))                                                # its score 1.0 is the best

    def test_the_text_rules_run_after_the_card_rules(self):
        # the card's rule takes `girl` away first, so the line that needs it never fires
        card = [rule(if_all=["girl"], remove=["girl"], add=["woman"])]
        got = combos("girl -> +never", rules=card)
        self.assertNotIn("never", tags_of(got))
        self.assertEqual(got["rules"], [{"rule": 0, "added": ["woman"], "removed": ["girl"]}])
        self.assertIn("never", tags_of(combos("girl -> +never")))                       # without the card rule it does fire
        # ... and the line sees what the card's rule added, in the same pass
        got = combos("happy + beach -> party", rules=[rule(if_all=["girl"], add=["happy"])])
        self.assertIn("party", tags_of(got))
        self.assertEqual([r["rule"] for r in got["rules"]], [0, "line 1"])               # card first, then the line

    def test_the_text_rules_run_in_the_order_written(self):
        got = combos("# one\ngirl -> +a\n\nbeach -> +b\nsolo -> -hat, +c")
        self.assertEqual([r["rule"] for r in got["rules"]], ["line 2", "line 4", "line 5"])
        # order changes the outcome when rules disagree: the first line removes what the second needs
        gone = combos("girl -> -beach\nbeach -> +never")
        self.assertNotIn("never", tags_of(gone))
        there = combos("beach -> +never\ngirl -> -beach")
        self.assertIn("never", tags_of(there))

    def test_rules_repeat_until_nothing_changes_across_cards_and_text(self):
        # written backwards, so each line needs the next pass; the card's rule needs what the lines add
        got = combos("z -> +end\ny -> +z\ngirl -> +y", rules=[rule(if_all=["end"], add=["done"])])
        names = tags_of(got)
        self.assertTrue({"y", "z", "end", "done"} <= set(names))
        self.assertEqual([r["rule"] for r in got["rules"]], [0, "line 1", "line 2", "line 3"])
        # a loop of lines that undo each other still stops (the same five passes as the card's rules)
        flip = combos("girl -> +b, -girl\nb -> +girl, -b")
        self.assertTrue(flip["rules"])
        self.assertEqual(len([n for n in tags_of(flip) if n in ("girl", "b")]), 1)

    def test_a_line_that_changes_nothing_is_not_reported(self):
        self.assertEqual(combos("girl -> +girl\nbeach -> -nothing here")["rules"], [])

    def test_the_trace_names_a_typed_rule_by_its_line_and_a_card_rule_by_its_number(self):
        got = combos("# note\n\ngirl + beach -> summer\ndog | cat -> pet", rules=[rule(if_all=["girl"], add=["happy"])])
        self.assertEqual(got["rules"], [{"rule": 0, "added": ["happy"], "removed": []},
                                        {"rule": "line 3", "added": ["summer"], "removed": []}])    # the dog line never fired

    def test_a_line_that_cannot_be_read_is_skipped_when_tags_are_made(self):
        got = combos("the lake house\ngirl + beach | sea -> nope\ngirl + beach -> summer")        # as a v2 vocabulary may have
        self.assertIn("summer", tags_of(got))
        self.assertNotIn("nope", tags_of(got))
        self.assertEqual(got["rules"], [{"rule": "line 3", "added": ["summer"], "removed": []}])

    def test_no_vocabulary_changes_nothing(self):
        self.assertEqual(tags_of(combos("")), tags_of(at.build(raw_of(PHOTO), S())))
        self.assertEqual(combos("# only a note\n\n")["rules"], [])


# ---------------------------------------------------------------- the explicit-tag check, the final list

class TestSexualTagsNeedAgreement(unittest.TestCase):
    """A sexual tag on a picture rated general/sensitive is kept only when at least two enabled taggers found it."""
    EVERYDAY = "wd:door=0.97,oral=0.96,loli=0.74,shirt=0.6|pixai:door=0.93,shirt=0.94,indoors=0.96|rating:general=0.9"

    def tags(self, *pictures, **settings):
        return at.detect(raw_of(*pictures), S(**settings))

    def test_one_tagger_alone_is_not_enough_on_a_general_picture(self):
        got = self.tags(self.EVERYDAY)
        self.assertNotIn("oral", got["tags"])
        self.assertNotIn("loli", got["tags"])
        self.assertIn("door", got["tags"])
        self.assertEqual(got["display"]["dropped"], ["loli", "oral"])

    def test_both_taggers_agreeing_keeps_it(self):
        got = self.tags("wd:nude=0.9,beach=0.8|pixai:nude=0.8,beach=0.9|rating:sensitive=0.7,general=0.3")
        self.assertIn("nude", got["tags"])
        self.assertNotIn("dropped", got["display"])

    def test_an_explicit_rating_keeps_everything(self):
        got = self.tags("wd:oral=0.96,penis=0.9|pixai:penis=0.95|rating:explicit=0.95,general=0.05")
        self.assertIn("oral", got["tags"])                  # only WD, but the picture is rated explicit

    def test_with_one_tagger_switched_off_a_general_picture_drops_them(self):
        got = self.tags(self.EVERYDAY, use_pixai=False)
        self.assertNotIn("oral", got["tags"])
        self.assertIn("door", got["tags"])
        only_pixai = self.tags("wd:door=0.9|pixai:oral=0.9,door=0.9|prating:general=0.9", use_wd=False)
        self.assertNotIn("oral", only_pixai["tags"])        # a single tagger can never confirm itself
        self.assertEqual(only_pixai["display"]["dropped"], ["oral"])

    def test_a_single_tagger_on_drops_it_even_when_it_is_the_only_one_that_saw_it_in_two_captures(self):
        got = self.tags("wd:oral=0.96|pixai:a=0.6|rating:general=0.9", "wd:oral=0.96|pixai:a=0.6|rating:general=0.9",
                        use_pixai=False)
        self.assertNotIn("oral", got["tags"])

    def test_without_a_rating_nothing_is_dropped(self):
        got = self.tags("wd:oral=0.96|pixai:door=0.9")
        self.assertIn("oral", got["tags"])

    def test_the_check_runs_on_renamed_tags_too(self):
        got = self.tags(self.EVERYDAY, vocabulary="oral -> mouth stuff")
        self.assertNotIn("mouth stuff", got["tags"])

    def test_hydra_votes_like_any_other_tagger(self):
        # WD and Hydra agree (PixAI does not name it): kept
        got = self.tags("wd:door=0.9,oral=0.9|pixai:door=0.9|e621:oral=0.8,door=0.9|rating:general=0.9")
        self.assertIn("oral", got["tags"])
        self.assertNotIn("dropped", got["display"])
        # Hydra alone is not enough, and neither is anyone else alone when Hydra is on
        got = self.tags("wd:door=0.9|pixai:door=0.9|e621:oral=0.9,door=0.9|rating:general=0.9")
        self.assertNotIn("oral", got["tags"])
        self.assertEqual(got["display"]["dropped"], ["oral"])
        got = self.tags("wd:door=0.9|pixai:oral=0.9,door=0.9|e621:door=0.9|rating:general=0.9")
        self.assertNotIn("oral", got["tags"])
        # PixAI and Hydra agree without WD; Hydra counts only while it is on
        picture = "wd:door=0.9|pixai:oral=0.9,door=0.9|e621:oral=0.8|rating:general=0.9"
        self.assertIn("oral", self.tags(picture)["tags"])
        self.assertNotIn("oral", self.tags(picture, use_e621=False)["tags"])
        # an explicit rating still keeps everything
        self.assertIn("oral", self.tags("wd:oral=0.9|e621:door=0.9|rating:explicit=0.9")["tags"])

    def test_hydra_confirms_by_its_own_strictness_and_with_the_categories_that_are_on(self):
        picture = "wd:oral=0.9,door=0.9|pixai:door=0.9|e621:oral=0.3,door=0.9|rating:general=0.9"
        self.assertNotIn("oral", self.tags(picture)["tags"])                       # 0.3 is below Hydra's 0.5
        self.assertIn("oral", self.tags(picture, e621_strictness=0.25)["tags"])
        # a name in Hydra's character category votes only while character tags are on
        picture = "wd:oral=0.9,door=0.9|pixai:door=0.9|e6char:oral=0.9|rating:general=0.9"
        self.assertIn("oral", self.tags(picture)["tags"])
        self.assertNotIn("oral", self.tags(picture, character_tags=False)["tags"])

    def test_a_rename_lets_two_vocabularies_agree_on_the_same_tag(self):
        # WD says "fellatio", Hydra says "oral sex": two names, one vote each, and "oral sex" is not on the explicit list
        picture = "wd:door=0.9,fellatio=0.9|pixai:door=0.9|e621:oral sex=0.8,door=0.9|rating:general=0.9"
        got = self.tags(picture)
        self.assertNotIn("fellatio", got["tags"])
        self.assertIn("oral sex", got["tags"])
        got = self.tags(picture, vocabulary="oral sex -> fellatio")                # now both say fellatio: two votes
        self.assertEqual(got["tags"]["fellatio"], (0.9, "wd"))
        self.assertNotIn("dropped", got["display"])


V1_RAM = {"beach": 0.8, "sea": 0.6, "wave": 0.3, "image": 0.9}       # what the v1 service stored for RAM++: flat


def v1_raw() -> dict:
    """Stored scores as the RAM++ version (v1) left them: WD nested as ever, RAM++ flat (no category level), WD's rating
    flat (not under a tagger's key), no PixAI entry."""
    return {"captures": 1, "scores": [{"wd": {"general": {"girl": 0.9}, "character": {}}, "ram": dict(V1_RAM)}],
            "ratings": [{"general": 0.9, "sensitive": 0.08}], "models": ["ram", "wd"]}


def v1_as_v3(raw: dict) -> dict:
    """The same scores in today's shape: RAM++ under ``general``, WD's rating under ``wd``."""
    return {**raw, "scores": [{**cap, "ram": {"general": cap["ram"]}} for cap in raw["scores"]],
            "ratings": [{"wd": r} for r in raw["ratings"]]}


def subsets(keys):
    return [c for size in range(len(keys) + 1) for c in itertools.combinations(keys, size)]


class TestRamPlusPlus(unittest.TestCase):
    """The real third tagger, RAM++ (key ``ram``): plain-English tags, no characters, no rating, a few noise words."""

    def kept(self, *pictures, **settings):
        return {t: v for t, v in at.detect(raw_of(*pictures), S(rating_tag=False, **settings))["tags"].items()}

    def test_its_tags_pass_its_own_strictness_and_say_ram(self):
        got = self.kept(RAM_PHOTO, use_wd=False, use_pixai=False)
        self.assertEqual(got, {"sea": (0.95, "ram"), "lake": (0.9, "ram"), "screenshot": (0.6, "ram")})
        self.assertEqual(self.kept(RAM_PHOTO, use_wd=False, use_pixai=False, ram_strictness=0.25)["wave"], (0.3, "ram"))
        self.assertEqual(set(self.kept(RAM_PHOTO, use_wd=False, use_pixai=False, ram_strictness=0.92)), {"sea"})
        # the other taggers' thresholds are not its own: its 0.3 stays below 0.5 while WD's 0.3 and PixAI's 0.4 pass 0.25
        got = self.kept(RAM_PHOTO, wd_strictness=0.25, pixai_strictness=0.25)
        self.assertEqual((got["hat"], got["wave"]), ((0.3, "wd"), (0.4, "pixai")))
        self.assertEqual(self.kept(RAM_PHOTO, ram_strictness=0.95).keys() & {"lake", "screenshot"}, set())
        self.assertEqual(self.kept(RAM_PHOTO, ram_strictness=0.95)["sea"], (0.95, "ram"))

    def test_it_can_be_switched_off_and_then_says_nothing(self):
        got = at.detect(raw_of(RAM_PHOTO), S(use_ram=False))
        self.assertEqual(got["display"]["ram"], [])
        self.assertTrue(all(source != "ram" for _score, source in got["tags"].values()))
        self.assertNotIn("lake", got["tags"])
        self.assertIn("lake", at.detect(raw_of(RAM_PHOTO), S())["tags"])

    def test_it_combines_the_captures_like_the_others(self):
        raw = raw_of("ram:cat=0.9,dog=0.6", "ram:cat=0.2", "ram:cat=0.9")
        found = at.detect(raw, S(rating_tag=False, use_wd=False, use_pixai=False))
        self.assertAlmostEqual(found["tags"]["cat"][0], 0.9)                  # median .9, best .9
        self.assertEqual(set(found["tags"]), {"cat"})                         # "dog" in one capture only: (0 + .6) / 2 = .3
        self.assertEqual({t["tag"]: (t["score"], t["kept"]) for t in found["display"]["ram"]},
                         {"cat": (0.9, True), "dog": (0.3, False)})
        raw = at.raw_from_results([read_picture(b"ram:cat=0.9")[0], None], [None, "cannot identify image file"])
        self.assertEqual(raw["captures"], 1)                                   # a capture it could not read is no vote

    def test_character_tags_do_not_touch_it_because_it_has_no_characters(self):
        picture = "wd:girl=0.9|char:miku=0.9|pixai:smile=0.7|pchar:rin=0.9|copy:vocaloid=0.9|ram:sea=0.9,screenshot=0.8"
        everything = self.kept(picture)
        self.assertEqual(everything.keys(), {"girl", "miku", "smile", "rin", "vocaloid", "sea", "screenshot"})
        self.assertEqual(self.kept(picture, character_tags=False).keys(), {"girl", "smile", "sea", "screenshot"})

    def test_it_reports_no_rating_so_it_never_takes_part_in_it(self):
        # general: wd .9, pixai .3 -> .6; explicit: wd .1, pixai .7 -> .4 (RAM++ has no rating to add)
        picture = "wd:a=0.9|rating:general=0.9,explicit=0.1|prating:general=0.3,explicit=0.7|ram:b=0.9"
        got = at.detect(raw_of(picture), S())
        self.assertEqual(got["display"]["rating"], {"general": 0.6, "explicit": 0.4})
        self.assertEqual((got["tags"]["rating: general"][1], round(got["tags"]["rating: general"][0], 3)), ("wd", 0.6))
        self.assertEqual(at.detect(raw_of(picture), S(use_ram=False))["display"]["rating"], got["display"]["rating"])
        # a rating stored under "ram" (nothing writes one) is not read
        raw = raw_of(picture)
        raw["ratings"][0]["ram"] = {"general": 0.0, "explicit": 1.0}
        self.assertEqual(at.detect(raw, S())["display"]["rating"], {"general": 0.6, "explicit": 0.4})
        # and a rating in the service's answer for it is not stored
        stored = at.raw_from_results([{"wd": {"general": {"a": 0.9}, "character": {}, "rating": {"general": 0.8}},
                                       "ram": {"general": {"b": 0.9}, "rating": {"explicit": 0.99}}}], [None])
        self.assertEqual(stored["ratings"], [{"wd": {"general": 0.8}, "pixai": None, "ram": None, "e621": None}])
        self.assertEqual(stored["scores"][0]["ram"], {"general": {"b": 0.9}})
        # with RAM++ the only tagger there is no rating at all: no rating tag, nothing on the Test card
        alone = at.detect(raw_of(picture), S(use_wd=False, use_pixai=False))
        self.assertEqual(alone["display"]["rating"], {})
        self.assertEqual(alone["tags"], {"b": (0.9, "ram")})
        # RAM++ does not dilute the mean of the two that have one: each averaged over the captures, then the two
        two = ("wd:a=0.9|rating:general=0.8|prating:general=0.2|ram:b=0.9", "wd:a=0.9|rating:general=0.6|prating:general=0.4|ram:b=0.9")
        self.assertAlmostEqual(at.detect(raw_of(*two), S())["display"]["rating"]["general"], 0.5, places=3)
        # a tie still goes to the earlier tagger, not to RAM++
        tie = at.detect(raw_of("wd:a=0.9|rating:general=0.6,sensitive=0.4|prating:general=0.6,sensitive=0.4|ram:b=0.9"), S())
        self.assertEqual(tie["tags"]["rating: general"], (0.6, "wd"))

    def test_the_highest_score_wins_and_ties_go_to_the_earlier_tagger(self):
        got = self.kept("wd:smile=0.7,a=0.6|pixai:smile=0.8|ram:smile=0.9,a=0.8|rating:general=0.9")
        self.assertEqual((got["smile"], got["a"]), ((0.9, "ram"), (0.8, "ram")))
        got = self.kept("wd:smile=0.95|pixai:smile=0.8|ram:smile=0.9|rating:general=0.9")
        self.assertEqual(got["smile"], (0.95, "wd"))
        self.assertEqual(self.kept("wd:smile=0.9|ram:smile=0.9")["smile"], (0.9, "wd"))
        self.assertEqual(self.kept("pixai:smile=0.9|ram:smile=0.9")["smile"], (0.9, "pixai"))
        self.assertEqual(self.kept("wd:woman=0.9|ram:woman=0.9|rating:general=0.9", vocabulary="woman -> lady")["lady"],
                         (0.9, "wd"))                                          # a rename applies to its tags too

    def test_its_names_are_normalised_like_the_others(self):
        # RAM++ writes some names with capitals and spaces (``3D CG rendering``)
        got = self.kept("ram:3D_CG_rendering=0.9,Birthday Cake=0.8", use_wd=False, use_pixai=False)
        self.assertEqual(got, {"3d cg rendering": (0.9, "ram"), "birthday cake": (0.8, "ram")})

    # ---- noise: the words RAM++ says that describe nothing (``image``, ``catch`` ...) never get anywhere
    def test_noise_words_never_reach_the_tags_the_block_or_the_preview_lists(self):
        ram = next(k for k in at.TAGGERS if k.key == "ram")
        self.assertTrue(ram.noise)
        words = sorted(ram.noise)
        picture = "ram:" + ",".join(f"{w}=0.95" for w in words) + ",sea=0.9,Image=0.9|rating:general=0.9"
        for strictness in (0.5, at.STRICTNESS[0]):                             # not a matter of the threshold
            built = at.build(raw_of(picture), S(ram_strictness=strictness))
            names = tags_of(built)
            self.assertEqual(names, ["sea", "rating: general"], strictness)
            self.assertEqual(built["block"], "[AI Tagger]\nTags: sea, rating: general\n[/AI Tagger]")
            listed = {t["tag"] for t in built["models"]["ram"]}
            self.assertEqual(listed, {"sea"}, strictness)                     # no noise word in the Test card's list
            for word in words:
                self.assertNotIn(word, names)
                self.assertNotIn(word, listed)
        # shown from 0.2 up in the list, but a noise word at 0.3 is not shown either
        listed = {t["tag"] for t in at.detect(raw_of("ram:image=0.3,sea=0.3|rating:general=0.9"), S())["display"]["ram"]}
        self.assertEqual(listed, {"sea"})

    def test_noise_is_only_rams_and_only_the_exact_words(self):
        # the same word from WD or PixAI is a tag like any other; RAM++'s other words are kept
        got = self.kept("wd:image=0.9,take=0.9|pixai:label=0.9|ram:image=0.9,take=0.9,label=0.9,stand=0.9,sit=0.9,images=0.9")
        self.assertEqual({t: source for t, (_s, source) in got.items()},
                         {"image": "wd", "take": "wd", "label": "pixai", "stand": "ram", "sit": "ram", "images": "ram"})

    def test_noise_does_not_count_as_a_vote_for_the_explicit_tag_check(self):
        # a word that is both noise and (hypothetically) explicit would not confirm: it is gone before the vote
        ram = at.TaggerKind("ram", "RAM++ (swin-large)", ("general",), has_rating=False, noise=frozenset({"oral"}))
        saved = list(at.TAGGERS)
        at.configure_taggers([*saved[:2], ram])
        self.addCleanup(at.configure_taggers, saved)
        got = at.detect(raw_of("wd:oral=0.9,door=0.9|pixai:door=0.9|ram:oral=0.9|rating:general=0.9"), S())
        self.assertNotIn("oral", got["tags"])
        self.assertEqual(got["display"]["dropped"], ["oral"])                 # WD alone: dropped as on any general picture

    def test_the_noise_filter_is_applied_when_tags_are_made_not_when_scores_are_stored(self):
        raw = raw_of("ram:image=0.9,sea=0.9")
        self.assertEqual(raw["scores"][0]["ram"], {"general": {"image": 0.9, "sea": 0.9}})       # all of it is stored
        self.assertEqual(set(at.detect(raw, S(rating_tag=False))["tags"]), {"sea"})


class TestHydraE621(unittest.TestCase):
    """The real fourth tagger, Hydra 3.5 (key ``e621``): the furry booru's vocabulary in four categories (general,
    species, character, copyright), no rating, and ``mammal`` (it lands on every human and animal) as its noise word."""

    ONLY = dict(use_wd=False, use_pixai=False, use_ram=False)          # Hydra on its own

    def kept(self, *pictures, **settings):
        return dict(at.detect(raw_of(*pictures), S(rating_tag=False, **settings))["tags"])

    def listed(self, picture, **settings):
        return {t["tag"]: t["kept"] for t in at.detect(raw_of(picture), S(**settings))["display"]["e621"]}

    def test_its_four_categories_pass_its_own_strictness_and_say_e621(self):
        got = self.kept(E621_PHOTO, **self.ONLY)
        self.assertEqual(got, {"anthro": (0.95, "e621"), "fur": (0.6, "e621"), "wolf": (0.9, "e621"),
                               "canine": (0.6, "e621"), "fenrir": (0.8, "e621"), "norse mythology": (0.7, "e621")})
        low = self.kept(E621_PHOTO, e621_strictness=0.25, **self.ONLY)                     # tail .3, fox .25, loki .3
        self.assertEqual({t: low[t] for t in ("tail", "fox", "loki")},
                         {"tail": (0.3, "e621"), "fox": (0.25, "e621"), "loki": (0.3, "e621")})
        self.assertEqual(set(self.kept(E621_PHOTO, e621_strictness=0.85, **self.ONLY)), {"anthro", "wolf"})
        self.assertEqual(self.kept(E621_PHOTO, e621_strictness=0.95, **self.ONLY), {"anthro": (0.95, "e621")})
        # the other taggers' thresholds are not its own: its 0.3 stays below 0.5 while WD's 0.3 and PixAI's 0.4 pass 0.25
        got = self.kept(E621_PHOTO, wd_strictness=0.25, pixai_strictness=0.25)
        self.assertEqual((got["hat"], got["wave"]), ((0.3, "wd"), (0.4, "pixai")))
        self.assertEqual(got.keys() & {"tail", "fox", "loki"}, set())
        # ... and its own does not move theirs
        got = self.kept(E621_PHOTO, e621_strictness=0.25)
        self.assertEqual((got["tail"], got["girl"]), ((0.3, "e621"), (0.9, "wd")))
        self.assertEqual(got.keys() & {"hat", "wave"}, set())

    def test_it_can_be_switched_off_and_then_says_nothing(self):
        got = at.detect(raw_of(E621_PHOTO), S(use_e621=False))
        self.assertEqual(got["display"]["e621"], [])
        self.assertTrue(all(source != "e621" for _score, source in got["tags"].values()))
        self.assertEqual(got["tags"].keys() & {"anthro", "wolf", "fenrir", "norse mythology"}, set())
        self.assertIn("wolf", at.detect(raw_of(E621_PHOTO), S())["tags"])

    def test_the_species_character_and_copyright_categories_are_read_like_general(self):
        picture = "e621:anthro=0.9|e6species:wolf=0.8|e6char:fenrir=0.7|e6copy:norse mythology=0.6"
        self.assertEqual(self.kept(picture, **self.ONLY),
                         {"anthro": (0.9, "e621"), "wolf": (0.8, "e621"), "fenrir": (0.7, "e621"),
                          "norse mythology": (0.6, "e621")})
        self.assertEqual(self.listed(picture), {"anthro": True, "wolf": True, "fenrir": True, "norse mythology": True})

    def test_a_tag_in_two_of_its_categories_keeps_the_higher_score(self):
        self.assertEqual(self.kept("e621:wolf=0.6|e6species:wolf=0.8", **self.ONLY), {"wolf": (0.8, "e621")})
        self.assertEqual(self.kept("e621:wolf=0.9|e6species:wolf=0.6", **self.ONLY), {"wolf": (0.9, "e621")})
        # the category that is switched off no longer counts: the lower score of the other one stays
        self.assertEqual(self.kept("e621:fenrir=0.6|e6char:fenrir=0.8", character_tags=False, **self.ONLY),
                         {"fenrir": (0.6, "e621")})
        self.assertEqual(self.kept("e621:fenrir=0.6|e6char:fenrir=0.8", **self.ONLY), {"fenrir": (0.8, "e621")})

    def test_the_highest_score_wins_across_taggers_and_ties_go_to_the_earlier_one(self):
        got = self.kept("wd:smile=0.7,a=0.6|pixai:smile=0.8|ram:smile=0.85|e621:smile=0.9,a=0.8|rating:general=0.9")
        self.assertEqual((got["smile"], got["a"]), ((0.9, "e621"), (0.8, "e621")))
        self.assertEqual(self.kept("wd:smile=0.95|pixai:smile=0.8|ram:smile=0.9|e621:smile=0.9")["smile"], (0.95, "wd"))
        self.assertEqual(self.kept("wd:smile=0.9|e621:smile=0.9")["smile"], (0.9, "wd"))          # a tie: the earlier one
        self.assertEqual(self.kept("pixai:smile=0.9|e621:smile=0.9")["smile"], (0.9, "pixai"))
        self.assertEqual(self.kept("ram:smile=0.9|e621:smile=0.9")["smile"], (0.9, "ram"))
        self.assertEqual(self.kept("wd:woman=0.9|e621:woman=0.9", vocabulary="woman -> lady")["lady"], (0.9, "wd"))

    def test_character_tags_gate_its_characters_and_series_but_not_its_species(self):
        picture = "e621:anthro=0.9|e6species:wolf=0.9|e6char:fenrir=0.9|e6copy:norse mythology=0.9"
        self.assertEqual(self.kept(picture, **self.ONLY).keys(), {"anthro", "wolf", "fenrir", "norse mythology"})
        self.assertEqual(self.kept(picture, character_tags=False, **self.ONLY).keys(), {"anthro", "wolf"})
        # the Test card follows the same switch
        self.assertEqual(self.listed(picture).keys(), {"anthro", "wolf", "fenrir", "norse mythology"})
        self.assertEqual(self.listed(picture, character_tags=False).keys(), {"anthro", "wolf"})
        # one switch for every tagger's characters and series
        everyone = ("wd:girl=0.9|char:miku=0.9|pixai:smile=0.9|pchar:rin=0.9|copy:vocaloid=0.9|ram:sea=0.9|" + picture)
        self.assertEqual(self.kept(everyone).keys(), {"girl", "miku", "smile", "rin", "vocaloid", "sea", "anthro", "wolf",
                                                      "fenrir", "norse mythology"})
        self.assertEqual(self.kept(everyone, character_tags=False).keys(), {"girl", "smile", "sea", "anthro", "wolf"})

    def test_mammal_never_shows(self):
        # it is on every human and animal, so it says nothing: not a tag, not on the Test card, not in the block,
        # whatever the threshold, and whichever of its categories it comes in
        picture = "e621:mammal=0.99,anthro=0.9|e6species:Mammal=0.95,wolf=0.9|e6char:mammal=0.9|rating:general=0.9"
        for strictness in (0.5, at.STRICTNESS[0]):
            with self.subTest(strictness=strictness):
                built = at.build(raw_of(picture), S(e621_strictness=strictness))
                self.assertEqual(tags_of(built), ["anthro", "wolf", "rating: general"])
                self.assertEqual(built["block"], "[AI Tagger]\nTags: anthro, wolf, rating: general\n[/AI Tagger]")
                self.assertEqual({t["tag"] for t in built["models"]["e621"]}, {"anthro", "wolf"})
        # stored in full (it is dropped when tags are made, not when scores are stored), and exactly its word
        raw = raw_of(picture)
        self.assertEqual(raw["scores"][0]["e621"]["general"], {"mammal": 0.99, "anthro": 0.9})
        self.assertEqual(self.kept("e621:mammals=0.9,mammal like=0.9", **self.ONLY).keys(), {"mammals", "mammal like"})
        # the same word from the other taggers is a tag like any other, and Hydra's .99 never counts against theirs
        got = self.kept("wd:mammal=0.8|pixai:mammal=0.85|ram:mammal=0.7|e621:mammal=0.99")
        self.assertEqual(got, {"mammal": (0.85, "pixai")})
        self.assertEqual(self.kept("e621:mammal=0.99", **self.ONLY), {})

    def test_it_reports_no_rating_so_it_never_takes_part_in_it(self):
        # general: wd .9, pixai .3 -> .6; explicit: wd .1, pixai .7 -> .4 (Hydra has no rating to add)
        picture = "wd:a=0.9|rating:general=0.9,explicit=0.1|prating:general=0.3,explicit=0.7|e621:b=0.9"
        got = at.detect(raw_of(picture), S())
        self.assertEqual(got["display"]["rating"], {"general": 0.6, "explicit": 0.4})
        self.assertEqual((got["tags"]["rating: general"][1], round(got["tags"]["rating: general"][0], 3)), ("wd", 0.6))
        self.assertEqual(at.detect(raw_of(picture), S(use_e621=False))["display"]["rating"], got["display"]["rating"])
        # a rating stored under "e621" (nothing writes one) is not read
        raw = raw_of(picture)
        raw["ratings"][0]["e621"] = {"general": 0.0, "explicit": 1.0}
        self.assertEqual(at.detect(raw, S())["display"]["rating"], {"general": 0.6, "explicit": 0.4})
        # and a rating in the service's answer for it is not stored (nor is a stray category)
        stored = at.raw_from_results([{"wd": {"general": {"a": 0.9}, "character": {}, "rating": {"general": 0.8}},
                                       "e621": {"general": {"b": 0.9}, "species": {}, "character": {}, "copyright": {},
                                                "rating": {"explicit": 0.99}, "artist": {"someone": 0.9}}}], [None])
        self.assertEqual(stored["ratings"], [{"wd": {"general": 0.8}, "pixai": None, "ram": None, "e621": None}])
        self.assertEqual(stored["scores"][0]["e621"], {"general": {"b": 0.9}, "species": {}, "character": {}, "copyright": {}})
        # with Hydra (and RAM++) the only taggers there is no rating at all: no rating tag, nothing on the Test card
        for only in (dict(use_wd=False, use_pixai=False, use_ram=False), dict(use_wd=False, use_pixai=False)):
            alone = at.detect(raw_of(picture + "|ram:c=0.9"), S(**only))
            self.assertEqual(alone["display"]["rating"], {})
            self.assertEqual({t: source for t, (_s, source) in alone["tags"].items()},
                             {"b": "e621"} if "use_ram" in only else {"b": "e621", "c": "ram"})
        # it does not dilute the mean of the two that have one: each averaged over the captures, then the two
        two = ("wd:a=0.9|rating:general=0.8|prating:general=0.2|e621:b=0.9", "wd:a=0.9|rating:general=0.6|prating:general=0.4|e621:b=0.9")
        self.assertAlmostEqual(at.detect(raw_of(*two), S())["display"]["rating"]["general"], 0.5, places=3)
        # a tie still goes to the earlier tagger, not to Hydra
        tie = at.detect(raw_of("wd:a=0.9|rating:general=0.6,sensitive=0.4|prating:general=0.6,sensitive=0.4|e621:b=0.9"), S())
        self.assertEqual(tie["tags"]["rating: general"], (0.6, "wd"))

    def test_it_combines_the_captures_like_the_others_category_by_category(self):
        raw = raw_of("e621:cat=0.9,dog=0.6", "e621:cat=0.2", "e6species:cat=0.9")
        found = at.detect(raw, S(rating_tag=False, **self.ONLY))
        self.assertAlmostEqual(found["tags"]["cat"][0], 0.9)                  # median .9, best .9 (capture 3 says it as species)
        self.assertEqual(set(found["tags"]), {"cat"})                         # "dog" in one capture only: (0 + .6) / 2 = .3
        self.assertEqual({t["tag"]: (t["score"], t["kept"]) for t in found["display"]["e621"]},
                         {"cat": (0.9, True), "dog": (0.3, False)})
        raw = at.raw_from_results([read_picture(b"e621:cat=0.9")[0], None], [None, "cannot identify image file"])
        self.assertEqual(raw["captures"], 1)                                   # a capture it could not read is no vote

    def test_its_names_are_normalised_like_the_others(self):
        # Hydra's names keep underscores (``human_on_anthro``); some have capitals
        got = self.kept("e621:human_on_anthro=0.9,Male=0.8|e6species:Canis Lupus=0.7|e6copy:the_legend_of_zelda=0.6", **self.ONLY)
        self.assertEqual(got, {"human on anthro": (0.9, "e621"), "male": (0.8, "e621"), "canis lupus": (0.7, "e621"),
                               "the legend of zelda": (0.6, "e621")})

    def test_a_rename_or_a_combination_can_bridge_the_two_vocabularies(self):
        # WD's own "furry with non-furry" is Hydra's "human on anthro"; a combination adds one from the other's tags
        got = self.kept("wd:furry with non-furry=0.8|e621:human_on_anthro=0.7,anthro=0.9",
                        vocabulary="furry with non-furry -> human on anthro")
        self.assertEqual(got, {"human on anthro": (0.8, "wd"), "anthro": (0.9, "e621")})
        got = at.build(raw_of("wd:furry=0.9,human=0.8|e621:anthro=0.9"), S(rating_tag=False, vocabulary="furry + human -> human on anthro"))
        self.assertEqual(got["rules"], [{"rule": "line 1", "added": ["human on anthro"], "removed": []}])
        self.assertEqual({t["tag"]: t["source"] for t in got["tags"]}["human on anthro"], "rule")


class TestFourTaggers(Base):
    """The real four taggers (wd, pixai, ram, e621), all on: agreement, the rating, the lists, the merge, the stored
    scores, what is asked for."""

    def detect(self, *pictures, **settings):
        return at.detect(raw_of(*pictures), S(**settings))

    def test_ram_and_e621_can_be_switched_off_so_two_taggers_work_as_before(self):
        picture = "wd:a=0.9|pixai:b=0.9|ram:c=0.9|e621:d=0.9|rating:general=0.9"
        self.assertEqual(set(self.detect(picture)["tags"]), {"a", "b", "c", "d", "rating: general"})
        self.assertEqual(set(self.detect(picture, use_ram=False)["tags"]), {"a", "b", "d", "rating: general"})
        self.assertEqual(set(self.detect(picture, use_e621=False)["tags"]), {"a", "b", "c", "rating: general"})
        self.assertEqual(set(self.detect(picture, use_ram=False, use_e621=False)["tags"]), {"a", "b", "rating: general"})
        self.assertEqual(self.detect(picture, use_ram=False)["display"]["ram"], [])
        self.assertEqual(self.detect(picture, use_e621=False)["display"]["e621"], [])

    def test_a_sexual_tag_is_kept_when_any_two_of_the_four_found_it(self):
        # every subset of the four taggers that names "oral": two, three or four keep it, one alone (or none) does not
        for who in subsets(("wd", "pixai", "ram", "e621")):
            with self.subTest(named_by=who):
                picture = "|".join(f"{key}:door=0.9" + (",oral=0.9" if key in who else "")
                                   for key in ("wd", "pixai", "ram", "e621")) + "|rating:general=0.9"
                got = self.detect(picture)
                self.assertEqual("oral" in got["tags"], len(who) >= 2)
                self.assertEqual(got["display"].get("dropped"), ["oral"] if len(who) == 1 else None)
                self.assertIn("door", got["tags"])

    def test_found_means_it_passed_that_taggers_own_strictness(self):
        for key in ("ram", "e621"):
            with self.subTest(tagger=key):
                picture = (f"wd:oral=0.9,door=0.9|pixai:door=0.9|{key}:oral=0.3,door=0.9|"
                           "rating:general=0.9|prating:general=0.9")
                self.assertNotIn("oral", self.detect(picture)["tags"])                       # its 0.3 is below its 0.5
                self.assertIn("oral", self.detect(picture, **{f"{key}_strictness": 0.25})["tags"])     # now it found it too
                self.assertNotIn("oral", self.detect(picture, **{f"{key}_strictness": 0.25, f"use_{key}": False})["tags"])
                # another tagger's threshold is not its own
                other = "ram" if key == "e621" else "e621"
                self.assertNotIn("oral", self.detect(picture, **{f"{other}_strictness": 0.25})["tags"])
                # switching a second tagger off leaves one: dropped (PixAI still gives the general rating that asks for it)
                self.assertNotIn("oral", self.detect(picture, **{f"{key}_strictness": 0.25, "use_wd": False})["tags"])

    def test_with_the_real_registry_ram_neither_confirms_nor_removes_a_sexual_tag(self):
        # RAM++ has no sexual vocabulary: what it says is plain English, so it neither confirms nor removes one
        for picture, kept in [("wd:oral=0.9,door=0.9|pixai:oral=0.8,door=0.9|ram:door=0.9,room=0.8", True),
                              ("wd:oral=0.9,door=0.9|pixai:door=0.9|ram:door=0.9,room=0.8", False),
                              ("wd:door=0.9|pixai:oral=0.9,door=0.9|ram:door=0.9,room=0.8", False)]:
            for ram in (True, False):
                with self.subTest(picture=picture, ram=ram):
                    got = self.detect(picture + "|rating:general=0.9", use_ram=ram)
                    self.assertEqual("oral" in got["tags"], kept)
                    self.assertEqual(got["display"].get("dropped"), None if kept else ["oral"])
                    self.assertEqual("room" in got["tags"], ram)
        # Hydra's vocabulary does cover them: it is a voter like WD and PixAI, with or without RAM++
        for ram in (True, False):
            with self.subTest(hydra=True, ram=ram):
                got = self.detect("wd:oral=0.9,door=0.9|pixai:door=0.9|ram:door=0.9|e621:oral=0.8|rating:general=0.9", use_ram=ram)
                self.assertIn("oral", got["tags"])
                got = self.detect("wd:door=0.9|pixai:door=0.9|ram:door=0.9|e621:oral=0.8|rating:general=0.9", use_ram=ram)
                self.assertNotIn("oral", got["tags"])
        self.assertIn("oral", self.detect("wd:oral=0.9|pixai:oral=0.9|ram:door=0.9|e621:door=0.9|rating:explicit=0.9")["tags"])

    def test_an_explicit_rating_keeps_tags_one_tagger_saw(self):
        got = self.detect("wd:oral=0.9|pixai:a=0.9|ram:b=0.9|e621:c=0.9|rating:explicit=0.9,general=0.05")
        self.assertIn("oral", got["tags"])

    def test_ram_and_e621_never_take_part_in_the_rating(self):
        # general: wd .9, pixai .3 -> .6; explicit: wd .1, pixai .7 -> .4, whatever the other two say
        picture = ("wd:a=0.9|rating:general=0.9,explicit=0.1|prating:general=0.3,explicit=0.7|ram:b=0.9|e621:c=0.9")
        want = {"general": 0.6, "explicit": 0.4}
        for off in subsets(("use_ram", "use_e621")):
            with self.subTest(off=off):
                got = self.detect(picture, **{key: False for key in off})
                self.assertEqual(got["display"]["rating"], want)
                self.assertEqual(got["tags"]["rating: general"][1], "wd")
        raw = raw_of(picture)                                              # ratings stored under their keys are not read
        raw["ratings"][0]["ram"] = {"general": 0.0, "explicit": 1.0}
        raw["ratings"][0]["e621"] = {"general": 0.0, "explicit": 1.0}
        self.assertEqual(at.detect(raw, S())["display"]["rating"], want)
        self.assertEqual(raw_of(picture)["ratings"], [{"wd": {"general": 0.9, "explicit": 0.1},
                                                       "pixai": {"general": 0.3, "explicit": 0.7}, "ram": None, "e621": None}])

    def test_with_ram_or_e621_alone_there_is_no_rating_and_so_no_explicit_check(self):
        # with either the only tagger (or both) there is no rating and so no check: its tags are all kept
        for only in ({"use_wd": False, "use_pixai": False, "use_e621": False}, {"use_wd": False, "use_pixai": False, "use_ram": False},
                     {"use_wd": False, "use_pixai": False}):
            with self.subTest(only=only):
                got = self.detect("ram:oral=0.9,door=0.9|e621:oral=0.9,door=0.9", **only)
                self.assertEqual(set(got["tags"]), {"oral", "door"})
                self.assertNotIn("dropped", got["display"])
                self.assertEqual(got["display"]["rating"], {})

    def test_the_preview_has_a_list_for_every_registered_tagger(self):
        picture = ("wd:girl=0.9,hat=0.3|pixai:beach=0.8|ram:sea=0.7,wave=0.3,gone=0.1|"
                   "e621:anthro=0.9,tail=0.3,gone too=0.1|rating:general=0.9")
        shown = self.detect(picture)["display"]
        self.assertEqual(set(shown), {"wd", "pixai", "ram", "e621", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in shown["ram"]}, {"sea": True, "wave": False})   # from 0.2 up
        self.assertEqual({t["tag"]: t["kept"] for t in shown["e621"]}, {"anthro": True, "tail": False})
        self.assertEqual([t["tag"] for t in shown["wd"]], ["girl", "hat"])
        for key in ("ram", "e621"):                                       # off: an empty list, the others as before
            off = self.detect(picture, **{f"use_{key}": False})["display"]
            self.assertEqual((off[key], set(off)), ([], {"wd", "pixai", "ram", "e621", "rating"}))
            self.assertEqual([t["tag"] for t in off["wd"]], ["girl", "hat"])

    def test_stored_scores_keep_every_taggers_categories_and_rating(self):
        raw = raw_of("wd:girl=0.9|char:miku=0.8|rating:general=0.9|pixai:smile=0.7|pchar:rin=0.6|copy:vocaloid=0.8|"
                     "prating:general=0.8|ram:sea=0.7,Birthday Cake=0.4|"
                     "e621:anthro=0.9,Mammal=0.4|e6species:wolf=0.8|e6char:fenrir=0.6|e6copy:norse mythology=0.7")
        self.assertEqual(raw["scores"], [{"wd": {"general": {"girl": 0.9}, "character": {"miku": 0.8}},
                                          "pixai": {"general": {"smile": 0.7}, "character": {"rin": 0.6},
                                                    "copyright": {"vocaloid": 0.8}},
                                          "ram": {"general": {"sea": 0.7, "Birthday Cake": 0.4}},       # nested, as served
                                          "e621": {"general": {"anthro": 0.9, "Mammal": 0.4}, "species": {"wolf": 0.8},
                                                   "character": {"fenrir": 0.6}, "copyright": {"norse mythology": 0.7}}}])
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.9}, "pixai": {"general": 0.8}, "ram": None, "e621": None}])
        self.assertEqual(raw["models"], ["e621", "pixai", "ram", "wd"])
        self.store.save_raw("a", raw)
        self.assertEqual(self.store.raw("a"), raw)
        self.assertTrue(at.has_kind(raw, "ram"))
        self.assertTrue(at.has_kind(raw, "e621"))

    def test_a_service_answer_that_lacks_one_of_hydras_categories_is_stored_with_it_empty(self):
        stored = at.raw_from_results([{"e621": {"general": {"anthro": 0.9}, "species": {"wolf": 0.8}}}], [None])
        self.assertEqual(stored["scores"][0]["e621"], {"general": {"anthro": 0.9}, "species": {"wolf": 0.8},
                                                       "character": {}, "copyright": {}})
        self.assertEqual(stored["models"], ["e621"])
        # a tagger that was not asked for has no entry at all (None), and so counts as missing when it is on
        only_wd = at.raw_from_results([read_picture(b"wd:a=0.9", ["wd"])[0]], [None])
        self.assertEqual(only_wd["scores"], [{"wd": {"general": {"a": 0.9}, "character": {}}, "pixai": None, "ram": None,
                                              "e621": None}])
        self.assertFalse(at.has_kind(only_wd, "e621"))

    def test_a_stored_raw_row_lacking_an_enabled_tagger_is_incomplete(self):
        full = raw_of("wd:a=0.9|pixai:b=0.9|rating:general=0.9")
        for gone in ("ram", "e621"):
            with self.subTest(made_before=gone):
                old = {**full, "scores": [{k: v for k, v in cap.items() if k != gone} for cap in full["scores"]]}
                self.assertEqual(at.missing_kinds(old, S()), [gone])
                self.assertEqual(at.missing_kinds(old, S(**{f"use_{gone}": False})), [])               # it is off: nothing missing
                self.assertEqual(at.missing_kinds(old, S(use_wd=False, use_pixai=False)), [gone])
        both = {**full, "scores": [{k: v for k, v in cap.items() if k not in ("ram", "e621")} for cap in full["scores"]]}
        self.assertEqual(at.missing_kinds(both, S()), ["ram", "e621"])
        self.assertEqual(at.missing_kinds(both, S(use_ram=False)), ["e621"])
        self.assertEqual(at.missing_kinds(at.raw_from_results([read_picture(b"wd:a=0.9", ["wd", "pixai"])[0]], [None]), S()),
                         ["ram", "e621"])
        self.assertEqual(at.missing_kinds(None, S()), REAL)
        self.assertEqual(at.missing_kinds(raw_of("wd:a=0.9|ram:b=0.9|e621:c=0.9"), S()), [])
        self.assertEqual(at.missing_kinds(raw_of("wd:a=0.9"), S(use_wd=False, use_pixai=False, use_ram=False, use_e621=False)), [])

    def test_only_the_enabled_taggers_are_asked_for_and_stored(self):
        self.run_indexer()
        self.assertEqual(set(map(tuple, self.services.tag_models)), {tuple(REAL)})
        for key in ("ram", "e621"):
            self.assertTrue(at.has_kind(self.store.raw(self.ids[0]), key))
        for asked in [("wd",), ("ram", "e621"), ("e621",), ("ram",), ("wd", "pixai", "e621"), ("pixai", "ram")]:
            with self.subTest(enabled=asked):
                self.services.tag_models.clear()
                self.store.enqueue(self.ids[:2], "full")
                self.run_indexer(**{f"use_{key}": key in asked for key in REAL})
                self.assertEqual(set(map(tuple, self.services.tag_models)), {asked})      # asked for in registry order
                raw = self.store.raw(self.ids[0])
                self.assertEqual(raw["models"], sorted(asked))
                for kind in REAL:
                    self.assertEqual(at.has_kind(raw, kind), kind in asked, kind)

    def test_ram_takes_part_end_to_end(self):
        self.catalog[0]["preview"] = RAM_PHOTO
        self.run_indexer()
        self.assertEqual(self.description(0),
                         "[AI Tagger]\nTags: sea, girl, lake, beach, miku, solo, screenshot, rating: general\n[/AI Tagger]")
        by = {t["tag"]: t for t in self.store.result(self.ids[0])["tags"]}
        self.assertEqual((by["sea"]["source"], by["lake"]["source"], by["screenshot"]["source"], by["girl"]["source"],
                          by["beach"]["source"]), ("ram", "ram", "ram", "wd", "pixai"))
        self.assertEqual(by["sea"]["score"], 0.95)                          # the highest of PixAI's .7 and RAM++'s .95
        for noise in ("image", "catch", "wave"):                            # its noise words, and the one below 0.5
            self.assertNotIn(noise, by)
        self.assertEqual(self.store.raw(self.ids[0])["models"], ["e621", "pixai", "ram", "wd"])
        self.assertIn("image", self.store.raw(self.ids[0])["scores"][0]["ram"]["general"])      # stored, filtered at tagging
        self.assertEqual(self.description(1), "[AI Tagger]\nTags: dog, grass, rating: general\n[/AI Tagger]")

    def test_hydra_takes_part_end_to_end(self):
        self.catalog[0]["preview"] = E621_PHOTO
        self.run_indexer()
        self.assertEqual(self.description(0), "[AI Tagger]\nTags: anthro, girl, wolf, beach, fenrir, miku, solo, "
                                              "norse mythology, sea, canine, fur, rating: general\n[/AI Tagger]")
        by = {t["tag"]: t for t in self.store.result(self.ids[0])["tags"]}
        for tag in ("anthro", "wolf", "fenrir", "norse mythology", "fur", "canine"):
            self.assertEqual(by[tag]["source"], "e621", tag)
        self.assertEqual((by["girl"]["source"], by["beach"]["source"], by["sea"]["source"]), ("wd", "pixai", "pixai"))
        self.assertEqual((by["anthro"]["score"], by["wolf"]["score"]), (0.95, 0.9))
        for left_out in ("mammal", "tail", "fox", "loki"):                 # its noise word, and three below 0.5
            self.assertNotIn(left_out, by)
        raw = self.store.raw(self.ids[0])
        self.assertEqual(raw["models"], ["e621", "pixai", "ram", "wd"])
        self.assertEqual(raw["scores"][0]["e621"]["species"], {"wolf": 0.9, "canine": 0.6, "fox": 0.25})   # all of it is stored
        self.assertEqual(raw["scores"][0]["e621"]["general"]["mammal"], 0.99)
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.9, "sensitive": 0.08, "questionable": 0.01, "explicit": 0.01},
                                           "pixai": None, "ram": None, "e621": None}])
        self.assertEqual(self.description(1), "[AI Tagger]\nTags: dog, grass, rating: general\n[/AI Tagger]")

    def test_the_character_switch_is_a_retag_that_takes_hydras_characters_and_series_away(self):
        self.catalog[0]["preview"] = ALL_PHOTO
        self.run_indexer()
        self.assertIn("fenrir", self.description(0))
        self.services.tag_calls.clear()
        self.set(character_tags=False)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])                         # the stored scores are enough
        names = {t["tag"]: t["source"] for t in self.store.result(self.ids[0])["tags"]}
        self.assertEqual(names.keys() & {"fenrir", "norse mythology", "miku"}, set())
        self.assertEqual((names["wolf"], names["anthro"], names["canine"]), ("e621", "e621", "e621"))   # species stays

    def test_noise_never_reaches_the_written_description_or_the_native_tags(self):
        ram = next(k for k in at.TAGGERS if k.key == "ram")
        self.catalog[0]["preview"] = "ram:" + ",".join(f"{w}=0.99" for w in sorted(ram.noise)) + ",lake=0.9"
        self.run_indexer(write_tags=True)
        self.assertEqual(self.description(0), "[AI Tagger]\nTags: lake\n[/AI Tagger]")     # no rating: nobody here has one
        self.assertEqual(sorted(self.fake.tags[t]["value"] for t in self.fake.asset_tags[self.ids[0]]), ["AI/lake"])
        hydra = next(k for k in at.TAGGERS if k.key == "e621")
        self.catalog[0]["preview"] = "e621:" + ",".join(f"{w}=0.99" for w in sorted(hydra.noise)) + ",anthro=0.9"
        self.indexer.last_sync = None                                       # read the library list again
        self.store.enqueue(self.ids[:1], "full")
        self.run_indexer(write_tags=True)
        self.assertEqual(self.description(0), "[AI Tagger]\nTags: anthro\n[/AI Tagger]")
        self.assertEqual(sorted(self.fake.tags[t]["value"] for t in self.fake.asset_tags[self.ids[0]]), ["AI/anthro"])

    def enabling_it_later(self, key):
        self.run_indexer(**{f"use_{key}": False})
        self.assertFalse(at.has_kind(self.store.raw(self.ids[0]), key))
        for other in REAL:
            if other != key:
                self.assertTrue(at.has_kind(self.store.raw(self.ids[0]), other), other)
        self.services.tag_calls.clear()
        self.services.tag_models.clear()
        self.assertEqual(self.set(**{f"use_{key}": True}), [f"use_{key}"])
        self.assertEqual(at.reprocess(self.store, "outdated", "retag"), 3)  # only a retag was asked for ...
        self.run_indexer()
        self.assertEqual(sum(self.services.tag_calls), 1 + 1 + 3)           # ... but the stored scores lack an enabled tagger
        self.assertEqual({tuple(m) for m in self.services.tag_models}, {tuple(REAL)})
        self.assertTrue(at.has_kind(self.store.raw(self.ids[0]), key))
        self.services.tag_calls.clear()
        self.assertEqual(self.set(**{f"{key}_strictness": 0.6}), [f"{key}_strictness"])
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])                       # complete now: a retag is cheap again

    def test_enabling_ram_later_makes_a_retag_a_full_reprocess(self):
        self.enabling_it_later("ram")

    def test_enabling_hydra_later_makes_a_retag_a_full_reprocess(self):
        self.enabling_it_later("e621")

    def test_a_retag_with_all_four_stored_needs_no_gpu(self):
        self.catalog[0]["preview"] = ALL_PHOTO
        self.run_indexer()
        self.services.tag_calls.clear()
        self.set(ram_strictness=0.95)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])
        names = {t["tag"]: t["source"] for t in self.store.result(self.ids[0])["tags"]}
        self.assertNotIn("lake", names)                                     # RAM++'s .9 is below its .95 now
        self.assertNotIn("screenshot", names)
        self.assertEqual(names["sea"], "ram")                               # its .95 still passes
        self.assertEqual((names["wolf"], names["anthro"]), ("e621", "e621"))        # Hydra's are as they were
        self.set(ram_strictness=0.5, use_ram=False)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])                       # switching it off applies to the stored scores too
        names = {t["tag"]: t["source"] for t in self.store.result(self.ids[0])["tags"]}
        self.assertEqual((names.get("lake"), names.get("screenshot"), names["sea"], names["wolf"]), (None, None, "pixai", "e621"))
        self.set(use_ram=True, e621_strictness=0.95)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])
        names = {t["tag"]: t["source"] for t in self.store.result(self.ids[0])["tags"]}
        self.assertEqual((names["anthro"], names.get("wolf"), names.get("fenrir"), names["lake"]), ("e621", None, None, "ram"))
        self.set(e621_strictness=0.5, use_e621=False)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])                       # ... and Hydra's too
        names = {t["tag"]: t["source"] for t in self.store.result(self.ids[0])["tags"]}
        self.assertEqual((names.get("anthro"), names.get("wolf"), names.get("fenrir"), names["lake"]), (None, None, None, "ram"))

    def agreement_end_to_end(self, key):
        picture = f"wd:door=0.9,oral=0.9|pixai:door=0.9|{key}:oral=0.9|rating:general=0.9"
        self.catalog = [{**self.catalog[0], "preview": picture}]
        self.run_indexer()
        self.assertIn("oral", self.description(0))                          # WD and the other one agree
        self.set(**{f"use_{key}": False})
        at.reprocess(self.store, "outdated", "retag")
        calls = list(self.services.tag_calls)
        self.run_indexer()
        self.assertNotIn("oral", self.description(0))                       # only WD is left that saw it
        self.assertEqual(self.services.tag_calls, calls)                    # (the stored scores were enough)
        self.set(**{f"use_{key}": True})
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertIn("oral", self.description(0))                          # and back, from the same stored scores
        self.assertEqual(self.services.tag_calls, calls)

    def test_agreement_by_any_two_of_the_four_end_to_end_with_ram(self):
        self.agreement_end_to_end("ram")

    def test_agreement_by_any_two_of_the_four_end_to_end_with_hydra(self):
        self.agreement_end_to_end("e621")

    def test_the_preview_lists_all_four_taggers(self):
        self.catalog[0]["preview"] = ALL_PHOTO
        got = self.indexer.test(self.ids[0])
        self.assertEqual(set(got["models"]), {"wd", "pixai", "ram", "e621", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in got["models"]["ram"]},
                         {"sea": True, "lake": True, "screenshot": True, "wave": False})       # no noise word is listed
        self.assertEqual({t["tag"]: t["kept"] for t in got["models"]["e621"]},
                         {"anthro": True, "fur": True, "wolf": True, "canine": True, "fenrir": True, "norse mythology": True,
                          "tail": False, "fox": False, "loki": False})                         # no "mammal"
        self.assertEqual(self.services.tag_models, [REAL])
        self.assertEqual((got["models"]["rating"]["general"], got["models"]["rating"]["sensitive"]), (0.9, 0.08))
        self.assertEqual(self.services.ready_calls[0], {"tagger": True})
        self.assertEqual({t["tag"] for t in got["tags"]} & {"image", "catch", "mammal"}, set())
        self.assertTrue({"screenshot", "wolf", "fenrir"} <= {t["tag"] for t in got["tags"]})
        self.assertNotIn("image", got["block"])
        self.assertNotIn("mammal", got["block"])
        # with one of them off the card has its (empty) list and the others as before
        for key, gone in (("ram", "lake"), ("e621", "wolf")):
            self.services.tag_models.clear()
            self.set(**{f"use_{key}": False})
            off = self.indexer.test(self.ids[0])
            self.assertEqual((off["models"][key], self.services.tag_models), ([], [[k for k in REAL if k != key]]))
            self.assertNotIn(gone, {t["tag"] for t in off["tags"]})
            self.set(**{f"use_{key}": True})


class TestV1ScoresStayReadable(Base):
    """v1 stored RAM++ flat (``"ram": {tag: score}``, no category level) and WD's rating flat. Such rows may still be in
    the store: they are read as today's shape (RAM++'s ``general``, WD's rating), never taken for "no tags"."""

    def test_a_flat_ram_row_is_read_as_the_general_category(self):
        raw = v1_raw()
        for settings in (S(use_pixai=False), S(use_pixai=False, ram_strictness=0.7), S(use_pixai=False, use_wd=False),
                         S(use_pixai=False, use_ram=False), S(use_pixai=False, rating_tag=False)):
            self.assertEqual(at.detect(raw, settings), at.detect(v1_as_v3(raw), settings))
        got = at.detect(raw, S(use_pixai=False))
        self.assertEqual(got["tags"], {"girl": (0.9, "wd"), "beach": (0.8, "ram"), "sea": (0.6, "ram"),
                                       "rating: general": (0.9, "wd")})
        self.assertEqual({t["tag"]: t["kept"] for t in got["display"]["ram"]}, {"beach": True, "sea": True, "wave": False})
        self.assertEqual(got["display"]["rating"], {"general": 0.9, "sensitive": 0.08})
        self.assertNotIn("image", got["tags"])                                # (.9, but one of RAM++'s noise words)

    def test_a_flat_row_neither_crashes_nor_vanishes_when_a_tag_is_called_like_a_category(self):
        raw = v1_raw()
        raw["scores"][0]["ram"] = {"general": 0.7, "beach": 0.8}              # RAM++ tag "general", next to a real one
        got = at.detect(raw, S(use_pixai=False, rating_tag=False))
        self.assertEqual({t: v for t, v in got["tags"].items()}, {"girl": (0.9, "wd"), "general": (0.7, "ram"),
                                                                  "beach": (0.8, "ram")})
        raw["scores"][0]["ram"] = {}                                          # nothing said: no tags, no error
        self.assertEqual(set(at.detect(raw, S(use_pixai=False, rating_tag=False))["tags"]), {"girl"})
        self.assertEqual(at.detect(raw, S(use_pixai=False, rating_tag=False))["display"]["ram"], [])

    def test_a_mixed_row_is_read_per_capture(self):
        # an old row and a new capture in the same list (cannot happen today, but each capture is read for what it is)
        raw = v1_raw()
        raw["scores"].append({"wd": {"general": {"girl": 0.9}, "character": {}}, "ram": {"general": {"beach": 0.8}}})
        raw["captures"] = 2
        got = at.detect(raw, S(use_pixai=False, rating_tag=False))
        self.assertEqual(got["tags"]["beach"], (0.8, "ram"))

    def test_the_flat_shape_in_a_service_answer_is_stored_nested(self):
        stored = at.raw_from_results([{"wd": {"general": {"a": 0.9}, "character": {}, "rating": {"general": 0.9}},
                                       "ram": {"beach": 0.8}}], [None])
        self.assertEqual(stored["scores"][0]["ram"], {"general": {"beach": 0.8}})
        self.assertEqual(stored["ratings"], [{"wd": {"general": 0.9}, "pixai": None, "ram": None, "e621": None}])

    def test_a_v1_row_lacks_pixai_and_hydra_so_with_them_on_it_needs_a_full_reprocess(self):
        raw = v1_raw()
        self.assertTrue(at.has_kind(raw, "ram"))
        self.assertTrue(at.has_kind(raw, "wd"))
        self.assertFalse(at.has_kind(raw, "pixai"))
        self.assertFalse(at.has_kind(raw, "e621"))
        self.assertEqual(at.missing_kinds(raw, S()), ["pixai", "e621"])        # the default settings: a retag becomes full
        self.assertEqual(at.missing_kinds(raw, S(use_pixai=False)), ["e621"])  # Hydra is still on, and has no scores in it
        self.assertEqual(at.missing_kinds(raw, S(use_pixai=False, use_e621=False)), [])      # both off: enough

    def test_a_v1_store_row_round_trips_as_written_and_is_read_right(self):
        folder = Path(self.tmp.name) / "v1rows"
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        raw = v1_raw()
        store.conn.execute("insert into raw values (?,?,?,?,?,?)", ("a", 1, json.dumps(raw["scores"]),
                                                                     json.dumps(raw["ratings"]), "2026-09-30T10:00:00+00:00",
                                                                     "ram,wd"))
        store.conn.commit()
        got = store.raw("a")
        self.assertEqual(got, raw)                                            # nothing is rewritten behind the owner's back
        self.assertEqual(at.detect(got, S(use_pixai=False))["tags"]["beach"], (0.8, "ram"))
        self.assertEqual(at.build(got, S(use_pixai=False))["block"],
                         "[AI Tagger]\nTags: girl, beach, sea, rating: general\n[/AI Tagger]")


class TestAFifthTagger(Base):
    """The registry is open: a fake fifth tagger ``extra`` on top of wd, pixai, ram and e621 takes part in everything the
    registry loops over (agreement, the rating, the lists, the merge, the stored scores, the models asked for)."""

    def setUp(self):
        with_extra(self)
        super().setUp()

    def detect(self, *pictures, **settings):
        return at.detect(raw_of(*pictures), S(**{"use_extra": True, **settings}))

    def test_extra_is_off_by_default_so_the_four_work_as_before(self):
        picture = "wd:a=0.9|pixai:b=0.9|ram:r=0.9|e621:h=0.9|extra:c=0.9|rating:general=0.9"
        raw = raw_of(picture)
        self.assertEqual(set(at.detect(raw, S())["tags"]), {"a", "b", "r", "h", "rating: general"})
        self.assertEqual(set(self.detect(picture)["tags"]), {"a", "b", "r", "h", "c", "rating: general"})

    def test_a_sexual_tag_is_kept_when_any_two_of_the_five_found_it(self):
        keys = (*REAL, "extra")
        for who in subsets(keys):
            with self.subTest(named_by=who):
                picture = "|".join(f"{key}:door=0.9" + (",oral=0.9" if key in who else "") for key in keys) + \
                          "|rating:general=0.9"
                got = self.detect(picture)
                self.assertEqual("oral" in got["tags"], len(who) >= 2)
                self.assertEqual(got["display"].get("dropped"), ["oral"] if len(who) == 1 else None)
                self.assertIn("door", got["tags"])

    def test_found_means_it_passed_that_taggers_own_strictness(self):
        picture = "wd:oral=0.9,door=0.9|pixai:door=0.9|extra:oral=0.3,door=0.9|rating:general=0.9|erating:general=0.9"
        self.assertNotIn("oral", self.detect(picture)["tags"])                           # extra's 0.3 is below its 0.5
        self.assertIn("oral", self.detect(picture, extra_strictness=0.25)["tags"])       # now extra found it too
        self.assertNotIn("oral", self.detect(picture, extra_strictness=0.25, use_extra=False)["tags"])
        # switching a second tagger off leaves one: dropped
        self.assertNotIn("oral", self.detect(picture, extra_strictness=0.25, use_wd=False)["tags"])

    def test_an_explicit_rating_keeps_tags_one_tagger_saw(self):
        got = self.detect("wd:oral=0.9|pixai:a=0.9|extra:b=0.9|rating:explicit=0.9,general=0.05")
        self.assertIn("oral", got["tags"])

    def test_the_rating_is_the_mean_of_the_taggers_that_report_one_and_ram_and_hydra_do_not(self):
        # general: wd .9, pixai .3, extra .6 -> .6; explicit: wd .1, pixai .7, extra .4 -> .4 (RAM++ and Hydra report none)
        pictures = ("wd:a=0.9|rating:general=0.9,explicit=0.1|prating:general=0.3,explicit=0.7|extra:b=0.9|ram:r=0.9|"
                    "e621:h=0.9|erating:general=0.6,explicit=0.4",)
        got = self.detect(*pictures)
        self.assertEqual(got["display"]["rating"], {"general": 0.6, "explicit": 0.4})
        score, source = got["tags"]["rating: general"]
        self.assertAlmostEqual(score, 0.6)
        self.assertEqual(source, "wd")                                      # the one surest of the winner
        # each tagger is averaged over the captures first, then the three are averaged
        two = ("wd:a=0.9|rating:general=0.8|prating:general=0.2|erating:general=0.4",
               "wd:a=0.9|rating:general=0.6|prating:general=0.4|erating:general=0.8")
        self.assertAlmostEqual(self.detect(*two)["display"]["rating"]["general"], (0.7 + 0.3 + 0.6) / 3, places=3)
        # the fifth one decides when the two others split
        split = "wd:a=0.9|rating:general=0.6,explicit=0.4|prating:general=0.45,explicit=0.55|erating:general=0.1,explicit=0.9"
        self.assertEqual(list(k for k in self.detect(split)["tags"] if k.startswith("rating")), ["rating: explicit"])
        no_extra = at.detect(raw_of(split), S())                           # extra off: general .525 against explicit .475
        self.assertEqual(no_extra["tags"]["rating: general"], (0.525, "wd"))
        # a tagger that is off, or that gave no rating, does not take part or dilute it
        self.assertEqual(self.detect(*pictures, use_extra=False)["display"]["rating"], {"general": 0.6, "explicit": 0.4})
        self.assertAlmostEqual(self.detect(*pictures, use_wd=False)["display"]["rating"]["general"], 0.45)
        silent = self.detect("wd:a=0.9|rating:general=0.9|extra:b=0.9")             # extra and pixai said nothing
        self.assertEqual(silent["tags"]["rating: general"], (0.9, "wd"))
        # an exact tie goes to the first registered tagger
        tie = self.detect("wd:a=0.9|rating:general=0.6,sensitive=0.4|prating:general=0.6,sensitive=0.4|"
                          "erating:general=0.6,sensitive=0.4")
        self.assertAlmostEqual(tie["tags"]["rating: general"][0], 0.6)
        self.assertEqual(tie["tags"]["rating: general"][1], "wd")

    def test_a_tagger_without_a_rating_is_left_out_of_the_rating(self):
        at.configure_taggers([*at.TAGGERS[:4], EXTRA_NO_RATING])      # this test's fake reports no rating (setUp restores)
        self.assertFalse(at.TAGGERS[-1].has_rating)
        raw = at.raw_from_results([read_picture(b"wd:a=0.9|rating:general=0.8|extra:b=0.9|erating:explicit=0.99")[0]], [None])
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.8}, "pixai": None, "ram": None, "e621": None,
                                           "extra": None}])                                  # never stored
        got = at.detect(raw, S(use_extra=True))
        self.assertEqual(got["display"]["rating"], {"general": 0.8})
        self.assertEqual(got["tags"]["b"], (0.9, "extra"))                                      # its tags still count

    def test_the_highest_score_wins_and_the_source_is_that_tagger(self):
        got = self.detect("wd:smile=0.7,a=0.6|pixai:smile=0.8|extra:smile=0.9,a=0.8|ram:smile=0.85|rating:general=0.9")["tags"]
        self.assertEqual((got["smile"], got["a"]), ((0.9, "extra"), (0.8, "extra")))
        got = self.detect("wd:smile=0.95|pixai:smile=0.8|extra:smile=0.9|ram:smile=0.9|e621:smile=0.9|rating:general=0.9")["tags"]
        self.assertEqual(got["smile"], (0.95, "wd"))
        got = self.detect("ram:smile=0.9|extra:smile=0.9|rating:general=0.9")["tags"]               # a tie: the earlier one
        self.assertEqual(got["smile"], (0.9, "ram"))
        got = self.detect("e621:smile=0.9|extra:smile=0.9|rating:general=0.9")["tags"]
        self.assertEqual(got["smile"], (0.9, "e621"))

    def test_character_tags_gate_the_fifth_taggers_character_category(self):
        picture = "wd:girl=0.9|extra:smile=0.9|echar:hatsune_miku=0.9|ram:sea=0.9|e6char:fenrir=0.9|rating:general=0.9"
        self.assertIn("hatsune miku", self.detect(picture)["tags"])
        got = self.detect(picture, character_tags=False)
        self.assertNotIn("hatsune miku", got["tags"])
        self.assertNotIn("fenrir", got["tags"])
        self.assertIn("smile", got["tags"])
        self.assertIn("sea", got["tags"])                                   # RAM++ has none to switch off
        self.assertNotIn("hatsune miku", {t["tag"] for t in got["display"]["extra"]})

    def test_a_fifth_taggers_noise_words_are_its_own(self):
        at.configure_taggers([*at.TAGGERS[:4], EXTRA_NOISY])
        picture = ("extra:filler=0.9,Filler=0.8,sea=0.9,image=0.9,mammal=0.9|ram:filler=0.9,image=0.9,sea=0.8|"
                   "e621:filler=0.7,mammal=0.99|rating:general=0.9")
        got = self.detect(picture, rating_tag=False)
        self.assertEqual({t: v[1] for t, v in got["tags"].items()},
                         {"sea": "extra", "image": "extra", "filler": "ram", "mammal": "extra"})   # Hydra's "mammal" is still noise
        self.assertEqual({t["tag"] for t in got["display"]["extra"]}, {"sea", "image", "mammal"})
        self.assertEqual({t["tag"] for t in got["display"]["ram"]}, {"filler", "sea"})
        self.assertEqual({t["tag"] for t in got["display"]["e621"]}, {"filler"})

    def test_the_preview_has_a_list_for_every_registered_tagger(self):
        picture = "wd:girl=0.9,hat=0.3|pixai:beach=0.8|ram:r=0.9|extra:sea=0.7,wave=0.3,gone=0.1|rating:general=0.9"
        shown = self.detect(picture)["display"]
        self.assertEqual(set(shown), {"wd", "pixai", "ram", "e621", "extra", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in shown["extra"]}, {"sea": True, "wave": False})   # from 0.2 up
        self.assertEqual([t["tag"] for t in shown["wd"]], ["girl", "hat"])
        off = at.detect(raw_of(picture), S())["display"]                                   # extra is off: an empty list
        self.assertEqual((off["extra"], set(off)), ([], {"wd", "pixai", "ram", "e621", "extra", "rating"}))

    def test_stored_scores_keep_every_taggers_categories_and_rating(self):
        raw = raw_of("wd:girl=0.9|char:miku=0.8|rating:general=0.9|pixai:smile=0.7|pchar:rin=0.6|copy:vocaloid=0.8|"
                     "prating:general=0.8|ram:lake=0.6|e621:anthro=0.9|e6species:wolf=0.8|"
                     "extra:sea=0.7|echar:luka=0.6|erating:general=0.7,explicit=0.2")
        self.assertEqual(raw["scores"], [{"wd": {"general": {"girl": 0.9}, "character": {"miku": 0.8}},
                                          "pixai": {"general": {"smile": 0.7}, "character": {"rin": 0.6},
                                                    "copyright": {"vocaloid": 0.8}},
                                          "ram": {"general": {"lake": 0.6}},
                                          "e621": {"general": {"anthro": 0.9}, "species": {"wolf": 0.8}, "character": {},
                                                   "copyright": {}},
                                          "extra": {"general": {"sea": 0.7}, "character": {"luka": 0.6}}}])
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.9}, "pixai": {"general": 0.8}, "ram": None, "e621": None,
                                           "extra": {"general": 0.7, "explicit": 0.2}}])
        self.assertEqual(raw["models"], ["e621", "extra", "pixai", "ram", "wd"])
        self.store.save_raw("a", raw)
        self.assertEqual(self.store.raw("a"), raw)
        self.assertTrue(at.has_kind(raw, "extra"))

    def test_a_stored_raw_row_lacking_an_enabled_tagger_is_incomplete(self):
        old = raw_of("wd:a=0.9|pixai:b=0.9|rating:general=0.9")
        old["scores"] = [{k: v for k, v in cap.items() if k != "extra"} for cap in old["scores"]]     # made before extra
        self.assertEqual(at.missing_kinds(old, S()), [])                                     # extra is off: nothing missing
        self.assertEqual(at.missing_kinds(old, S(use_extra=True)), ["extra"])
        self.assertEqual(at.missing_kinds(old, S(use_extra=True, use_wd=False, use_pixai=False, use_ram=False)), ["extra"])
        self.assertEqual(at.missing_kinds(None, S()), REAL)
        self.assertEqual(at.missing_kinds(None, S(use_extra=True)), [*REAL, "extra"])
        self.assertEqual(at.missing_kinds(raw_of("wd:a=0.9|extra:b=0.9"), S(use_extra=True)), [])
        self.assertEqual(at.missing_kinds(raw_of("wd:a=0.9"), S(use_wd=False, use_pixai=False, use_ram=False, use_e621=False)), [])

    def test_only_the_enabled_taggers_are_asked_for_and_stored(self):
        self.set(use_extra=True)
        self.run_indexer()
        self.assertEqual(set(map(tuple, self.services.tag_models)), {(*REAL, "extra")})
        self.assertTrue(at.has_kind(self.store.raw(self.ids[0]), "extra"))
        self.services.tag_models.clear()
        self.store.enqueue(self.ids[:2], "full")
        self.run_indexer(use_pixai=False, use_extra=False)
        self.assertEqual(set(map(tuple, self.services.tag_models)), {("wd", "ram", "e621")})
        self.assertFalse(at.has_kind(self.store.raw(self.ids[0]), "pixai"))
        self.assertFalse(at.has_kind(self.store.raw(self.ids[0]), "extra"))

    EXTRA_PHOTO = PHOTO + "|extra:lake=0.9,sea=0.95|erating:general=0.9"

    def test_the_fifth_tagger_takes_part_end_to_end(self):
        self.catalog[0]["preview"] = self.EXTRA_PHOTO
        self.run_indexer(use_extra=True)
        self.assertEqual(self.description(0),
                         "[AI Tagger]\nTags: sea, girl, lake, beach, miku, solo, rating: general\n[/AI Tagger]")
        by = {t["tag"]: t for t in self.store.result(self.ids[0])["tags"]}
        self.assertEqual((by["sea"]["source"], by["lake"]["source"], by["girl"]["source"], by["beach"]["source"]),
                         ("extra", "extra", "wd", "pixai"))
        self.assertEqual(by["sea"]["score"], 0.95)                          # the highest of pixai's .7 and extra's .95
        self.assertEqual(self.store.raw(self.ids[0])["models"], ["e621", "extra", "pixai", "ram", "wd"])
        self.assertEqual(self.description(1), "[AI Tagger]\nTags: dog, grass, rating: general\n[/AI Tagger]")

    def test_enabling_it_later_makes_a_retag_a_full_reprocess(self):
        self.run_indexer()                                                  # extra is off
        self.assertFalse(at.has_kind(self.store.raw(self.ids[0]), "extra"))
        self.services.tag_calls.clear()
        self.services.tag_models.clear()
        self.assertEqual(self.set(use_extra=True), ["use_extra"])
        self.assertEqual(at.reprocess(self.store, "outdated", "retag"), 3)  # only a retag was asked for ...
        self.run_indexer()
        self.assertEqual(sum(self.services.tag_calls), 1 + 1 + 3)           # ... but the stored scores lack an enabled tagger
        self.assertEqual({tuple(m) for m in self.services.tag_models}, {(*REAL, "extra")})
        self.assertTrue(at.has_kind(self.store.raw(self.ids[0]), "extra"))
        self.services.tag_calls.clear()
        self.assertEqual(self.set(extra_strictness=0.6), ["extra_strictness"])
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])                       # complete now: a retag is cheap again

    def test_a_retag_with_all_five_stored_needs_no_gpu(self):
        self.catalog[0]["preview"] = self.EXTRA_PHOTO
        self.run_indexer(use_extra=True)
        self.services.tag_calls.clear()
        self.set(extra_strictness=0.95)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])
        names = {t["tag"]: t["source"] for t in self.store.result(self.ids[0])["tags"]}
        self.assertNotIn("lake", names)                                     # extra's .9 is below its .95 now
        self.assertEqual(names["sea"], "extra")                             # its .95 still passes
        self.set(extra_strictness=0.5, use_extra=False)
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])                       # switching it off applies to the stored scores too
        names = {t["tag"]: t["source"] for t in self.store.result(self.ids[0])["tags"]}
        self.assertEqual((names.get("lake"), names["sea"]), (None, "pixai"))

    def test_agreement_by_any_two_end_to_end(self):
        picture = "wd:door=0.9,oral=0.9|pixai:door=0.9|extra:oral=0.9|rating:general=0.9|erating:general=0.9"
        self.catalog = [{**self.catalog[0], "preview": picture}]
        self.run_indexer(use_extra=True)
        self.assertIn("oral", self.description(0))                          # WD and extra agree
        self.set(use_extra=False)
        at.reprocess(self.store, "outdated", "retag")
        calls = list(self.services.tag_calls)
        self.run_indexer()
        self.assertNotIn("oral", self.description(0))                       # only WD is left that saw it
        self.assertEqual(self.services.tag_calls, calls)                    # (the stored scores were enough)

    def test_the_preview_lists_all_five_taggers(self):
        self.catalog[0]["preview"] = self.EXTRA_PHOTO.replace("lake=0.9", "lake=0.9,wave=0.3")
        self.set(use_extra=True)
        got = self.indexer.test(self.ids[0])
        self.assertEqual(set(got["models"]), {"wd", "pixai", "ram", "e621", "extra", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in got["models"]["extra"]}, {"sea": True, "lake": True, "wave": False})
        self.assertEqual(self.services.tag_models, [[*REAL, "extra"]])
        self.assertEqual((got["models"]["rating"]["general"], got["models"]["rating"]["sensitive"]), (0.9, 0.04))
        self.assertEqual(self.services.ready_calls[0], {"tagger": True})
        self.assertIn("sea", {t["tag"] for t in got["tags"]})

    def test_taking_it_out_again_leaves_the_real_four_working(self):
        self.set(use_extra=True)
        self.run_indexer()
        at.configure_taggers(at.TAGGERS[:4])                                # as if it had never been added
        settings = at.load_settings()
        self.assertNotIn("use_extra", settings)
        self.assertEqual(at.missing_kinds(self.store.raw(self.ids[0]), settings), [])        # its stored scores are just ignored
        built = at.build(self.store.raw(self.ids[0]), settings)
        self.assertEqual(tags_of(built), ["girl", "beach", "miku", "solo", "sea", "rating: general"])
        self.assertEqual(set(built["models"]), {"wd", "pixai", "ram", "e621", "rating"})


class TestFinalTags(unittest.TestCase):
    def build(self, pictures=(PHOTO,), **settings):
        return at.build(raw_of(*pictures), S(**settings))

    def test_the_rules_run_on_the_detected_tags_and_blocked_wins_over_everything(self):
        rules = [rule(if_all=["girl"], add=["happy", "cat"]), rule(if_all=["happy"], remove=["solo"])]
        got = self.build(rules=rules, blocked=["cat"])
        names = tags_of(got)
        self.assertIn("happy", names)
        self.assertNotIn("cat", names)                           # added by a rule, blocked afterwards
        self.assertNotIn("solo", names)
        self.assertEqual(got["rules"], [{"rule": 0, "added": ["happy", "cat"], "removed": []},     # what the rule did, before "blocked"
                                        {"rule": 1, "added": [], "removed": ["solo"]}])
        by = {t["tag"]: t for t in got["tags"]}
        self.assertEqual(by["happy"], {"tag": "happy", "score": 1.0, "source": "rule"})

    def test_the_cap_keeps_the_best_and_always_the_rating(self):
        pic = "wd:" + ",".join(f"t{i:02d}={0.99 - i * 0.01:.2f}" for i in range(20)) + "|rating:general=0.6,sensitive=0.4"
        got = self.build(pictures=(pic,), max_tags=5)
        names = tags_of(got)
        self.assertEqual(names, ["t00", "t01", "t02", "t03", "rating: general"])         # 4 + the pinned rating
        self.assertEqual(len(self.build(pictures=(pic,), max_tags=5, rating_tag=False)["tags"]), 5)

    def test_order_is_by_score_then_name_with_the_rating_last(self):
        got = self.build()
        self.assertEqual(tags_of(got), ["girl", "beach", "miku", "solo", "sea", "rating: general"])
        self.assertEqual([t["score"] for t in got["tags"]], [0.9, 0.8, 0.8, 0.8, 0.7, 0.9])

    def test_the_block_is_tags_only(self):
        got = self.build()
        self.assertEqual(got["block"], "[AI Tagger]\nTags: girl, beach, miku, solo, sea, rating: general\n[/AI Tagger]")
        self.assertEqual(set(got), {"tags", "models", "rules", "block"})            # no description, no describer answer
        self.assertNotIn("Description", got["block"])
        self.assertEqual(self.build(pictures=("wd:dull=0.1",), rating_tag=False)["block"], "")     # nothing to say
        self.assertEqual(at.finalize({}, S())[0], [])
        self.assertFalse(hasattr(at, "VLM_ADD_SCORE") or hasattr(at, "VLM_PROTECT"))


# ---------------------------------------------------------------- the block in the description

BLOCK = "[AI Tagger]\nTags: girl, beach\n[/AI Tagger]"
BLOCK2 = "[AI Tagger]\nTags: dog\n[/AI Tagger]"
V2_BLOCK = "[AI Tagger]\nTags: girl, beach\nDescription: A girl on a beach.\n[/AI Tagger]"       # what v2 wrote


class TestBlock(unittest.TestCase):
    def test_compose(self):
        self.assertEqual(at.compose_block(["girl", "beach", "rating: general"]),
                         "[AI Tagger]\nTags: girl, beach, rating: general\n[/AI Tagger]")
        self.assertEqual(at.compose_block(["dog"]), BLOCK2)
        self.assertEqual(at.compose_block([]), "")
        self.assertEqual(at.compose_block(["a", "b   c"]), "[AI Tagger]\nTags: a, b c\n[/AI Tagger]")
        self.assertEqual(len(at.compose_block(["a", "b"]).splitlines()), 3)             # no Description line

    def test_a_tag_cannot_forge_the_markers(self):
        block = at.compose_block(["a", "x [/AI Tagger] and [AI Tagger] y"])
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

    def test_a_v2_block_with_a_description_line_is_rewritten_without_it(self):
        self.assertEqual(at.merge_description("Mine\n\n" + V2_BLOCK, BLOCK), "Mine\n\n" + BLOCK)
        self.assertEqual(at.merge_description(V2_BLOCK + "\nAfter.", BLOCK), BLOCK + "\nAfter.")
        self.assertEqual(at.strip_block("Mine\n\n" + V2_BLOCK), "Mine")                  # and it is still taken out cleanly

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
        self.assertEqual(body["floor"], 0.2)                             # at 0.05 RAM++ alone sent ~4,400 tags per picture
        self.assertEqual(body["floor"], at.FLOOR)
        self.assertNotIn("models", body)                                 # no list given: the service runs all of them
        self.assertEqual(client.health()["status"], "ok")

    def test_the_models_to_run_are_sent_when_given(self):
        server = self.serve({("POST", "/tag"): lambda b: (200, {"results": [{"wd": {"general": {}, "character": {}}}],
                                                                "errors": [None], "tookMs": 1})})
        at.Tagger(server.url).tag([b"x"], models=["wd", "ram"])
        (_, _, body), = [r for r in server.requests if r[1] == "/tag"]
        self.assertEqual(body["models"], ["wd", "ram"])
        at.Tagger(server.url).tag([b"x"], models=["e621"])
        self.assertEqual([r[2]["models"] for r in server.requests if r[1] == "/tag"], [["wd", "ram"], ["e621"]])
        self.assertTrue(all(r[2]["floor"] == 0.2 for r in server.requests if r[1] == "/tag"))

    def test_the_answer_of_the_service_with_ram_and_hydra_goes_through_as_it_is(self):
        # the service's /tag answer: RAM++ nested under "general", Hydra under its four categories, neither with a
        # rating (docs/AI-TAGGER.md, v3)
        hydra = {"general": {"anthro": 0.93, "mammal": 0.99}, "species": {"wolf": 0.7}, "character": {},
                 "copyright": {"norse mythology": 0.4}}
        answer = {"results": [{"wd": {"general": {"a": 0.9}, "character": {}, "rating": {"general": 1.0}},
                               "pixai": {"general": {"b": 0.8}, "character": {}, "copyright": {}, "rating": {"general": 0.9}},
                               "ram": {"general": {"screenshot": 0.88, "text message": 0.62}}, "e621": hydra}, None],
                  "errors": [None, "cannot identify image file"], "tookMs": 412}
        server = self.serve({("POST", "/tag"): lambda b: (200, answer)})
        results, errors = at.Tagger(server.url).tag([b"x", b"y"], models=["wd", "pixai", "ram", "e621"])
        self.assertEqual(results[0]["ram"], {"general": {"screenshot": 0.88, "text message": 0.62}})
        self.assertEqual(results[0]["e621"], hydra)
        raw = at.raw_from_results(results, errors)
        self.assertEqual(raw["scores"][0]["ram"], {"general": {"screenshot": 0.88, "text message": 0.62}})
        self.assertEqual(raw["scores"][0]["e621"], hydra)
        self.assertEqual(raw["models"], ["e621", "pixai", "ram", "wd"])
        self.assertEqual(raw["ratings"], [{"wd": {"general": 1.0}, "pixai": {"general": 0.9}, "ram": None, "e621": None}])
        # an old service that does not know Hydra answers without it: no entry, so it counts as missing
        old = at.raw_from_results([{"wd": {"general": {"a": 0.9}, "character": {}, "rating": {"general": 1.0}},
                                    "pixai": {"general": {}, "character": {}, "copyright": {}, "rating": {}},
                                    "ram": {"general": {}}}], [None])
        self.assertEqual((old["models"], at.missing_kinds(old, S())), (["pixai", "ram", "wd"], ["e621"]))

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

    def test_a_dead_graphics_card_is_the_taggers_failure_not_the_pictures(self):
        # 2026-10-02: after "CUDA failure 999" every picture failed instantly and 9,408 assets were marked failed
        server = self.serve({("POST", "/tag"): lambda b: (200, {"results": [None, None], "errors": ['wd: Fail: [ONNXRuntimeError] : 1 : FAIL : CUDA failure 999: unknown error ; GPU=0 ; file=/onnxruntime_src/onnxruntime/core/providers/cuda/gpu_data_transfer.cc', 'wd: Fail: [ONNXRuntimeError] : 1 : FAIL : CUDA failure 999: unknown error ; GPU=0 ; file=/onnxruntime_src/onnxruntime/core/providers/cuda/gpu_data_transfer.cc']})})
        with self.assertRaises(at.GpuBroken) as ctx:
            at.Tagger(server.url).tag([b"x", b"y"])
        self.assertIsInstance(ctx.exception, sp.ServiceDown)          # handled like a server that went away
        self.assertIn("CUDA failure 999", str(ctx.exception))
        for text in ("RuntimeError: CUDA error: an illegal memory access was encountered",
                     "CUBLAS_STATUS_EXECUTION_FAILED", "device-side assert triggered"):
            self.assertTrue(at._is_gpu_broken(text), text)
        for text in ("cannot identify image file", "invalid base64", "CUDA out of memory", None, ""):
            self.assertFalse(at._is_gpu_broken(text), text)            # a bad file, or what halving handles
        server = self.serve({("POST", "/tag"): lambda b: (200, {"results": [None], "errors": ["CUDA out of memory"]})})
        with self.assertRaises(at.GpuOOM):                              # out of memory stays out of memory
            at.Tagger(server.url).tag([b"x"])
        server = self.serve({("POST", "/tag"): lambda b: (200, {"results": [None], "errors": ["cannot identify image file"]})})
        self.assertEqual(at.Tagger(server.url).tag([b"x"])[1], ["cannot identify image file"])   # still the picture's

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


class TestTheDescriberIsGone(unittest.TestCase):
    """v3 removed the describer: no client, prompt, schema, guards, settings, container or status section."""

    def test_nothing_of_it_is_left_in_the_module(self):
        for name in ("VLM", "VLMRejected", "VLM_SCHEMA", "VLM_SYSTEM", "VLM_UTIL", "VLM_ADD_SCORE", "VLM_PROTECT", "VLM_URL",
                     "VLM_MODEL", "VLM_CONTAINER", "VLM_SERVICE", "vlm_prompt", "parse_vlm_answer", "needs_vlm",
                     "has_pixai", "MODEL_LABELS", "IDLE_EXIT_MINUTES", "_watch"):
            self.assertFalse(hasattr(at, name), name)
        self.assertFalse(hasattr(sp, "AITAGGER_VLM_CONTAINER"))
        for gone in ("describe", "instructions", "language", "vlm_parallel"):
            self.assertNotIn(gone, at.DEFAULTS)
            self.assertNotIn(gone, at.LIMITS)
            self.assertNotIn(gone, at.CONTENT)
        self.assertNotIn("describe", at.REPROCESS)
        for method in ("describe", "run_vlm", "decide"):
            self.assertFalse(hasattr(at.Pipeline, method), method)
        self.assertFalse(hasattr(at.Services, "describe"))
        self.assertFalse(hasattr(at.Services, "idle_check"))

    def test_a_result_has_no_description_or_describer_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = at.Store(Path(tmp))
            try:
                store.save_result("a", tags=[{"tag": "dog", "score": 0.9, "source": "wd"}], block=BLOCK2, version=1)
                self.assertEqual(set(store.result("a")), {"tags", "block", "settings_version", "processed_at", "written_at"})
                self.assertEqual({r[1] for r in store.conn.execute("pragma table_info(results)")},
                                 {"id", "tags_json", "block", "settings_version", "processed_at", "written_at"})
            finally:
                store.conn.close()


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
        self.store.enqueue([self.ids[4]], "retag")
        self.store.enqueue([self.ids[2]], "full")
        self.assertEqual([w["id"] for w in self.store.work(2)], [self.ids[4], self.ids[2]])

    def test_a_stronger_mode_replaces_a_weaker_one_for_the_same_asset(self):
        a = self.ids[0]
        self.store.enqueue([a], "retag")
        self.store.enqueue([a], "full")
        self.assertEqual([q["mode"] for q in self.store.queue()], ["full"])
        self.store.enqueue([a], "retag")                               # weaker: ignored
        self.assertEqual([q["mode"] for q in self.store.queue()], ["full"])
        self.assertEqual(self.store.enqueue([self.ids[1], self.ids[1], self.ids[2]], "retag"), 2)
        for bad in ("everything", "", None):
            with self.assertRaises(ValueError):
                self.store.enqueue([a], bad)

    def test_describe_is_a_retag_in_the_queue(self):
        a, b = self.ids[0], self.ids[1]
        self.assertEqual(self.store.enqueue([a], "describe"), 1)         # an old app asks for it: it is a retag
        self.assertEqual(self.store.queue()[0]["mode"], "retag")
        self.store.enqueue([a], "full")
        self.store.enqueue([a], "describe")                              # a weaker request never lowers a full one
        self.assertEqual([q["mode"] for q in self.store.queue()], ["full"])
        self.store.sync_catalog(self.catalog)
        self.store.save_result(b, tags=[], block="", version=1, written=True)
        self.assertEqual(at.reprocess(self.store, "ids", "describe", ids=[b]), 1)
        self.assertEqual({q["id"]: q["mode"] for q in self.store.queue()}[b], "retag")
        self.assertEqual(at.reprocess(self.store, "all", "describe"), 1)

    def test_a_describe_row_left_in_the_queue_by_v2_becomes_a_retag(self):
        folder = Path(self.tmp.name) / "v2queue"
        old = at.Store(folder)
        old.conn.execute("insert into queue values (?,?,?)", (self.ids[0], "describe", "2026-09-30T10:00:00+00:00"))
        old.conn.execute("insert into queue values (?,?,?)", (self.ids[1], "full", "2026-09-30T10:00:01+00:00"))
        old.conn.commit()
        old.conn.close()
        store = at.Store(folder)                                          # opening it fixes the row
        self.addCleanup(store.conn.close)
        self.assertEqual({q["id"]: q["mode"] for q in store.queue()}, {self.ids[0]: "retag", self.ids[1]: "full"})
        store.conn.execute("insert or replace into queue values (?,?,?)", (self.ids[2], "describe", "2026-09-30T10:00:02+00:00"))
        store.conn.commit()
        store.sync_catalog(self.catalog)
        self.assertEqual(store.queue()[-1]["mode"], "describe")           # one that got in some other way ...
        store.dequeue(self.ids[2], "retag")                               # ... is still understood: a retag covers it
        self.assertNotIn(self.ids[2], [q["id"] for q in store.queue()])

    def test_dequeue_only_when_the_work_done_covers_what_was_asked(self):
        a = self.ids[0]
        self.store.enqueue([a], "full")
        self.store.dequeue(a, "retag")
        self.assertEqual(len(self.store.queue()), 1)
        self.store.dequeue(a, "full")
        self.assertEqual(self.store.queue(), [])
        self.store.enqueue([a], "retag")
        self.store.dequeue(a, "full")
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
        self.assertEqual(raw["models"], ["e621", "pixai", "ram", "wd"])
        self.assertEqual(raw["scores"][0]["ram"], {"general": {}})                     # RAM++ answered, with nothing to say
        self.assertEqual(raw["scores"][0]["e621"], {"general": {}, "species": {}, "character": {}, "copyright": {}})
        self.assertIsNone(self.store.raw("b"))

    def test_raw_keeps_the_pixai_categories_and_both_ratings(self):
        raw = raw_of("wd:girl=0.9|char:miku=0.8|rating:general=0.9|pixai:smile=0.7|pchar:rin=0.6|copy:vocaloid=0.8|"
                     "prating:general=0.8,explicit=0.2")
        self.assertEqual(raw["scores"], [{"wd": {"general": {"girl": 0.9}, "character": {"miku": 0.8}},
                                          "pixai": {"general": {"smile": 0.7}, "character": {"rin": 0.6},
                                                    "copyright": {"vocaloid": 0.8}},
                                          "ram": {"general": {}},
                                          "e621": {"general": {}, "species": {}, "character": {}, "copyright": {}}}])
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.9}, "pixai": {"general": 0.8, "explicit": 0.2}, "ram": None,
                                           "e621": None}])
        self.store.save_raw("a", raw)
        self.assertEqual(self.store.raw("a"), raw)
        self.assertTrue(at.has_kind(raw, "pixai"))
        self.assertFalse(at.has_kind(self.store.raw("nothing"), "pixai"))
        only_wd = at.raw_from_results([{"wd": {"general": {"a": 0.9}, "character": {}, "rating": {"general": 1.0}}}], [None])
        self.assertEqual((only_wd["models"], at.has_kind(only_wd, "wd"), at.has_kind(only_wd, "pixai")), (["wd"], True, False))
        self.assertEqual(only_wd["scores"], [{"wd": {"general": {"a": 0.9}, "character": {}}, "pixai": None, "ram": None,
                                              "e621": None}])
        self.assertFalse(at.has_kind(only_wd, "ram"))
        self.assertFalse(at.has_kind(only_wd, "e621"))

    def v1_store(self, ids):
        """A store as the RAM++ panel left it: written results with stored scores that have no PixAI entry."""
        folder = Path(self.tmp.name) / "v1"
        old = at.Store(folder)
        old.sync_catalog(self.catalog)
        v1_scores = [{"wd": {"general": {"girl": 0.9}, "character": {}}, "ram": {"beach": 0.8}}]
        for i in ids:
            old.conn.execute("insert into raw values (?,?,?,?,?,?)", (self.ids[i], 1, json.dumps(v1_scores),
                                                                       json.dumps([{"general": 0.9}]), "2026-09-30T10:00:00+00:00", "ram,wd"))
            old.save_result(self.ids[i], tags=[{"tag": "girl", "score": 0.9, "source": "wd"}], block="B", version=1)
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
        self.assertFalse(at.has_kind(store.raw(self.ids[0]), "pixai"))
        raw = store.raw(self.ids[0])
        self.assertEqual(raw["scores"][0]["ram"], {"beach": 0.8})            # kept as v1 wrote it (flat) ...
        self.assertEqual(at.detect(raw, S(use_pixai=False))["tags"]["beach"], (0.8, "ram"))      # ... and read as RAM++'s tags
        self.assertEqual(at.missing_kinds(raw, S()), ["pixai", "e621"])       # with PixAI and Hydra on, a retag becomes a full
        store.conn.close()
        again = at.Store(folder)                                            # opening it again does not bump it again
        self.addCleanup(again.conn.close)
        self.assertEqual(again.settings_version, 2)
        self.assertEqual(again.meta("raw_format"), at.RAW_FORMAT)

    OLD_RESULTS = ("create table results (id text primary key, tags_json text, vlm_json text, description text, block text,"
                   " settings_version integer, processed_at text, written_at text, note text)")

    def v2_store(self, blocks, taggers="wd,pixai,ram,e621"):
        """A store as the describer version left it: results with the old columns, whose blocks are ``blocks``. It knows
        the registry as it is now (``taggers``, the four real ones), so that only the block format decides whether it is
        bumped; ``taggers`` None is a store from before the registry was remembered (then it had wd and pixai)."""
        folder = Path(self.tmp.name) / "v2"
        folder.mkdir()
        conn = sqlite3.connect(str(folder / "tagger.sqlite"))
        conn.execute(self.OLD_RESULTS)
        conn.execute("create table meta (key text primary key, value text)")
        conn.execute("insert into meta values ('raw_format', ?)", (at.RAW_FORMAT,))      # not from before v2
        if taggers is not None:
            conn.execute("insert into meta values ('taggers', ?)", (taggers,))
        for i, block in enumerate(blocks):
            conn.execute("insert into results values (?,?,?,?,?,?,?,?,?)",
                         (self.ids[i], json.dumps([{"tag": "girl", "score": 0.9, "source": "wd"}]),
                          json.dumps({"description": "A girl.", "add_tags": [], "remove_tags": []}), "A girl.", block, 1,
                          "2026-09-30T10:00:00+00:00", "2026-09-30T10:00:01+00:00", ""))
        conn.commit()
        conn.close()
        return folder

    def test_results_with_a_description_line_count_as_outdated_once(self):
        folder = self.v2_store([V2_BLOCK, BLOCK2, V2_BLOCK])
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual(store.settings_version, 2)                         # bumped once: the version 1 results are older
        self.assertEqual(store.meta("block_format"), at.BLOCK_FORMAT)
        store.sync_catalog(self.catalog)
        self.assertEqual(store.counts()["outdated"], 3)
        self.assertEqual(set(store.scope_ids("outdated")), set(self.ids[:3]))
        store.conn.close()
        again = at.Store(folder)                                            # opening it again does not bump it again
        self.addCleanup(again.conn.close)
        self.assertEqual((again.settings_version, again.meta("block_format")), (2, at.BLOCK_FORMAT))
        # a result written afterwards (made with the current version) is not outdated, and the old columns do not matter
        again.save_result(self.ids[0], tags=[{"tag": "dog", "score": 0.9, "source": "wd"}], block=BLOCK2, version=2, written=True)
        self.assertEqual(again.result(self.ids[0])["block"], BLOCK2)
        again.sync_catalog(self.catalog)
        self.assertEqual(again.counts()["outdated"], 2)
        self.assertEqual(set(again.scope_ids("outdated")), {self.ids[1], self.ids[2]})
        self.assertEqual(again.list_assets()["items"][0]["tags"], ["dog"])

    def test_a_store_without_a_description_line_anywhere_is_not_bumped(self):
        folder = self.v2_store([BLOCK, BLOCK2, ""])                         # tags only (describe was off), or nothing to say
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual(store.settings_version, 1)
        self.assertEqual(store.meta("block_format"), at.BLOCK_FORMAT)       # but the check is not repeated

    def test_a_tag_that_looks_like_the_old_line_is_not_a_description_line(self):
        # tags are lower case and on the one "Tags:" line, so the marker "\nDescription: " cannot come from a tag
        folder = self.v2_store(["[AI Tagger]\nTags: description: x, y\n[/AI Tagger]"])
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual(store.settings_version, 1)

    def test_a_new_store_is_not_bumped_and_remembers_the_format(self):
        self.assertEqual(self.store.settings_version, 1)
        self.assertEqual((self.store.meta("block_format"), self.store.meta("raw_format")), (at.BLOCK_FORMAT, at.RAW_FORMAT))
        self.assertEqual(self.store.meta("taggers"), "wd,pixai,ram,e621")

    def test_ram_and_hydra_arriving_make_results_made_without_them_outdated_once(self):
        # a store from before RAM++ was registered: the panel remembered "wd,pixai" (or, in v2, nothing: wd and pixai).
        # Both RAM++ and Hydra are on by default: the results lack the tags of both
        for taggers in ("wd,pixai", None):
            with self.subTest(remembered=taggers):
                folder = self.v2_store([BLOCK], taggers=taggers)
                try:
                    store = at.Store(folder)
                    self.assertEqual((store.settings_version, store.meta("taggers")), (2, "wd,pixai,ram,e621"))
                    store.sync_catalog(self.catalog)
                    self.assertEqual(store.counts()["outdated"], 1)
                    store.conn.close()
                    again = at.Store(folder)                                # once
                    self.assertEqual((again.settings_version, again.meta("taggers")), (2, "wd,pixai,ram,e621"))
                    again.conn.close()
                finally:
                    shutil.rmtree(folder, ignore_errors=True)

    def test_hydra_arriving_makes_results_made_without_it_outdated_once(self):
        # the store of the three-tagger panel: it knew wd, pixai and ram (and had no e621 scores)
        folder = self.v2_store([BLOCK], taggers="wd,pixai,ram")
        store = at.Store(folder)                                            # Hydra is on by default: the results lack its tags
        self.addCleanup(store.conn.close)
        self.assertEqual((store.settings_version, store.meta("taggers")), (2, "wd,pixai,ram,e621"))
        store.sync_catalog(self.catalog)
        self.assertEqual(store.counts()["outdated"], 1)
        self.assertEqual(set(store.scope_ids("outdated")), {self.ids[0]})
        store.conn.close()
        again = at.Store(folder)                                            # once
        self.addCleanup(again.conn.close)
        self.assertEqual((again.settings_version, again.meta("taggers")), (2, "wd,pixai,ram,e621"))
        # a result written afterwards is made with the new version: not outdated
        again.save_result(self.ids[0], tags=[{"tag": "dog", "score": 0.9, "source": "e621"}], block=BLOCK2, version=2, written=True)
        again.sync_catalog(self.catalog)
        self.assertEqual(again.counts()["outdated"], 0)

    def test_a_store_that_already_knows_hydra_is_not_bumped_by_it(self):
        folder = self.v2_store([BLOCK], taggers="wd,pixai,ram,e621")
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual((store.settings_version, store.meta("taggers")), (1, "wd,pixai,ram,e621"))
        store.conn.close()
        again = at.Store(folder)
        self.addCleanup(again.conn.close)
        self.assertEqual((again.settings_version, again.meta("taggers")), (1, "wd,pixai,ram,e621"))

    def test_a_store_that_knows_ram_is_not_bumped_by_it(self):
        # the panel as it was before Hydra: three taggers registered, the store knows all of them
        saved = list(at.TAGGERS)
        self.addCleanup(at.configure_taggers, saved)
        at.configure_taggers(saved[:3])
        folder = self.v2_store([BLOCK], taggers="wd,pixai,ram")
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual((store.settings_version, store.meta("taggers")), (1, "wd,pixai,ram"))

    def test_a_fifth_tagger_that_is_on_by_default_makes_old_results_outdated_once(self):
        folder = self.v2_store([BLOCK])                                     # knows wd, pixai, ram and e621
        store = at.Store(folder)                                            # same registry as then: nothing to do
        self.assertEqual((store.settings_version, store.meta("taggers")), (1, "wd,pixai,ram,e621"))
        store.conn.close()
        with_extra(self, EXTRA_ON)                                          # a fifth tagger that is on by default arrives
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual((store.settings_version, store.meta("taggers")), (2, "wd,pixai,ram,e621,extra"))
        store.sync_catalog(self.catalog)
        self.assertEqual(store.counts()["outdated"], 1)
        store.conn.close()
        again = at.Store(folder)                                            # once
        self.addCleanup(again.conn.close)
        self.assertEqual(again.settings_version, 2)

    def test_a_tagger_that_is_off_by_default_or_removed_changes_nothing(self):
        folder = self.v2_store([BLOCK], taggers="wd,pixai,ram,e621")
        with_extra(self)                                                    # EXTRA is off by default: no tags are missing
        store = at.Store(folder)
        self.assertEqual((store.settings_version, store.meta("taggers")), (1, "wd,pixai,ram,e621,extra"))
        store.conn.close()
        at.configure_taggers(list(at.TAGGERS[:4]))                          # and removing one is no reason either
        again = at.Store(folder)
        self.addCleanup(again.conn.close)
        self.assertEqual((again.settings_version, again.meta("taggers")), (1, "wd,pixai,ram,e621"))
        again.conn.close()
        at.configure_taggers(list(at.TAGGERS[:3]))                          # not even Hydra going away again
        gone = at.Store(folder)
        self.addCleanup(gone.conn.close)
        self.assertEqual((gone.settings_version, gone.meta("taggers")), (1, "wd,pixai,ram"))
        gone.conn.close()
        at.configure_taggers(list(at.TAGGERS[:2]))                          # nor RAM++
        last = at.Store(folder)
        self.addCleanup(last.conn.close)
        self.assertEqual((last.settings_version, last.meta("taggers")), (1, "wd,pixai"))

    def test_an_empty_store_opened_with_a_new_tagger_is_not_bumped(self):
        with_extra(self, EXTRA_ON)
        store = at.Store(Path(self.tmp.name) / "fresh")
        self.addCleanup(store.conn.close)
        self.assertEqual(store.settings_version, 1)
        self.assertEqual(store.meta("taggers"), "wd,pixai,ram,e621,extra")

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
        self.store.save_result(a, tags=tags, block="B", version=3)
        got = self.store.result(a)
        self.assertEqual((got["tags"], got["block"], got["settings_version"]), (tags, "B", 3))
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
            self.store.save_result(self.ids[i], tags=[{"tag": t, "score": 1.0, "source": "wd"} for t in tags], block="",
                                   version=version)
            self.store.mark_written(self.ids[i])
        self.set(max_tags=10)                                                  # version 2
        self.set(max_tags=11)                                                  # version 3
        self.assertEqual(set(self.store.scope_ids("all")), set(self.ids[:3]))
        self.assertEqual(set(self.store.scope_ids("outdated")), set(self.ids[:3]))
        self.assertEqual(set(self.store.scope_ids("tag", tag="Girl")), {self.ids[0], self.ids[2]})
        self.assertEqual(self.store.scope_ids("ids", ids=["x", "y", "x"]), ["x", "y"])
        self.assertEqual(self.store.scope_ids("tag", tag="nothing"), [])
        self.store.save_result(self.ids[0], tags=[], block="", version=3)
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
        data = [(0, ["girl", "beach"]), (1, ["dog", "grass"]), (2, ["dog", "car", "100%_real"])]
        for i, tags in data:
            self.store.save_result(self.ids[i], tags=[{"tag": t, "score": 1.0, "source": "wd"} for t in tags], block="",
                                   version=1)
            self.store.mark_written(self.ids[i])
        self.set(max_tags=7)
        everything = self.store.list_assets()
        self.assertEqual(everything["total"], 3)
        self.assertEqual([i["id"] for i in everything["items"]], self.ids[:3])                  # newest first
        self.assertEqual(everything["items"][0]["tags"], ["girl", "beach"])
        self.assertEqual(set(everything["items"][0]), {"id", "name", "type", "taken", "tags", "settingsVersion", "processedAt"})
        self.assertEqual(everything["items"][0]["settingsVersion"], 1)
        self.assertEqual(everything["tags"][0], {"tag": "dog", "count": 2})
        self.assertEqual(len(everything["tags"]), 6)
        self.assertEqual([i["id"] for i in self.store.list_assets(tag="dog")["items"]], self.ids[1:3])
        self.assertEqual([i["id"] for i in self.store.list_assets(q="BEACH")["items"]], [self.ids[0]])     # in the tags
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
            self.store.save_result(a, tags=[{"tag": "dog", "score": 1.0, "source": "wd"}], block="", version=1)
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
            self.store.save_result(self.ids[i], tags=[], block=BLOCK, version=1)
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
            self.store.save_result(i, tags=[], block="", version=1)
        self.put_description(0, BLOCK)
        self.fake.fail_next[f"/api/assets/{a}"] = 1
        got = self.pipe.remove([a, b], exclude=True)
        self.assertEqual((got["removed"], got["excluded"], [f["id"] for f in got["failed"]]), (1, 2, [a]))
        self.assertIsNotNone(self.store.result(a))
        self.assertIsNone(self.store.result(b))


# ---------------------------------------------------------------- the indexer, end to end

class TestIndexer(Base):
    def test_tags_and_writes_everything(self):
        self.run_indexer()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        c = self.store.counts()
        self.assertEqual((c["assets"], c["processed"], c["pending"], c["failed"]), (5, 3, 0, 2))
        self.assertEqual(self.description(0), "[AI Tagger]\nTags: girl, beach, miku, solo, sea, rating: general\n[/AI Tagger]")
        self.assertEqual(self.description(1), "[AI Tagger]\nTags: dog, grass, rating: general\n[/AI Tagger]")
        self.assertEqual(self.description(2), "[AI Tagger]\nTags: dog, car, rating: general\n[/AI Tagger]")
        self.assertEqual(self.description(3), "")
        for i in range(3):
            self.assertNotIn("Description", self.description(i))                      # tags only
        res = self.store.result(self.ids[0])
        self.assertEqual(res["settings_version"], 1)
        self.assertIsNotNone(res["written_at"])
        self.assertEqual(res["tags"][0], {"tag": "girl", "score": 0.9, "source": "wd"})
        self.assertEqual(self.store.raw(self.ids[2])["captures"], 3)
        self.assertEqual(self.services.ready_calls[0], {"tagger": True})
        self.assertEqual(sum(self.services.tag_calls), 1 + 1 + 3 + 1)                # the unreadable file never got to the tagger
        self.assertEqual({tuple(m) for m in self.services.tag_models}, {tuple(REAL)})     # the enabled ones are asked for

    def test_only_the_enabled_taggers_are_asked_for_and_stored(self):
        self.run_indexer(use_pixai=False, use_ram=False, use_e621=False)
        self.assertEqual({tuple(m) for m in self.services.tag_models}, {("wd",)})
        raw = self.store.raw(self.ids[0])
        self.assertEqual([at.has_kind(raw, key) for key in REAL], [True, False, False, False])
        self.assertEqual(raw["models"], ["wd"])
        self.assertNotIn("sea", self.description(0))                                 # a PixAI-only tag
        self.assertIn("girl", self.description(0))
        self.services.tag_models.clear()
        self.store.enqueue(self.ids[:1], "full")
        self.run_indexer(use_wd=False, use_pixai=True)
        self.assertEqual({tuple(m) for m in self.services.tag_models}, {("pixai",)})
        self.assertIn("sea", self.description(0))
        self.assertNotIn("girl", self.description(0))

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

    def test_the_owners_text_is_kept_while_tagging(self):
        self.put_description(0, "Holiday in Spain.")
        self.run_indexer()
        self.assertEqual(self.description(0).split("\n\n")[0], "Holiday in Spain.")
        self.assertIn("[AI Tagger]", self.description(0))

    def test_a_dead_graphics_card_is_not_the_assets_fault(self):
        real, calls = self.services.tag, []

        def broken_once(images, models=None):
            calls.append(len(images))
            if len(calls) == 1:
                raise at.GpuBroken("the tagger lost the graphics card: CUDA failure 999: unknown error")
            return real(images, models=models)

        self.services.tag = broken_once
        with mock.patch.object(at.Indexer, "DROP_WAIT", 0):
            self.run_indexer()
        failed = dict(self.store.conn.execute("select id, error from failed").fetchall())
        self.assertFalse([e for e in failed.values() if "CUDA" in e or "graphics card" in e])   # nobody blamed
        self.assertEqual(set(failed), {self.ids[3], self.ids[4]})       # only the fixture's two unreadable files
        self.assertEqual(self.store.counts()["processed"], 3)          # a fresh tagger did the rest
        self.assertGreater(len(calls), 1)

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
        tagged = list(self.services.tag_calls)
        self.run_indexer()
        self.assertEqual(self.store.counts()["processed"], 1)
        self.assertEqual(self.services.tag_calls, tagged)                           # no second GPU pass
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

    def test_no_tagger_means_no_tags(self):
        self.run_indexer(use_wd=False, use_pixai=False, use_ram=False, use_e621=False)
        self.assertEqual(self.services.tag_calls, [])
        self.assertEqual(self.services.ready_calls[0], {"tagger": False})            # the container is not even started
        self.assertEqual(self.store.counts()["processed"], 4)               # no tagger to say "broken" about the last one
        self.assertEqual(self.description(0), "")                           # nothing to say: nothing written
        self.assertEqual(self.store.result(self.ids[0])["tags"], [])

    def test_a_picture_with_no_tags_gets_no_block(self):
        self.catalog = [{**self.catalog[0], "preview": "wd:dull=0.1", "name": "dull.jpg"}]
        self.put_description(0, "Mine.")
        self.run_indexer(rating_tag=False)
        res = self.store.result(self.ids[0])
        self.assertEqual((res["tags"], res["block"]), ([], ""))
        self.assertEqual(self.description(0), "Mine.")                      # nothing to say: the owner's text is untouched

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


class GatedServices(FakeServices):
    """The tagger with a gate: a /tag request that arrives waits until the test lets it through (``release(n)`` for the
    n-th request to arrive, ``open_all()`` for all of them), and the n-th request can be made to fail (``fail[n]``,
    raised when it is let through). ``arrived`` holds the pictures of every request, in order of arrival."""

    def __init__(self):
        super().__init__()
        self.cond = threading.Condition()
        self.arrived: list[list[bytes]] = []
        self.answered = 0
        self.released: set[int] = set()
        self.all_open = False
        self.fail: dict[int, Exception] = {}
        self.peak = 0                                       # most requests inside ``tag`` at the same time

    def tag(self, images, models=None):
        with self.cond:
            self.arrived.append(list(images))
            n = len(self.arrived)
            self.peak = max(self.peak, n - self.answered)
            self.cond.notify_all()
            if not self.cond.wait_for(lambda: self.all_open or n in self.released, 30):
                raise AssertionError(f"the test never let request {n} through")
            self.answered += 1
            self.cond.notify_all()
        if n in self.fail:
            raise self.fail[n]
        return super().tag(images, models)

    def wait_arrived(self, n: int, timeout: float = 10) -> bool:
        with self.cond:
            return self.cond.wait_for(lambda: len(self.arrived) >= n, timeout)

    def release(self, n: int) -> None:
        with self.cond:
            self.released.add(n)
            self.cond.notify_all()

    def open_all(self) -> None:
        with self.cond:
            self.all_open = True
            self.cond.notify_all()


class Preps:
    """The ``frames`` function of the indexer, remembering which assets had their pictures read."""

    def __init__(self, frames):
        self.frames, self.cond, self.seen = frames, threading.Condition(), []

    def __call__(self, item, n):
        with self.cond:
            self.seen.append(item["id"])
            self.cond.notify_all()
        return self.frames(item, n)

    def wait_for(self, asset_id: str, timeout: float = 10) -> bool:
        """True once the pictures of that asset are being read (False after ``timeout``)."""
        with self.cond:
            return self.cond.wait_for(lambda: asset_id in self.seen, timeout)


class TestIndexerPipeline(Base):
    """The stages overlap across rounds: a round hands its batches to the tagger and goes on, so the next round is
    prepared while the earlier requests are still on the GPU. The tagger here holds every request at a gate."""

    def setUp(self):
        super().setUp()
        self.services = GatedServices()
        self.indexer = at.Indexer(self.store, self.services, client=self.client, catalog=lambda: self.catalog,
                                  frames=fake_frames)
        self.indexer.DROP_WAIT = 0.01
        self.addCleanup(self.indexer.stop, 5)
        self.addCleanup(self.services.open_all)             # (runs first) so that no request is left at the gate
        self.preps = Preps(fake_frames)
        self.indexer.frames = self.preps
        self.sent_with: list[int] = []                      # requests in flight right after each batch was handed over
        real_send = self.indexer._send

        def spy(*args, **kwargs):
            sent = real_send(*args, **kwargs)
            self.sent_with.append(self.indexer.tag_requests())
            return sent

        self.indexer._send = spy

    def library(self, n: int, broken=()) -> list[str]:
        """n photos, newest first (``ids[0]`` is worked on first), each with a picture of its own; the numbers in
        ``broken`` have no preview. Returns their ids."""
        self.catalog = [{"id": self.ids[i], "type": "IMAGE", "taken": f"2026-03-01 10:{59 - i:02d}:00", "name": f"IMG_{i:04d}.jpg",
                         "preview": "MISSING" if i in broken else f"wd:p{i}=0.9|rating:general=0.9", "original": "",
                         "duration_ms": 0} for i in range(n)]
        return self.ids[:n]

    def start(self, **settings) -> None:
        self.set(keep_updated=False, indexing=True, **settings)
        self.indexer.start()

    def finish(self) -> None:
        """Let every request through and wait for the run to end by itself."""
        self.services.open_all()
        self.indexer.thread.join(30)
        self.assertFalse(self.indexer.running(), "the indexer did not finish")

    def pictures(self) -> list[bytes]:
        return [p for request in self.services.arrived for p in request]

    def test_the_next_round_is_prepared_while_the_gpu_is_busy_with_the_last(self):
        ids = self.library(6)
        self.start(batch_size=2)
        self.assertTrue(self.services.wait_arrived(1))                  # round 1 is on the tagger, held at the gate
        self.assertTrue(self.preps.wait_for(ids[2]), "round 2 was not prepared while request 1 was running")
        self.assertTrue(self.preps.wait_for(ids[3]))
        self.assertEqual(self.services.answered, 0)                     # (request 1 is still running)
        self.assertLessEqual({ids[0], ids[1]}, self.indexer._flying())  # its assets stay claimed ...
        self.assertFalse({ids[0], ids[1]} & {a["id"] for a in self.store.work(6, skip=self.indexer._flying())})   # ... so no one else takes them
        self.assertEqual(len(self.services.ready_calls), 1)             # no health check while a request is running
        self.finish()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 6)
        self.assertEqual(self.services.tag_calls, [2, 2, 2])

    def test_never_more_than_TAG_REQUESTS_in_flight_and_the_pictures_held_stay_bounded(self):
        ids = self.library(12)
        svc = self.services
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(2))                            # two requests on the tagger (the limit)
        self.assertTrue(self.preps.wait_for(ids[5]))                    # round 3 is prepared, in hand, waiting for a slot
        # Round 4 must not be read while both slots are taken: 2 batches in flight + 1 in hand is all that is held.
        self.assertFalse(self.preps.wait_for(ids[6], timeout=0.3))
        self.assertEqual(len(svc.arrived), 2)
        self.assertEqual(self.indexer.tag_requests(), 2)
        svc.release(1)                                                  # a slot is free: the batch in hand goes out
        self.assertTrue(svc.wait_arrived(3))
        self.assertEqual(self.indexer.tag_requests(), 2)                # (request 2 and the new one)
        self.finish()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 12)
        self.assertEqual(max(self.sent_with), self.indexer.TAG_REQUESTS)
        self.assertLessEqual(svc.peak, self.indexer.TAG_REQUESTS)
        self.assertEqual(self.indexer.tag_requests(), 0)

    def test_with_one_request_at_a_time_the_next_round_is_still_prepared_ahead(self):
        self.indexer.TAG_REQUESTS = 1
        ids = self.library(8)
        svc = self.services
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(1))
        self.assertTrue(self.preps.wait_for(ids[3]))                    # round 2 is in hand while request 1 runs
        self.assertFalse(self.preps.wait_for(ids[4], timeout=0.3))      # round 3 waits until request 1 is answered
        self.assertEqual(len(svc.arrived), 1)
        self.finish()
        self.assertEqual(self.store.counts()["processed"], 8)
        self.assertEqual(max(self.sent_with), 1)
        self.assertEqual(svc.peak, 1)

    def test_the_last_partial_batch_of_a_round_goes_out_without_waiting_for_anything(self):
        ids = self.library(4, broken={2})                               # round 2: one unreadable file, so a batch of one
        svc = self.services
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(2), "the partial batch waited for request 1")
        self.assertEqual(svc.answered, 0)                               # request 1 is still running
        self.assertEqual(sorted(len(r) for r in svc.arrived), [1, 2])   # (a batch of one asset, and the full one)
        self.finish()
        self.assertEqual(self.store.counts()["processed"], 3)
        self.assertEqual(self.store.counts()["failed"], 1)
        self.assertEqual(self.indexer._flying(), set())

    def test_a_failing_request_surfaces_and_the_assets_go_back_to_the_pool(self):
        ids = self.library(6)
        svc = self.services
        svc.fail[1] = RuntimeError("the tagger fell over")
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(2))                            # request 2 is out too, request 1 still at the gate
        svc.release(1)                                                  # request 1 fails now ...
        svc.release(2)                                                  # ... and request 2, in flight beside it, succeeds
        self.indexer.thread.join(30)
        self.assertFalse(self.indexer.running())
        self.assertEqual(self.indexer.state, "error")
        self.assertIn("RuntimeError: the tagger fell over", self.indexer.error)
        self.assertEqual(self.indexer._flying(), set())                 # nothing stays claimed
        c = self.store.counts()
        self.assertEqual((c["processed"], c["failed"]), (2, 0))         # request 2 was finished; nobody is "failed"
        self.assertEqual({a["id"] for a in self.store.work(10)}, set(ids[:2]) | set(ids[4:]))
        self.assertEqual(self.indexer.tag_requests(), 0)

    def test_a_server_that_goes_away_while_the_next_round_is_prepared_is_waited_for(self):
        ids = self.library(6)
        svc = self.services
        svc.fail[1] = at.ServiceDown("gone")
        flying_after_drain = []
        real_drain = self.indexer._drain

        def drain():
            real_drain()
            flying_after_drain.append(self.indexer._flying())

        self.indexer._drain = drain
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(2))
        svc.release(1)
        self.finish()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 6)
        self.assertEqual(self.store.counts()["failed"], 0)              # not the assets' fault
        self.assertEqual(len(svc.arrived), 4)                           # 1 failed, 2, then the assets of 1 again, then round 3
        self.assertEqual(sum(svc.tag_calls), 6)                         # six answers: no picture was tagged twice
        self.assertEqual(flying_after_drain[0], set())                  # the drain waited for request 2 and gave back the rest
        self.assertEqual(self.indexer._flying(), set())

    def test_with_nothing_left_to_read_it_waits_for_the_requests_instead_of_looking_again_and_again(self):
        self.library(2)
        svc = self.services
        looks, real_work = [], self.store.work
        self.store.work = lambda *a, **k: looks.append(1) or real_work(*a, **k)
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(1))                            # the only batch is on the tagger, held at the gate
        self.assertFalse(self.preps.wait_for("nothing is ever read again", timeout=0.3))
        self.assertLessEqual(len(looks), 3)                             # (a look at the work is a database query)
        self.finish()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 2)

    def test_a_request_that_fails_after_the_last_round_was_sent_is_not_lost(self):
        self.library(2)
        svc = self.services
        svc.fail[1] = at.ServiceDown("gone")
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(1))
        svc.release(1)
        self.finish()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 2)           # "everything is tagged" only when it is
        self.assertEqual(len(svc.arrived), 2)

    def test_a_failing_request_that_is_the_last_thing_left_ends_in_an_error(self):
        self.library(2)
        svc = self.services
        svc.fail[1] = RuntimeError("bad answer")
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(1))
        svc.release(1)
        self.finish()
        self.assertEqual(self.indexer.state, "error")
        self.assertIn("bad answer", self.indexer.error)
        self.assertEqual(self.store.counts()["processed"], 0)
        self.assertEqual(self.indexer._flying(), set())

    def test_pausing_with_requests_in_flight_loses_nothing_and_leaves_nothing_claimed(self):
        ids = self.library(10)
        svc = self.services
        self.start(batch_size=2)
        self.assertTrue(svc.wait_arrived(2))                            # two requests on the tagger
        self.assertTrue(self.preps.wait_for(ids[5]))                    # round 3 is being prepared, waiting for a slot
        self.indexer.stop()
        svc.open_all()                                                  # the two requests on the tagger are answered
        self.indexer.thread.join(30)
        self.assertFalse(self.indexer.running())
        self.assertEqual((self.indexer.state, self.indexer.detail), ("stopped", "paused"))
        self.assertEqual(self.indexer._flying(), set())
        self.assertEqual(len(svc.arrived), 2)                           # nothing was sent after the pause
        c = self.store.counts()
        self.assertEqual((c["processed"], c["failed"]), (4, 0))         # what was on the GPU was finished and written
        self.assertEqual({a["id"] for a in self.store.work(20)}, set(ids[4:]))    # the rest is waiting its turn
        self.indexer.start()                                            # and it picks up where it was
        self.indexer.thread.join(30)
        self.assertFalse(self.indexer.running())
        self.assertEqual(self.store.counts()["processed"], 10)
        self.assertEqual(len(self.pictures()), 10)                      # every picture went to the tagger once ...
        self.assertEqual(len(set(self.pictures())), 10)                 # ... and only once

    def test_no_asset_is_claimed_or_processed_twice_while_the_rounds_overlap(self):
        ids = self.library(24)
        svc = self.services
        claimed_twice, processed = [], collections.Counter()
        real_claim, real_process = self.indexer._claim, self.indexer.pipe.process

        def claim(asset_id):
            if asset_id in self.indexer._flying():
                claimed_twice.append(asset_id)
            real_claim(asset_id)

        def process(item, mode, raw, settings, version):
            processed[item["id"]] += 1
            return real_process(item, mode, raw, settings, version)

        self.indexer._claim, self.indexer.pipe.process = claim, process
        self.start(batch_size=3)
        for n in range(1, 9):                                           # answers come one at a time, rounds overlap
            self.assertTrue(svc.wait_arrived(n))
            svc.release(n)
        self.finish()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(claimed_twice, [])
        self.assertEqual(processed, collections.Counter({i: 1 for i in ids}))
        self.assertEqual(len(set(self.pictures())), 24)
        self.assertEqual(sum(svc.tag_calls), 24)
        c = self.store.counts()
        self.assertEqual((c["processed"], c["pending"], c["failed"]), (24, 0, 0))
        self.assertEqual(self.indexer._done, 24)                        # the progress and rate counters
        self.assertEqual(len(self.indexer.done_times), 24)
        self.assertEqual(self.indexer._flying(), set())

    def test_out_of_memory_halving_works_with_requests_in_flight(self):
        self.library(12)
        svc = self.services
        svc.oom_above = 2
        svc.open_all()
        self.start(batch_size=4)
        self.indexer.thread.join(30)
        self.assertFalse(self.indexer.running())
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 12)
        self.assertEqual(self.store.counts()["failed"], 0)
        self.assertEqual(self.indexer.batch_cap, 2)                     # 4 -> 2 works
        self.assertTrue(svc.oom_calls and all(n == 4 for n in svc.oom_calls))
        self.assertTrue(all(n <= 2 for n in svc.tag_calls))
        self.assertEqual(sum(svc.tag_calls), 12)
        self.assertEqual(self.indexer._flying(), set())


class TestReprocess(Base):
    def setUp(self):
        super().setUp()
        self.run_indexer()
        self.services.tag_calls.clear()
        self.services.tag_models.clear()
        self.services.ready_calls.clear()

    def test_retag_needs_no_gpu_and_no_models(self):
        self.assertEqual(self.set(wd_strictness=0.85, blocked=["solo"], max_tags=5), ["wd_strictness", "blocked", "max_tags"])
        self.assertEqual(self.store.counts()["outdated"], 3)
        self.assertEqual(at.reprocess(self.store, "outdated", "retag"), 3)
        self.run_indexer()
        self.assertEqual((self.services.tag_calls, self.services.ready_calls), ([], []))
        # girl .9 stays; WD's beach .6 and miku .8 are below .85, but PixAI still says beach; solo is blocked
        self.assertEqual(self.description(0), "[AI Tagger]\nTags: girl, beach, sea, rating: general\n[/AI Tagger]")
        self.assertEqual(self.store.counts()["outdated"], 0)
        self.assertEqual(self.store.result(self.ids[0])["settings_version"], 2)
        self.assertEqual(self.store.queue(), [])
        self.assertEqual(len(self.store.history(self.ids[0])), 2)

    def test_a_vocabulary_rename_is_a_retag(self):
        self.assertEqual(self.set(vocabulary="girl -> woman"), ["vocabulary"])
        self.assertEqual(at.suggest_mode(["vocabulary"]), "retag")
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])                         # the stored scores are enough
        self.assertEqual(self.description(0), "[AI Tagger]\nTags: woman, beach, miku, solo, sea, rating: general\n[/AI Tagger]")

    def test_typed_combinations_are_a_retag_too(self):
        text = "# my combinations\ngirl + beach -> summer, -solo\n1girl -> woman"
        self.assertEqual(self.set(vocabulary=text), ["vocabulary"])
        self.assertEqual(self.store.counts()["outdated"], 3)                   # the tagged assets were made without it
        self.assertEqual(at.reprocess(self.store, "outdated", "retag"), 3)
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])                            # the stored scores are enough, no GPU
        self.assertEqual(self.description(0),
                         "[AI Tagger]\nTags: summer, girl, beach, miku, sea, rating: general\n[/AI Tagger]")   # solo removed
        self.assertEqual(self.store.counts()["outdated"], 0)

    def test_describe_is_handled_as_a_retag(self):
        self.set(blocked=["solo"])
        self.assertEqual(at.reprocess(self.store, "all", "describe"), 3)                 # what an old app asks for
        self.assertEqual({q["mode"] for q in self.store.queue()}, {"retag"})
        self.run_indexer()
        self.assertEqual((self.services.tag_calls, self.services.ready_calls), ([], []))
        self.assertNotIn("solo", self.description(0))
        self.assertEqual(self.store.queue(), [])
        # a "describe" row that is in the queue anyway (written by v2) is run as a retag and leaves the queue
        self.store.conn.execute("insert or replace into queue values (?,?,?)", (self.ids[1], "describe", at._now()))
        self.store.conn.commit()
        self.assertEqual(self.store.work(5)[0]["mode"], "describe")
        self.set(blocked=["solo", "grass"])
        self.run_indexer()
        self.assertEqual((self.services.tag_calls, self.services.ready_calls), ([], []))
        self.assertEqual(self.store.queue(), [])
        self.assertNotIn("grass", self.description(1))

    def test_a_v2_result_is_rewritten_without_its_description_line_by_a_retag(self):
        v2 = "[AI Tagger]\nTags: girl, beach, miku, solo, sea, rating: general\nDescription: A girl on a beach.\n[/AI Tagger]"
        self.put_description(0, "Mine.\n\n" + v2)
        self.store.conn.execute("update results set block=?, settings_version=0 where id=?", (v2, self.ids[0]))
        self.store.conn.commit()
        self.assertEqual(self.store.counts()["outdated"], 1)
        self.assertEqual(at.reprocess(self.store, "outdated", "retag"), 1)
        self.run_indexer()
        self.assertEqual(self.description(0),
                         "Mine.\n\n[AI Tagger]\nTags: girl, beach, miku, solo, sea, rating: general\n[/AI Tagger]")
        self.assertEqual(self.services.tag_calls, [])                         # no GPU: the stored scores are enough
        self.assertEqual(self.store.counts()["outdated"], 0)

    def test_full_runs_everything_again(self):
        self.set(video_frames=2)
        at.reprocess(self.store, "outdated", "full")
        self.run_indexer()
        self.assertEqual(sum(self.services.tag_calls), 1 + 1 + 3)          # the video's three captures
        self.assertEqual({tuple(m) for m in self.services.tag_models}, {tuple(REAL)})

    def test_without_stored_scores_retag_becomes_full(self):
        self.store.conn.execute("delete from raw where id=?", (self.ids[1],))
        self.store.conn.commit()
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])

    def make_v1(self, i):
        """Turn asset i's stored scores into what the RAM++ service left (WD scores plus a "ram" entry, no PixAI and no
        Hydra)."""
        scores = [{"wd": {"general": {"girl": 0.9}, "character": {}}, "ram": {"beach": 0.8}}]
        self.store.conn.execute("update raw set scores_json=?, rating_json=?, models=? where id=?",
                                (json.dumps(scores), json.dumps([{"general": 0.9}]), "ram,wd", self.ids[i]))
        self.store.conn.commit()
        self.assertFalse(at.has_kind(self.store.raw(self.ids[i]), "pixai"))

    def test_stored_scores_without_an_enabled_tagger_make_a_retag_a_full_reprocess(self):
        self.make_v1(1)
        self.set(max_tags=11)
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])                    # the taggers ran again for it, and only for it
        self.assertEqual(self.services.ready_calls[0], {"tagger": True})
        self.assertTrue(at.has_kind(self.store.raw(self.ids[1]), "pixai"))        # the stored scores are complete now
        self.assertTrue(at.has_kind(self.store.raw(self.ids[1]), "e621"))
        self.assertIn("grass", self.description(1))                       # PixAI's tag is in the result
        self.assertEqual(self.store.queue(), [])
        self.services.tag_calls.clear()
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])        # and the next retag is the cheap kind again
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [])
        self.make_v1(0)
        at.reprocess(self.store, "ids", "describe", ids=[self.ids[0]])     # a v2 "describe" is a retag, and this one needs PixAI too
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])
        self.assertTrue(at.has_kind(self.store.raw(self.ids[0]), "pixai"))

    def test_a_result_that_was_never_written_is_finished_as_full_when_its_scores_are_from_v1(self):
        self.make_v1(1)
        self.store.conn.execute("update results set written_at=null where id=?", (self.ids[1],))      # stored, not written
        self.store.conn.commit()
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_scores_without_hydra_make_a_retag_a_full_reprocess_even_with_pixai_off(self):
        self.make_v1(1)
        self.set(use_pixai=False)                                          # Hydra is on and the stored scores have none
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])
        self.assertEqual({tuple(m) for m in self.services.tag_models}, {("wd", "ram", "e621")})      # PixAI is not asked for
        self.assertTrue(at.has_kind(self.store.raw(self.ids[1]), "e621"))
        self.assertFalse(at.has_kind(self.store.raw(self.ids[1]), "pixai"))

    def test_scores_without_pixai_and_hydra_are_fine_while_both_are_off(self):
        self.make_v1(1)
        self.set(use_pixai=False, use_e621=False)                          # nothing is lost: neither is used
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual((self.services.tag_calls, self.services.ready_calls), ([], []))
        self.assertNotIn("grass", self.description(1))
        self.assertIn("girl", self.description(1))
        # v1's flat RAM++ scores and flat WD rating are read, not dropped: RAM++'s "beach" and the rating are there
        self.assertEqual(self.description(1), "[AI Tagger]\nTags: girl, beach, rating: general\n[/AI Tagger]")
        self.assertEqual({t["tag"]: t["source"] for t in self.store.result(self.ids[1])["tags"]},
                         {"girl": "wd", "beach": "ram", "rating: general": "wd"})
        self.set(use_pixai=True)                                           # switching it on is a "full" change, as before
        self.assertEqual(at.suggest_mode(["use_pixai"]), "full")
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])

    def test_a_v1_row_is_redone_as_full_and_comes_back_in_the_nested_shape(self):
        self.make_v1(1)
        self.assertEqual(self.store.raw(self.ids[1])["scores"][0]["ram"], {"beach": 0.8})             # flat, as v1 wrote it
        at.reprocess(self.store, "ids", "retag", ids=[self.ids[1]])
        self.run_indexer()
        self.assertEqual(self.services.tag_calls, [1])                         # PixAI is on and missing: the GPU runs
        raw = self.store.raw(self.ids[1])
        self.assertEqual(raw["scores"][0]["ram"], {"general": {}})             # what the v3 service answers (DOG: nothing)
        self.assertEqual(raw["scores"][0]["e621"], {"general": {}, "species": {}, "character": {}, "copyright": {}})
        self.assertEqual(raw["ratings"], [{"wd": {"general": 0.95, "sensitive": 0.05}, "pixai": None, "ram": None,
                                           "e621": None}])
        self.assertNotIn("beach", self.description(1))                         # the stale v1 RAM++ tag is gone with its row
        self.assertEqual(self.description(1), "[AI Tagger]\nTags: dog, grass, rating: general\n[/AI Tagger]")

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
        self.set(blocked=["car"])                                        # so that the queued retag really writes
        self.store.enqueue([self.ids[2]], "retag")
        before = len(self.fake.asset_puts)
        self.indexer.refresh_catalog()
        # The order work is handed out in (write threads may finish in any order, which made this test flaky)
        self.assertEqual([i["id"] for i in self.store.work(1, skip=set())], [self.ids[2]])   # the queued video first
        self.run_indexer(batch_size=1)
        written = {asset for asset, _body in self.fake.asset_puts[before:]}
        self.assertTrue({self.ids[2], self.ids[10]} <= written)          # and then the new photo as well

    def test_a_strong_request_beats_a_weak_one_in_the_run(self):
        a = self.ids[0]
        self.store.enqueue([a], "retag")
        self.store.enqueue([a], "full")
        self.store.enqueue([a], "describe")                                # a retag: it does not lower the full one
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
        got = self.indexer.test(self.ids[0])
        self.assertEqual((got["id"], got["name"], got["type"], got["captures"]), (self.ids[0], "IMG_0000.jpg", "IMAGE", 1))
        self.assertEqual(got["frames"], [at.data_url(PHOTO.encode())])
        self.assertTrue({t["tag"] for t in got["models"]["wd"]} >= {"girl", "hat"})
        self.assertEqual(set(got["models"]), {"wd", "pixai", "ram", "e621", "rating"})
        self.assertEqual({t["tag"]: t["kept"] for t in got["models"]["pixai"]}, {"beach": True, "sea": True, "wave": False})
        self.assertEqual((got["models"]["ram"], got["models"]["e621"]), ([], []))     # PHOTO has no RAM++ or Hydra tags
        self.assertEqual(got["models"]["rating"]["general"], 0.9)
        self.assertEqual(set(got), {"id", "name", "type", "captures", "frames", "models", "rules", "tags", "block",
                                    "currentDescription", "newDescription", "written"})        # no describer section, no description
        self.assertEqual(got["rules"], [{"rule": 0, "added": ["summer"], "removed": []}])
        sources = {t["tag"]: t["source"] for t in got["tags"]}
        self.assertEqual((sources["girl"], sources["sea"], sources["summer"]), ("wd", "pixai", "rule"))
        self.assertEqual(got["currentDescription"], "Mine.")
        self.assertEqual(got["newDescription"], "Mine.\n\n" + got["block"])
        self.assertFalse(got["written"])
        self.assertEqual(self.description(0), "Mine.")                                 # nothing written
        self.assertIsNone(self.store.result(self.ids[0]))
        self.assertIsNone(self.store.raw(self.ids[0]))
        self.assertEqual(self.fake.asset_puts, [])
        self.assertEqual(self.services.ready_calls[0], {"tagger": True})
        self.assertEqual(self.services.tag_models, [REAL])

    def test_the_trace_names_typed_combinations_by_line(self):
        self.set(rules=[rule(if_all=["girl", "beach"], add=["summer"])],
                 vocabulary="# combinations\nsea + beach -> seaside, -solo\nsea + !beach -> never\nglass | hat -> never")
        got = self.indexer.test(self.ids[0])
        self.assertEqual(got["rules"], [{"rule": 0, "added": ["summer"], "removed": []},          # the form's rule: its number
                                        {"rule": "line 2", "added": ["seaside"], "removed": ["solo"]}])   # a typed one: its line
        names = [t["tag"] for t in got["tags"]]
        self.assertTrue({"summer", "seaside"} <= set(names))
        self.assertNotIn("solo", names)
        self.assertNotIn("never", names)
        self.assertEqual(self.description(0), "")                                                 # still only a preview

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
        self.set(use_wd=False, use_pixai=False, use_ram=False, use_e621=False)
        got = self.indexer.test(self.ids[0])
        self.assertEqual(self.services.tag_calls, [])
        self.assertEqual(tags_of(got), [])
        self.assertEqual(self.services.ready_calls[0], {"tagger": False})
        self.assertEqual(got["block"], "")                              # nothing to say


# ---------------------------------------------------------------- the model container

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeRunner:
    """Stands in for the docker / nvidia-smi runner: containers have a state, compose up starts them."""

    SERVICES = {"tagger": "immich_aitagger"}          # the compose services that exist: the describer's is gone

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
            self.state[self.SERVICES[cmd[-1]]] = "running"      # any other service name is a KeyError: it must not be asked for
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
    """A tagger that becomes ready ``after`` seconds (on the fake clock) once the container runs."""

    def __init__(self, clock, runner, container, after=10, error=None):
        self.clock, self.runner, self.container, self.after, self.error = clock, runner, container, after, error
        self.started = None

    def health(self, timeout=3):
        if self.runner.state.get(self.container) != "running":
            self.started = None
            return None
        if self.started is None:
            self.started = self.clock()
        if self.error:
            return {"status": "error", "error": self.error}
        ready = self.clock() - self.started >= self.after
        return {"status": "ok" if ready else "loading", "error": None}


class TestServices(Base):
    def setUp(self):
        super().setUp()
        self.clock, self.runner = Clock(), FakeRunner()
        self.stopped_search = []
        self.make()

    def make(self, tagger_after=10, **kw):
        self.tagger = FakeHealth(self.clock, self.runner, "immich_aitagger", tagger_after)
        self.svc = at.Services(self.tagger, runner=self.runner, store=self.store, clock=self.clock,
                               sleep=self.clock.sleep, search_stop=lambda: self.stopped_search.append(len(self.runner.calls)), **kw)

    def test_a_dead_graphics_card_stops_the_tagger_so_the_next_round_starts_a_fresh_one(self):
        self.svc.load()
        self.assertEqual(self.runner.state.get("immich_aitagger"), "running")

        def broken(images, models=None):
            raise at.GpuBroken("the tagger lost the graphics card: CUDA failure 999")

        self.tagger.tag = broken
        with self.assertRaises(at.GpuBroken):
            self.svc.tag([b"x"])
        self.assertIn(["docker", "stop", "-t", "20", "immich_aitagger"], [c for c, _ in self.runner.calls])

    def test_vram_gb_is_the_taggers_cap_and_nothing_else_is_derived(self):
        self.assertEqual(self.svc.env(), {"AITAGGER_VRAM_GB": str(at.VRAM_GB_DEFAULT)})
        self.assertEqual(self.svc.env(), {"AITAGGER_VRAM_GB": "6"})              # four taggers: 6 GB, full speed
        lo, hi = at.VRAM_GB_LIMITS
        self.assertEqual((lo, hi), (5, 8))
        for gb in (lo, hi):                                                 # AITAGGER_VRAM_GB = vram_gb, nothing else moves
            self.assertEqual(self.svc.env(S(vram_gb=gb)), {"AITAGGER_VRAM_GB": str(gb)})
        self.runner.gpu = "12288, 100"                                      # not derived from the card either
        self.make()
        self.assertEqual(self.svc.env(S(vram_gb=hi)), {"AITAGGER_VRAM_GB": str(hi)})
        self.assertFalse(any("VLM" in key for key in self.svc.env()))

    def test_the_card_size_is_still_reported_and_taken_to_be_24_gb_without_nvidia_smi(self):
        self.runner.gpu = ""
        self.make()
        self.assertEqual(self.svc.gpu(), {"totalGb": 24, "usedGb": None})

    def test_load_starts_only_the_tagger_through_compose(self):
        self.assertEqual(self.svc.load(), ["immich_aitagger"])
        cmds = self.runner.ups()
        compose = str(at.COMPOSE)
        self.assertTrue(compose.replace("\\", "/").endswith("deploy/aitagger/docker-compose.yml"))
        self.assertEqual(cmds, [["docker", "compose", "-p", "immich-aitagger", "-f", compose, "up", "-d", "tagger"]])
        for _, env in [c for c in self.runner.calls if c[0][:2] == ["docker", "compose"]]:
            self.assertEqual(env, {"AITAGGER_VRAM_GB": str(at.VRAM_GB_DEFAULT)})
        self.assertEqual(self.svc.load(), [])                               # already running: nothing to do
        self.assertEqual(len(self.runner.ups()), 1)

    def test_nothing_but_the_tagger_service_is_ever_started_or_stopped(self):
        self.runner.state["immich_searchplus"] = "running"
        self.svc.ensure_ready(wait=600)
        self.svc.status()
        self.svc.unload()
        self.svc.load()
        for cmd, env in self.runner.calls:
            joined = " ".join(cmd)
            self.assertNotIn("vlm", joined)
            self.assertFalse(env and any("VLM" in key for key in env), cmd)
            if cmd[:2] == ["docker", "compose"]:
                self.assertEqual(cmd[-1], "tagger")
            elif cmd[:2] == ["docker", "stop"]:
                self.assertEqual(cmd[-1], "immich_aitagger")
            elif cmd[:2] == ["docker", "inspect"]:
                self.assertIn(cmd[-1], ("immich_aitagger", "immich_searchplus"))
        self.assertEqual(self.runner.state["immich_searchplus"], "running")        # Search+ is left alone (not exclusive)
        self.assertFalse(hasattr(self.svc, "vlm_box"))
        self.assertFalse(hasattr(self.svc, "vlm"))

    def test_the_container_is_recreated_only_when_the_derived_env_changed(self):
        self.svc.load()
        self.assertFalse(any("--force-recreate" in c for c in self.runner.ups()))
        self.assertEqual(json.loads(self.store.meta("services_env"))["immich_aitagger"],
                         {"AITAGGER_VRAM_GB": str(at.VRAM_GB_DEFAULT)})
        self.svc.unload()
        self.runner.calls.clear()
        self.svc.load()                                                     # same settings: a plain start
        self.assertFalse(any("--force-recreate" in c for c in self.runner.ups()))
        self.svc.unload()
        self.set(batch_size=4, vram_gb=at.VRAM_GB_LIMITS[1])                # the taggers' cap changed: recreated
        self.runner.calls.clear()
        self.svc.load()
        self.assertEqual([c[-1] for c in self.runner.ups() if "--force-recreate" in c], ["tagger"])
        self.svc.unload()
        self.runner.calls.clear()
        self.svc.load()                                                     # remembered: no more recreating
        self.assertFalse(any("--force-recreate" in c for c in self.runner.ups()))

    def test_what_v2_remembered_about_the_describer_container_does_no_harm(self):
        self.store.set_meta("services_env", json.dumps({"immich_aitagger": {"AITAGGER_VRAM_GB": str(at.VRAM_GB_DEFAULT)},
                                                        "immich_aitagger_vlm": {"AITAGGER_VLM_UTIL": "0.22", "AITAGGER_VLM_SEQS": "16"}}))
        self.runner.state["immich_aitagger"] = "stopped"
        self.svc.load()
        self.assertFalse(any("--force-recreate" in c for c in self.runner.ups()))
        self.assertEqual(self.runner.state, {"immich_aitagger": "running"})

    def test_a_container_that_exists_with_an_unknown_env_is_recreated(self):
        self.runner.state["immich_aitagger"] = "stopped"                    # made by hand, before the panel remembered anything
        self.svc.load()
        self.assertEqual([c[-1] for c in self.runner.ups() if "--force-recreate" in c], ["tagger"])

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
        self.assertFalse(sp.AITAGGER_EXCLUSIVE)     # shipped: they share the card
        for exclusive in (True, False):
            with self.subTest(exclusive=exclusive), mock.patch.object(sp, "AITAGGER_EXCLUSIVE", exclusive):
                self.runner.state = {"immich_searchplus": "running"}
                self.stopped_search.clear()
                self.assertEqual(self.svc.load(), ["immich_aitagger"])
                self.assertEqual(len(self.stopped_search), 1 if exclusive else 0)       # False: Search+ is left running
                self.assertEqual(self.runner.state["immich_searchplus"], "running")     # (the fake stop is only a record)
                self.svc.unload()

    def test_search_plus_is_left_alone_when_the_tagger_is_already_up(self):
        self.svc.load()
        self.runner.state["immich_searchplus"] = "running"
        self.svc.load()
        self.assertEqual(self.stopped_search, [])

    def test_a_failed_start_is_a_service_down_with_the_reason(self):
        self.runner.up_fails = True
        with self.assertRaises(sp.ServiceDown) as err:
            self.svc.load()
        self.assertIn("no such image", str(err.exception))

    def test_ensure_ready_starts_and_waits(self):
        progress = []
        self.svc.ensure_ready(wait=600, progress=progress.append)
        self.assertEqual(self.clock.t, 1000 + 10)
        self.assertTrue(progress)
        self.assertIn("loading the tagger", progress[0])
        self.svc.ensure_ready()                                             # ready: no work
        self.assertEqual(len(self.runner.ups()), 1)
        self.assertEqual(self.clock.t, 1010)

    def test_ensure_ready_without_a_tagger_needs_nothing(self):
        self.svc.ensure_ready(need_tagger=False, wait=600)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.clock.t, 1000)

    def test_ensure_ready_gives_up_after_the_wait(self):
        self.make(tagger_after=10_000)
        with self.assertRaises(sp.ServiceDown) as err:
            self.svc.ensure_ready(wait=90)
        self.assertIn("still loading", str(err.exception))
        self.assertGreaterEqual(self.clock.t, 1090)
        self.assertLess(self.clock.t, 1100)
        self.assertEqual(len(self.runner.ups()), 1)                         # it was started, so a retry finds it further on

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
        self.make(tagger_after=10_000)
        real_sleep = self.clock.sleep

        def sleep(s):
            real_sleep(s)
            if self.clock.t > 1020:
                self.runner.state["immich_aitagger"] = "stopped"

        self.svc.sleep = sleep
        with self.assertRaises(RuntimeError) as err:
            self.svc.ensure_ready(wait=600)
        self.assertIn("docker logs immich_aitagger", str(err.exception))

    def test_unload_stops_the_tagger(self):
        self.svc.load()
        self.assertEqual(self.svc.unload(), ["immich_aitagger"])
        self.assertEqual(self.runner.state, {"immich_aitagger": "stopped"})
        self.assertEqual(self.svc.unload(), [])

    def test_the_panel_has_no_idle_clock_of_its_own(self):
        # the tagger container exits by itself after IDLE_EXIT_MINUTES (see tagger_service.py); only the describer
        # container needed the panel to stop it
        self.svc.ensure_ready(wait=600)
        self.clock.t += 3 * 60 * 60
        self.assertEqual(self.runner.state, {"immich_aitagger": "running"})
        self.assertEqual(len(self.runner.commands("stop")), 0)

    def test_using_the_tagger_goes_through_to_the_client(self):
        self.svc.tagger = mock.Mock(tag=mock.Mock(return_value=([], [])))
        self.svc.tag([b"x"], models=["wd"])
        self.svc.tagger.tag.assert_called_once_with([b"x"], models=["wd"])

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
                              "gpu": {"totalGb": 24.0, "usedGb": 1.0}, "searchplusRunning": False})
        self.assertNotIn("vlm", st)
        n = len(self.runner.calls)
        for _ in range(5):
            self.svc.status()
        self.assertEqual(len(self.runner.calls), n)                          # all from memory
        self.svc.load()
        self.svc.tagger_box.invalidate()
        st = self.svc.status()
        self.assertEqual(st["tagger"], {"container": "running", "status": "loading", "error": None})
        self.clock.t += 60
        self.runner.state["immich_searchplus"] = "running"
        st = self.svc.status()
        self.assertEqual((st["tagger"]["status"], st["searchplusRunning"]), ("ok", True))

    def test_docker_missing_is_unknown_not_a_crash(self):
        runner = mock.Mock(return_value=at._Done(127, "", "docker not found"))
        box = at.ComposeService("c", "s", runner)
        self.assertEqual(box.container_state(), "unknown")
        runner.return_value = at._Done(1, "", "Cannot connect to the Docker daemon")
        self.assertEqual(box.container_state(fresh=True), "unknown")

    def test_the_default_runner_never_raises(self):
        self.assertEqual(at.run_command(["definitely-not-a-command-xyz"]).returncode, 127)


class TestWithRealClients(Base):
    """The Indexer on the real Services / Tagger classes, talking HTTP to a stand-in model server."""

    def test_everything_end_to_end(self):
        def tag(body):
            got = [read_picture(base64.b64decode(i), body.get("models")) for i in body["images"]]
            return 200, {"results": [g[0] for g in got], "errors": [g[1] for g in got], "tookMs": 1}

        tagger = StubServer({("POST", "/tag"): tag, ("GET", "/health"): lambda b: (200, {"status": "ok"})})
        self.addCleanup(tagger.close)
        runner = FakeRunner()
        runner.state = {"immich_aitagger": "running"}
        services = at.Services(at.Tagger(tagger.url), runner=runner, store=self.store)
        self.indexer = at.Indexer(self.store, services, client=self.client, catalog=lambda: self.catalog, frames=fake_frames)
        self.indexer.DROP_WAIT = 0.01
        self.run_indexer()
        self.assertEqual(self.indexer.state, "done", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 3)
        self.assertEqual(self.description(1), "[AI Tagger]\nTags: dog, grass, rating: general\n[/AI Tagger]")
        requests = [r for r in tagger.requests if r[1] == "/tag"]
        self.assertEqual(len(requests), 1)                                                          # one batch
        self.assertEqual(requests[0][2]["models"], REAL)                                            # only the enabled taggers
        self.assertEqual(requests[0][2]["floor"], 0.2)
        self.assertEqual(runner.commands("compose"), [])                                            # it was running already
        status = services.status()
        self.assertEqual(status["tagger"]["status"], "ok")
        self.assertNotIn("vlm", status)
        # and a retag afterwards needs no tagging request to the server (it only looks at /health, to free the card)
        before = len([r for r in tagger.requests if r[1] == "/tag"])
        self.set(blocked=["dog"])
        at.reprocess(self.store, "outdated", "retag")
        self.run_indexer()
        self.assertEqual(len([r for r in tagger.requests if r[1] == "/tag"]), before)
        self.assertNotIn("dog", self.description(1))
        self.assertIn("grass", self.description(1))
        # turning a tagger off is sent on the next full pass: only the tagger that is still on is asked for
        self.set(use_pixai=False)
        self.store.enqueue([self.ids[1]], "full")
        self.run_indexer()
        self.assertEqual([r for r in tagger.requests if r[1] == "/tag"][-1][2]["models"], ["wd", "ram", "e621"])
        self.set(use_ram=False)
        self.store.enqueue([self.ids[1]], "full")
        self.run_indexer()
        self.assertEqual([r for r in tagger.requests if r[1] == "/tag"][-1][2]["models"], ["wd", "e621"])
        self.set(use_e621=False)
        self.store.enqueue([self.ids[1]], "full")
        self.run_indexer()
        self.assertEqual([r for r in tagger.requests if r[1] == "/tag"][-1][2]["models"], ["wd"])


class TestInstance(Base):
    def test_one_per_home_and_autostart_resumes_only_when_it_was_on(self):
        with mock.patch.object(at.Indexer, "start") as start:
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


# ---------------------------------------------------------------- keeping up to date, and freeing the card
#
# Same design as Search+ (tests/test_searchplus.py has the rules as a pure function and the probe's own tests): the
# indexer's waiting loop looks at the tagger container and stops it once it has been idle for ``unload_after``
# minutes, never while something is going on, and a cheap probe decides when the library list is read again.

def wait_for(cond, timeout=5.0):
    """Poll ``cond`` until it is true (the indexer runs on its own thread)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


class IdleServices(FakeServices):
    """FakeServices that say how long the tagger has been idle (what its /health says) and keep the facts the unload
    rules need. ``idle`` is /health's idleSeconds, ``busy_n`` its ``busy``, ``inflight_n`` the panel's requests in flight,
    ``age`` the seconds since a person last used the models (None: not at all)."""

    def __init__(self):
        super().__init__()
        self.running, self.idle, self.exit_minutes, self.busy_n = True, 0, 20, 0
        self.inflight_n, self.age, self.marks, self.unload_error = 0, None, 0, None

    def unload_inputs(self, fresh=False):
        if not self.running:
            return False, None
        return True, {"status": "ok", "idleSeconds": self.idle, "idleExitMinutes": self.exit_minutes, "busy": self.busy_n}

    def inflight(self):
        return self.inflight_n

    def interactive_age(self):
        return self.age

    def mark_interactive(self):
        self.marks += 1
        self.age = 0

    def unload(self):
        self.unloads += 1
        if self.unload_error:
            raise self.unload_error
        was, self.running = self.running, False
        return ["immich_aitagger"] if was else []


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Idle(Base):
    """A real Indexer on IdleServices, looking every few milliseconds."""

    def setUp(self):
        super().setUp()
        self.services = IdleServices()
        self.make()

    def make(self, **kw):
        self.indexer = at.Indexer(self.store, self.services, client=self.client, catalog=lambda: self.catalog,
                                  frames=fake_frames, **kw)
        self.indexer.DROP_WAIT = 0.01
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)

    def up_to_date(self, **settings):
        """Tag everything and leave the indexer waiting for new photos."""
        self.set(keep_updated=True, indexing=True, **settings)
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting), self.indexer.detail)

    def quiet(self, seconds=0.25):
        time.sleep(seconds)


class TestUnloadAfterTheWork(Idle):
    def test_the_models_are_stopped_once_the_tagger_has_been_idle_for_unload_after_minutes(self):
        self.services.idle = 30
        self.up_to_date()
        self.quiet()
        self.assertEqual(self.services.unloads, 0)
        self.assertEqual(self.store.counts()["processed"], 3)          # the work is done
        self.services.idle = 125
        self.assertTrue(wait_for(lambda: self.services.unloads == 1))
        self.assertFalse(self.services.running)
        self.assertTrue(self.indexer.running())                        # it goes on watching for new photos
        self.assertFalse(self.indexer.unload_status()["loaded"])
        self.quiet()
        self.assertEqual(self.services.unloads, 1)

    def test_never_while_a_request_is_in_flight_or_the_tagger_says_busy(self):
        self.services.idle, self.services.inflight_n = 500, 1
        self.up_to_date()
        self.quiet()
        self.assertEqual(self.services.unloads, 0)
        self.services.inflight_n, self.services.busy_n = 0, 2          # the tagger's own counter (a request from elsewhere)
        self.quiet()
        self.assertEqual(self.services.unloads, 0)
        self.assertTrue(self.indexer.unload_status()["busy"])
        self.services.busy_n = 0
        self.assertTrue(wait_for(lambda: self.services.unloads == 1))

    def test_never_while_the_indexer_is_working(self):
        gate = threading.Event()

        def slow(item, n):
            gate.wait(5)
            return fake_frames(item, n)

        self.indexer.frames = slow
        self.services.idle = 500
        self.set(keep_updated=True, indexing=True)
        self.indexer.start()
        self.quiet(0.3)
        self.assertEqual(self.services.unloads, 0)
        status = self.indexer.unload_status()
        self.assertEqual((status["loaded"], status["busy"], status["unloadInSeconds"]), (True, True, None))
        gate.set()
        self.assertTrue(wait_for(lambda: self.services.unloads == 1))
        self.assertEqual(self.store.counts()["processed"], 3)          # the work came first

    def test_not_within_the_interactive_grace_of_a_test_card_use(self):
        self.services.idle = 500
        self.up_to_date()
        self.assertTrue(wait_for(lambda: self.services.unloads == 1))   # (no grace yet: nobody used the Test card)
        self.services.running, self.services.unloads = True, 0
        self.indexer.test(self.ids[0])                                  # a preview: interactive use
        self.assertEqual(self.services.marks, 2)                        # before and after
        self.services.idle, self.services.age = 500, 600
        self.quiet()
        self.assertEqual(self.services.unloads, 0)
        status = self.indexer.unload_status()
        self.assertEqual((status["rule"], status["unloadInSeconds"]), ("interactive", 600))
        self.services.idle, self.services.age = 200, 1250               # the 20 minutes are over
        self.assertTrue(wait_for(lambda: self.services.unloads == 1))

    def test_the_unload_after_setting_decides(self):
        self.services.idle = 130
        self.up_to_date(unload_after=5)
        self.quiet()
        self.assertEqual(self.services.unloads, 0)
        self.services.idle = 305
        self.assertTrue(wait_for(lambda: self.services.unloads == 1))

    def test_a_paused_indexer_leaves_it_to_the_server(self):
        self.services.idle = 500
        status = self.indexer.unload_status()                           # no thread
        self.assertEqual((status["rule"], status["unloadInSeconds"], status["busy"]), ("server", 700, False))
        self.run_indexer()                                              # works, winds down (idle 500: stops it), ends
        self.services.running, self.services.unloads = True, 0
        self.quiet()
        self.assertEqual(self.services.unloads, 0)                      # nobody is looking any more

    def test_a_failing_stop_does_not_end_the_indexer(self):
        self.services.idle, self.services.unload_error = 500, OSError("docker is not answering")
        self.up_to_date()
        self.assertTrue(wait_for(lambda: self.services.unloads >= 2))
        self.assertTrue(self.indexer.running())
        self.assertEqual(self.indexer.state, "done")

    def test_a_new_photo_after_the_unload_is_tagged_again(self):
        self.services.idle = 500
        self.up_to_date()
        self.assertTrue(wait_for(lambda: self.services.unloads == 1))
        extra = {"id": self.ids[5], "type": "IMAGE", "taken": "2026-02-01 10:00:00", "name": "n.jpg",
                 "preview": DOG, "original": "", "duration_ms": 0}
        self.catalog = self.catalog + [extra]
        self.indexer.start()                                            # "look again now"
        self.assertTrue(wait_for(lambda: self.store.counts()["processed"] == 4))
        self.assertTrue(self.services.ready_calls)

    def test_a_stand_in_that_cannot_say_is_never_unloaded(self):
        self.services = FakeServices()
        self.make()
        self.up_to_date()
        self.quiet()
        self.assertEqual(self.services.unloads, 0)
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": False, "idleSeconds": None, "unloadInSeconds": None, "rule": None, "busy": False})


class TestWhenKeepUpdatedIsOff(Idle):
    """With "keep it up to date" off the thread ends after the work, but not before the card is freed."""

    def finished(self):
        return not self.indexer.running()

    def start(self):
        self.set(keep_updated=False, indexing=True)
        self.indexer.start()

    def test_the_card_is_freed_before_the_thread_ends(self):
        self.services.idle = 500
        self.start()
        self.assertTrue(wait_for(self.finished))
        self.assertEqual((self.indexer.state, self.services.unloads), ("done", 1))

    def test_it_waits_out_the_short_rule(self):
        self.services.idle = 30
        self.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.quiet()
        self.assertTrue(self.indexer.running())
        self.assertEqual(self.services.unloads, 0)
        self.services.idle = 125
        self.assertTrue(wait_for(self.finished))
        self.assertEqual(self.services.unloads, 1)

    def test_an_interactive_grace_leaves_it_to_the_server(self):
        self.services.idle, self.services.age = 500, 600
        self.start()
        self.assertTrue(wait_for(self.finished))
        self.assertEqual(self.services.unloads, 0)

    def test_a_restart_wakes_it_for_new_work(self):
        self.services.idle = 30
        self.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.store.retry_failed()
        self.indexer.start()                                            # Try again: picks the two failures up again
        self.assertTrue(wait_for(lambda: self.store.counts()["retrying"] == 2 and self.indexer.waiting))
        self.assertEqual(self.services.unloads, 0)

    def test_pausing_ends_the_wait_at_once(self):
        self.services.idle = 30
        self.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.indexer.stop(5)
        self.assertTrue(self.finished())
        self.assertEqual(self.services.unloads, 0)

    def test_a_test_card_request_still_in_flight_keeps_it_waiting(self):
        self.services.idle, self.services.inflight_n = 500, 1
        self.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.quiet()
        self.assertTrue(self.indexer.running())
        self.assertEqual(self.services.unloads, 0)
        self.services.inflight_n = 0
        self.assertTrue(wait_for(self.finished))
        self.assertEqual(self.services.unloads, 1)


class TestUnloadStatusObject(Idle):
    """The ``unload`` object of GET /api/aitagger, for each case."""

    def test_not_loaded(self):
        self.services.running = False
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": False, "idleSeconds": None, "unloadInSeconds": None, "rule": None, "busy": False})

    def test_waiting_for_new_photos(self):
        self.services.idle = 30
        self.up_to_date()
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 90, "rule": "after-work", "busy": False})

    def test_after_an_interactive_use(self):
        self.services.idle, self.services.age = 30, 30
        self.up_to_date()
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 1170, "rule": "interactive", "busy": False})

    def test_paused(self):
        self.services.idle = 30
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": 1170, "rule": "server", "busy": False})

    def test_busy_by_the_tagger_or_by_the_panel(self):
        self.services.idle, self.services.busy_n = 30, 1
        self.up_to_date()
        self.assertEqual(self.indexer.unload_status(),
                         {"loaded": True, "idleSeconds": 30, "unloadInSeconds": None, "rule": "after-work", "busy": True})
        self.services.busy_n, self.services.inflight_n = 0, 1
        self.assertTrue(self.indexer.unload_status()["busy"])

    def test_the_tagger_status_route_fields_are_unchanged(self):
        self.assertEqual(set(self.services.status()), {"tagger", "gpu", "searchplusRunning"})


class TestTheNewSettings(Base):
    def test_defaults_limits_and_that_they_are_not_content(self):
        s = at.load_settings()
        self.assertEqual((s["unload_after"], s["check_every"]), (2, 1))
        self.assertEqual((at.LIMITS["unload_after"], at.LIMITS["check_every"]), ((1, 60), (1, 60)))
        self.assertEqual((sp.LIMITS["unload_after"], sp.LIMITS["check_every"]), ((1, 60), (1, 60)))
        for key in ("unload_after", "check_every"):
            self.assertNotIn(key, at.CONTENT)
            self.assertFalse(any(key in keys for keys in at.REPROCESS.values()))
        self.assertEqual(at.suggest_mode(["unload_after", "check_every"]), "none")

    def test_saving_them_makes_nothing_outdated(self):
        version = self.store.settings_version
        settings, changed = at.apply_settings({"unload_after": 7, "check_every": 15}, self.store)
        self.assertEqual((settings["unload_after"], settings["check_every"], changed), (7, 15, []))
        self.assertEqual(self.store.settings_version, version)
        self.assertEqual(at.load_settings()["check_every"], 15)

    def test_they_are_whole_minutes_from_1_to_60(self):
        for key in ("unload_after", "check_every"):
            for bad in (0, 61, -5, 1.5, "2", True, None, float("nan")):
                with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                    at.validate_setting(key, bad)
            for good in (1, 2, 60, 5.0):
                self.assertEqual(at.validate_setting(key, good), int(good))
        with self.assertRaises(ValueError):
            at.apply_settings({"unload_after": 0, "max_tags": 20}, self.store)                  # all or nothing
        self.assertEqual(at.load_settings()["max_tags"], 30)

    def test_a_hand_edited_value_falls_back_to_the_default(self):
        at.settings_path().write_text(json.dumps({"unload_after": 0, "check_every": "soon"}))
        s = at.load_settings()
        self.assertEqual((s["unload_after"], s["check_every"]), (2, 1))

    def test_an_old_settings_file_without_them_gets_the_defaults(self):
        at.settings_path().write_text(json.dumps({"max_tags": 12}))
        s = at.load_settings()
        self.assertEqual((s["max_tags"], s["unload_after"], s["check_every"]), (12, 2, 1))


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

        self.services = IdleServices()
        self.indexer = at.Indexer(self.store, self.services, client=self.client, catalog=catalog, frames=fake_frames,
                                  clock=self.clock, probe=probe)
        self.indexer.DROP_WAIT = 0.01
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)
        self.set(keep_updated=True, indexing=True)

    def up_to_date(self):
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting), self.indexer.detail)

    def advance(self, seconds):
        self.clock.t += seconds

    def test_the_first_look_reads_everything(self):
        self.up_to_date()
        self.assertEqual((len(self.reads), len(self.probes)), (1, 1))
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_an_unchanged_probe_reads_nothing_however_often_it_looks(self):
        self.up_to_date()
        for _ in range(4):
            self.advance(61)
            self.assertTrue(wait_for(lambda n=len(self.probes): len(self.probes) > n))
        self.assertEqual(len(self.reads), 1)

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
        extra = {"id": self.ids[5], "type": "IMAGE", "taken": "2026-02-01 10:00:00",
                 "name": "n.jpg", "preview": DOG, "original": "", "duration_ms": 0}
        self.catalog = self.catalog + [extra]
        self.signature[0] = "B"
        self.advance(61)
        self.assertTrue(wait_for(lambda: self.store.counts()["processed"] == 4))
        self.assertEqual(len(self.reads), 2)
        self.advance(61)                                                # nothing changed since: no more reads
        self.assertTrue(wait_for(lambda: len(self.probes) == 3))
        time.sleep(0.1)
        self.assertEqual(len(self.reads), 2)

    def test_check_every_is_a_setting(self):
        self.up_to_date()
        self.set(check_every=5)
        self.signature[0] = "B"
        self.advance(4 * 60)
        time.sleep(0.2)
        self.assertEqual(len(self.reads), 1)
        self.advance(61)
        self.assertTrue(wait_for(lambda: len(self.reads) == 2))

    def test_a_safety_read_every_hour_even_when_the_probe_never_changes(self):
        self.up_to_date()
        self.advance(3599)
        self.assertTrue(wait_for(lambda: len(self.probes) >= 2))
        time.sleep(0.1)
        self.assertEqual(len(self.reads), 1)
        self.advance(2)
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
        self.advance(3600)
        self.assertTrue(wait_for(lambda: len(self.reads) == 2))

    def test_start_and_the_test_card_look_at_once(self):
        self.up_to_date()
        self.signature[0] = "B"
        self.indexer.start()
        self.assertTrue(wait_for(lambda: len(self.reads) == 2))
        self.signature[0] = "C"
        self.indexer.refresh_catalog()                                  # what the sample / Test routes use: a full read now,
        self.assertEqual(len(self.reads), 3)                            # and the probe's answer from before it is the baseline
        self.advance(61)                                                # nothing changed since it: the probe agrees
        self.assertTrue(wait_for(lambda: len(self.probes) >= 4))
        time.sleep(0.1)
        self.assertEqual(len(self.reads), 3)

    def test_the_real_probe_goes_with_the_real_catalogue_only(self):
        store = at.Store(Path(self.tmp.name) / "probe")
        self.addCleanup(store.conn.close)
        self.assertIs(at.Indexer(store, FakeServices())._watch.probe, sp.fetch_probe)
        self.assertIsNone(at.Indexer(store, FakeServices(), catalog=lambda: [])._watch.probe)


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
            if item["preview"] == "":                                  # as prepare_captures does: the picture itself
                return [DOG.encode()], "image"
            return fake_frames(item, n)

        self.services = IdleServices()
        self.indexer = at.Indexer(self.store, self.services, client=self.client, catalog=lambda: self.catalog, frames=frames)
        self.indexer.DROP_WAIT = 0.01
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)
        self.dog = self.catalog[1]                                      # a new upload Immich is still working on
        self.held = {**self.dog, "preview": "", "added": self.T0 - 60}
        self.catalog[1] = self.held

    def work(self):
        return [item["id"] for item in self.store.work(50)]

    def test_a_new_asset_without_a_preview_is_left_out_of_the_work_list(self):
        self.store.sync_catalog(self.catalog)
        self.assertNotIn(self.ids[1], self.work())
        self.assertIn(self.ids[0], self.work())
        self.assertEqual(self.store.held(), 1)

    def test_it_is_held_not_failed_and_picked_up_when_the_preview_appears(self):
        self.set(keep_updated=True, indexing=True)
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        self.assertNotIn(self.ids[1], self.asked)
        self.assertIsNone(self.store.conn.execute("select 1 from failed where id=?", (self.ids[1],)).fetchone())
        self.assertIn("1 new item is waiting for Immich", self.indexer.detail)
        self.assertEqual(self.store.counts()["processed"], 2)
        self.catalog[1] = {**self.held, "preview": DOG}                   # Immich made the preview
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.store.counts()["processed"] == 3))
        self.assertIn(self.ids[1], self.asked)
        self.assertEqual(self.store.held(), 0)

    def test_after_thirty_minutes_without_a_preview_it_is_processed_as_before(self):
        self.store.sync_catalog(self.catalog)
        self.now[0] = self.T0 - 60 + at.searchplus.PREVIEW_GRACE - 1
        self.assertNotIn(self.ids[1], self.work())
        self.now[0] = self.T0 - 60 + at.searchplus.PREVIEW_GRACE + 1
        self.assertIn(self.ids[1], self.work())
        self.assertEqual(self.store.held(), 0)
        self.services.idle = 500                                           # (so that the run may end: the card is freed first)
        self.run_indexer()
        self.assertIn(self.ids[1], self.asked)                            # it fell back to the file itself
        self.assertEqual(self.store.counts()["processed"], 3)

    def test_queued_work_waits_too(self):
        self.store.sync_catalog(self.catalog)
        self.store.enqueue([self.ids[1]], "full")
        self.assertNotIn(self.ids[1], self.work())
        self.now[0] += at.searchplus.PREVIEW_GRACE
        self.assertIn(self.ids[1], self.work())

    def test_assets_of_unknown_age_and_older_assets_are_never_held(self):
        self.catalog[1] = {**self.held, "added": 0}
        self.catalog[2] = {**self.catalog[2], "preview": "", "added": self.T0 - 7200}
        self.store.sync_catalog(self.catalog)
        self.assertTrue({self.ids[1], self.ids[2]} <= set(self.work()))

    def test_the_same_goes_for_videos(self):
        self.catalog[2] = {**self.catalog[2], "preview": "", "added": self.T0 - 5}
        self.store.sync_catalog(self.catalog)
        self.assertNotIn(self.ids[2], self.work())
        self.catalog[2] = {**self.catalog[2], "preview": CLIP}
        self.store.sync_catalog(self.catalog)
        self.assertIn(self.ids[2], self.work())

    def test_a_store_made_before_this_gets_the_column_and_nothing_is_held(self):
        folder = Path(self.tmp.name) / "old"
        folder.mkdir()
        conn = sqlite3.connect(str(folder / "tagger.sqlite"))
        conn.executescript("create table assets (id text primary key, type text, taken text, name text, preview text,"
                           " original text, duration_ms integer default 0, gone integer default 0);"
                           "insert into assets (id, type, taken, name, preview, original) values ('old1','IMAGE','2024','o','','');")
        conn.commit()
        conn.close()
        store = at.Store(folder)
        self.addCleanup(store.conn.close)
        self.assertEqual([i["id"] for i in store.work(5)], ["old1"])      # added = 0: unknown, not held
        store.sync_catalog([{**self.held, "id": "old1"}])
        self.assertEqual(store.work(5), [])
        self.assertEqual(store.held(), 1)


class TestServicesKeepTrack(Base):
    """The facts the unload rules need from the real Services: requests in flight, the tagger's health, interactive use."""

    def setUp(self):
        super().setUp()
        self.clock, self.runner = Clock(), FakeRunner()
        self.runner.state = {"immich_aitagger": "running"}
        self.tagger = mock.Mock()
        self.tagger.health.return_value = {"status": "ok", "idleSeconds": 7, "idleExitMinutes": 20, "busy": 0}
        self.svc = at.Services(self.tagger, runner=self.runner, store=self.store, clock=self.clock, sleep=self.clock.sleep)

    def test_requests_in_flight_are_counted_and_released(self):
        seen = []

        def tag(images, models=None):
            seen.append(self.svc.inflight())
            if models == ["boom"]:
                raise at.ServiceDown("gone")
            return [], []

        self.tagger.tag.side_effect = tag
        self.svc.tag([b"x"], models=["wd"])
        with self.assertRaises(sp.ServiceDown):
            self.svc.tag([b"x"], models=["boom"])
        self.assertEqual(seen, [1, 1])
        self.assertEqual(self.svc.inflight(), 0)

    def test_the_health_is_remembered_for_a_few_seconds_but_a_decision_looks_fresh(self):
        self.assertEqual(self.svc.tagger_health()["idleSeconds"], 7)
        for _ in range(4):
            self.svc.tagger_health()
        self.assertEqual(self.tagger.health.call_count, 1)
        self.clock.t += at.STATE_TTL + 1
        self.svc.tagger_health()
        self.assertEqual(self.tagger.health.call_count, 2)
        self.svc.tagger_health(fresh=True)
        self.assertEqual(self.tagger.health.call_count, 3)

    def test_the_status_route_and_the_unload_object_share_one_health_call(self):
        self.svc.status()
        self.svc.unload_inputs()
        self.svc.unload_inputs()
        self.assertEqual(self.tagger.health.call_count, 1)
        self.assertEqual([c[-1] for c in self.runner.commands("inspect")], ["immich_aitagger", "immich_searchplus"])  # once each

    def test_interactive_use_is_remembered_with_its_age(self):
        self.assertIsNone(self.svc.interactive_age())
        self.svc.mark_interactive()
        self.clock.t += 90
        self.assertEqual(self.svc.interactive_age(), 90)

    def test_unload_inputs(self):
        self.assertEqual(self.svc.unload_inputs(), (True, {"status": "ok", "idleSeconds": 7, "idleExitMinutes": 20, "busy": 0}))
        self.runner.state["immich_aitagger"] = "stopped"
        self.assertEqual(self.svc.unload_inputs(fresh=True), (False, None))
        self.tagger.health.reset_mock()
        self.assertEqual(self.svc.unload_inputs(), (False, None))
        self.tagger.health.assert_not_called()                              # a stopped container is not asked


class TestUnloadWithTheRealServices(Base):
    """The whole path to ``docker stop``, with the real Services over a stand-in tagger and a fake docker."""

    def test_the_tagger_container_is_stopped_after_the_work_and_nothing_else(self):
        idle, busy = [30], [0]

        def tag(body):
            got = [read_picture(base64.b64decode(i), body.get("models")) for i in body["images"]]
            return 200, {"results": [g[0] for g in got], "errors": [g[1] for g in got], "tookMs": 1}

        def health(_):
            return 200, {"status": "ok", "error": None, "idleSeconds": idle[0], "idleExitMinutes": 20, "busy": busy[0]}

        tagger = StubServer({("POST", "/tag"): tag, ("GET", "/health"): health})
        self.addCleanup(tagger.close)
        runner = FakeRunner()
        runner.state = {"immich_aitagger": "running", "immich_searchplus": "running"}
        services = at.Services(at.Tagger(tagger.url), runner=runner, store=self.store)
        self.indexer = at.Indexer(self.store, services, client=self.client, catalog=lambda: self.catalog, frames=fake_frames)
        self.indexer.DROP_WAIT = 0.01
        self.indexer.IDLE_POLL = 0.02
        self.addCleanup(self.indexer.stop, 5)
        self.set(keep_updated=True, indexing=True)
        self.indexer.start()
        self.assertTrue(wait_for(lambda: self.indexer.state == "done" and self.indexer.waiting))
        time.sleep(0.2)
        self.assertEqual(runner.commands("stop"), [])                        # 30 s idle: not yet
        status = self.indexer.unload_status()
        self.assertEqual((status["loaded"], status["rule"], status["busy"]), (True, "after-work", False))
        self.assertLessEqual(status["unloadInSeconds"], 90)
        busy[0] = 1                                                          # the tagger is serving someone else
        idle[0] = 500
        time.sleep(0.2)
        self.assertEqual(runner.commands("stop"), [])
        busy[0] = 0
        self.assertTrue(wait_for(lambda: runner.commands("stop")))
        self.assertEqual(runner.commands("stop"), [["docker", "stop", "-t", "20", "immich_aitagger"]])
        self.assertEqual(runner.state, {"immich_aitagger": "stopped", "immich_searchplus": "running"})   # Search+ left alone
        self.assertFalse(self.indexer.unload_status()["loaded"])


if __name__ == "__main__":
    unittest.main()
