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
