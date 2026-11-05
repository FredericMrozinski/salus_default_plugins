import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import slides  # noqa: E402
from errors import ViewerError  # noqa: E402


REAL_SLIDE = slides.Slide


class FakeOpenSlide:
    dimensions = (100, 100)

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def bare_slide(path: Path) -> slides.Slide:
    """A Slide around a fake OpenSlide handle whose tiles are rendered by `_render`."""
    slide = REAL_SLIDE.__new__(REAL_SLIDE)
    slide.path, slide.id, slide.slide = path, slides._slide_id(path), FakeOpenSlide()
    slide._overview, slide._overview_lock = "overview", None
    slide.viewers, slide._state, slide._active, slide._closing = set(), threading.Lock(), 0, False
    slide.dz = SimpleNamespace(level_count=1)
    slide._render = lambda level, col, row: b"tile"
    return slide


class SlideCloseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "a.svs"
        self.file.write_bytes(b"x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_close_frees_handle_and_overview_and_refuses_renders(self):
        slide = bare_slide(self.file)
        self.assertEqual(slide.render(0, 0, 0), b"tile")
        slide.close()
        self.assertTrue(slide.slide.closed)
        self.assertIsNone(slide._overview)
        with self.assertRaises(ViewerError):
            slide.render(0, 0, 0)

    def test_close_waits_for_a_running_render(self):
        slide = bare_slide(self.file)

        def render_during_close(level, col, row):
            slide.close()
            self.assertFalse(slide.slide.closed, "the file must stay open while a tile is being read")
            return b"tile"

        slide._render = render_during_close
        self.assertEqual(slide.render(0, 0, 0), b"tile")
        self.assertTrue(slide.slide.closed)


class SlidesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a = Path(self.tmp.name) / "a.svs"
        self.b = Path(self.tmp.name) / "b.svs"
        self.a.write_bytes(b"x")
        self.b.write_bytes(b"y")
        self.saved = slides.openslide, slides.Slide
        slides.openslide = SimpleNamespace(OpenSlideError=Exception)
        slides.Slide = bare_slide
        self.slides = slides.Slides()

    def tearDown(self):
        slides.openslide, slides.Slide = self.saved
        self.tmp.cleanup()

    def test_a_slide_is_closed_when_its_last_viewer_goes(self):
        first = self.slides.open(str(self.a), viewer=1)
        self.assertIs(self.slides.open(str(self.a), viewer=2), first)
        self.slides.open(str(self.a), viewer=2)  # the same viewer asking again does not count twice
        self.slides.release(1)
        self.assertFalse(first.slide.closed)
        self.assertIn(first.id, self.slides.by_id)
        self.slides.release(2)
        self.assertTrue(first.slide.closed)
        self.assertEqual((self.slides.by_id, self.slides._by_path), ({}, {}))

    def test_only_the_slides_of_that_viewer_are_closed_and_their_tiles_dropped(self):
        a = self.slides.open(str(self.a), viewer=1)
        b = self.slides.open(str(self.b), viewer=2)
        self.slides.cache._entries[(a.id, 0, 0, 0)] = b"ta"
        self.slides.cache._entries[(b.id, 0, 0, 0)] = b"tb"
        self.slides.cache.bytes = 4
        self.slides.release(1)
        self.assertTrue(a.slide.closed)
        self.assertFalse(b.slide.closed)
        self.assertEqual(list(self.slides.cache._entries), [(b.id, 0, 0, 0)])
        self.assertEqual(self.slides.cache.bytes, 2)

    def test_reopening_after_release_gives_a_fresh_slide(self):
        first = self.slides.open(str(self.a), viewer=1)
        self.slides.release(1)
        second = self.slides.open(str(self.a), viewer=3)
        self.assertIsNot(first, second)
        self.assertFalse(second.slide.closed)

    def test_releasing_an_unknown_viewer_is_harmless(self):
        self.slides.release(99)


if __name__ == "__main__":
    unittest.main()


class LevelsSlide:
    """An OpenSlide handle with the given level sizes (metadata only)."""

    def __init__(self, levels):
        self.level_dimensions = levels
        self.closed = False

    def close(self):
        self.closed = True


class PyramidCheckTest(unittest.TestCase):
    """GET /slide/check: slides without a (complete) pyramid are slow to open; the viewer asks first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "a.svs"
        self.file.write_bytes(b"x")
        self.original = slides.openslide.OpenSlide

    def tearDown(self):
        slides.openslide.OpenSlide = self.original
        self.tmp.cleanup()

    def check(self, levels):
        handles = []
        slides.openslide.OpenSlide = lambda path: handles.append(LevelsSlide(levels)) or handles[-1]
        result = slides.Slides().check(str(self.file))
        self.assertTrue(handles[0].closed, "the file is let go of again")
        return result

    def test_a_pyramid_opens_without_asking(self):
        result = self.check([(100_000, 80_000), (25_000, 20_000), (6_250, 5_000), (1_562, 1_250)])
        self.assertEqual((result["pyramid"], result["warn"], result["levels"]), (True, False, 4))

    def test_a_large_slide_without_a_pyramid_warns(self):
        result = self.check([(40_000, 30_000)])
        self.assertEqual((result["pyramid"], result["warn"]), (False, True))
        self.assertEqual((result["width"], result["height"], result["smallest"]), (40_000, 30_000, [40_000, 30_000]))

    def test_a_pyramid_that_stops_too_early_warns(self):
        result = self.check([(100_000, 80_000), (25_000, 20_000)])  # the smallest level is still 25k px wide
        self.assertEqual((result["pyramid"], result["warn"]), (False, True))

    def test_a_small_slide_without_a_pyramid_does_not(self):
        result = self.check([(9_000, 9_000)])  # 81 MP: read quickly
        self.assertEqual((result["pyramid"], result["warn"]), (False, False))
        self.assertEqual(self.check([(4_000, 3_000)])["pyramid"], True)

    def test_an_open_slide_does_not_warn_again(self):
        manager = slides.Slides()
        slide = bare_slide(self.file)
        manager._by_path[str(self.file)] = slide
        slides.openslide.OpenSlide = lambda path: LevelsSlide([(40_000, 30_000)])
        self.assertFalse(manager.check(str(self.file))["warn"])

    def test_only_slide_files(self):
        other = Path(self.tmp.name) / "notes.txt"
        other.write_text("x")
        with self.assertRaises(ViewerError):
            slides.Slides().check(str(other))
