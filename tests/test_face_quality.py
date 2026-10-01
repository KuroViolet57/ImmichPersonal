import unittest

try:
    import numpy as np
    from PIL import Image, ImageFilter
except ImportError:  # optional dependency
    np = None

from immich_organizer import face_quality as fq


@unittest.skipIf(np is None, "numpy/Pillow not installed")
class TestSharpness(unittest.TestCase):
    def pattern(self, size):
        rng = np.random.default_rng(1)
        arr = (rng.random((size, size)) * 255).astype("uint8")
        return Image.fromarray(arr).convert("RGB")

    def face(self, w, h, box):
        return fq.Face("f", "a", "p", "", w, h, box, "x.jpg")

    def test_blur_and_tiny_faces_score_lower(self):
        sharp = self.pattern(200)
        blurred = sharp.filter(ImageFilter.GaussianBlur(3))
        tiny = self.pattern(20)
        box = self.face(200, 200, (0, 0, 200, 200))
        s_sharp = fq.face_sharpness(sharp, box)
        s_blur = fq.face_sharpness(blurred, box)
        s_tiny = fq.face_sharpness(tiny, self.face(20, 20, (0, 0, 20, 20)))
        self.assertGreater(s_sharp, s_blur * 5)
        self.assertGreater(s_sharp, s_tiny * 5)

    def test_box_is_scaled_to_the_preview_size(self):
        img = Image.new("RGB", (400, 400), "white")
        img.paste(self.pattern(100), (200, 200))
        # detector saw a 200x200 image; the face sits at 100..150 there
        on_face = fq.face_sharpness(img, self.face(200, 200, (100, 100, 150, 150)))
        off_face = fq.face_sharpness(img, self.face(200, 200, (0, 0, 50, 50)))
        self.assertGreater(on_face, 1000)
        self.assertEqual(off_face, 0.0)

    def test_dark_but_sharp_is_not_penalised(self):
        sharp = np.asarray(self.pattern(200).convert("L"), dtype=float)
        dark = Image.fromarray((sharp * 0.15).astype("uint8")).convert("RGB")
        bright = Image.fromarray(sharp.astype("uint8")).convert("RGB")
        box = self.face(200, 200, (0, 0, 200, 200))
        ratio = fq.face_sharpness(dark, box) / fq.face_sharpness(bright, box)
        self.assertGreater(ratio, 0.8)

    def test_degenerate_box(self):
        img = self.pattern(50)
        self.assertEqual(fq.face_sharpness(img, self.face(50, 50, (10, 10, 10, 30))), 0.0)

    def test_summary_picks_the_sharpest_face(self):
        faces = [fq.Face(str(i), f"asset{i}", "p1", "", 1, 1, (0, 0, 1, 1), "x") for i in range(3)]
        people = fq.summarise(faces, {"0": 5.0, "1": 50.0, "2": 20.0})
        self.assertEqual(people["p1"]["bestAssetId"], "asset1")
        self.assertEqual(people["p1"]["median"], 20.0)
        self.assertEqual(people["p1"]["faces"], 3)
