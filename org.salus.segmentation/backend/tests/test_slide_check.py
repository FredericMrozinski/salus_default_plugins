"""GET /slide/check: images without a (complete) pyramid are slow to open; the viewer asks first."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import slides  # noqa: E402


class LevelsSlide:
    def __init__(self, levels):
        self.level_dimensions = levels
        self.closed = False

    def close(self):
        self.closed = True


class PyramidCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.original = slides.open_image

    def tearDown(self):
        slides.open_image = self.original
        self.tmp.cleanup()

    def test_by_the_levels(self):
        file = self.dir / "a.svs"
        file.write_bytes(b"x")
        for levels, pyramid, warn in (
            ([(100_000, 80_000), (25_000, 20_000), (6_250, 5_000), (1_562, 1_250)], True, False),
            ([(40_000, 30_000)], False, True),  # no pyramid, 1.2 GP
            ([(100_000, 80_000), (25_000, 20_000)], False, True),  # stops too early
            ([(9_000, 9_000)], False, False),  # no pyramid, but small enough to be read quickly
            ([(3_000, 2_000)], True, False),  # a picture: one level, small
        ):
            slides.open_image = lambda path, levels=levels: LevelsSlide(levels)
            result = slides.Slides().check(str(file))
            self.assertEqual((result["pyramid"], result["warn"]), (pyramid, warn), levels)

    def test_a_real_tiled_tiff_without_a_pyramid(self):
        try:
            import pyvips
        except ImportError:
            self.skipTest("pyvips is not installed")
        flat, pyramid = self.dir / "flat.tif", self.dir / "pyramid.tif"
        image = pyvips.Image.black(6000, 5000, bands=3) + 200
        image.tiffsave(str(flat), tile=True, pyramid=False, compression="jpeg")
        image.tiffsave(str(pyramid), tile=True, pyramid=True, compression="jpeg")
        manager = slides.Slides()
        self.assertEqual((manager.check(str(flat))["pyramid"], manager.check(str(flat))["levels"]), (False, 1))
        self.assertTrue(manager.check(str(pyramid))["pyramid"])
        self.assertFalse(manager.check(str(flat))["warn"], "30 MP: below WARN_PIXELS")
        original = slides.WARN_PIXELS
        slides.WARN_PIXELS = 10_000_000
        try:
            self.assertTrue(manager.check(str(flat))["warn"])
        finally:
            slides.WARN_PIXELS = original

    def test_a_missing_file(self):
        with self.assertRaises(slides.ViewerError):
            slides.Slides().check(str(self.dir / "nope.svs"))


if __name__ == "__main__":
    unittest.main()
