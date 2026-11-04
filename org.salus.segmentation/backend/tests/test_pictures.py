"""Plain pictures (JPEG, PNG, ...) open like slides: through Pillow, with one level."""
import asyncio
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import export  # noqa: E402
import smartbrush as sb  # noqa: E402
from errors import ViewerError  # noqa: E402
from slides import Slides  # noqa: E402


class PictureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        rgb = np.zeros((3000, 5000, 3), np.uint8)
        rgb[:, :2500] = (200, 40, 40)  # red left half, blue right half
        rgb[:, 2500:] = (40, 40, 200)
        self.picture = Image.fromarray(rgb)
        self.slides = Slides()

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, name):
        path = self.dir / name
        self.picture.save(path)
        return str(path)

    def test_jpeg_and_png_open_and_give_tiles(self):
        for name in ("a.jpg", "b.png", "c.bmp"):
            slide = self.slides.open(self.save(name), viewer=1)
            info = slide.info()
            self.assertEqual((info["width"], info["height"]), (5000, 3000), name)
            dz = slide.dz
            tile = asyncio.run(self.slides.tile(slide.id, str(dz.level_count - 1), "0_0.jpeg"))  # full resolution
            self.assertEqual(Image.open(io.BytesIO(tile)).getpixel((10, 10))[0] > 150, True)
            asyncio.run(self.slides.tile(slide.id, "8", "0_0.jpeg"))  # zoomed out: from the overview
            self.assertEqual(export.image_size(Path(slide.path))[:2], (5000, 3000))

    def test_the_smart_brush_reads_pictures_from_several_threads(self):
        slide = self.slides.open(self.save("a.png"), viewer=1)
        views = []
        threads = [threading.Thread(target=lambda x=x: views.append(sb.read_view(slide, x, 1000, 0.5, 256)))
                   for x in (1000, 3000, 2400, 100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(views), 4)
        self.assertTrue(all(v.shape == (256, 256, 3) for v in views))
        left = sb.read_view(slide, 100, 1000, 0.5, 64)
        self.assertGreater(int(left[..., 0].mean()), 150)  # red

    def test_zoomed_out_views_past_the_edge_are_white_there(self):
        # 1 screen px = 8 image px, so the view is cut from the overview; it starts 2000 px left of and above the image.
        slide = self.slides.open(self.save("a.jpg"), viewer=1)
        view = sb.read_view(slide, -2000.0, -2000.0, 0.125, 1024)
        self.assertEqual(view.shape, (1024, 1024, 3))
        self.assertTrue((view[:200, :200] == 255).all())  # outside: white
        self.assertGreater(int(view[300:600, 300:500, 0].mean()), 150)  # inside, left half: red
        self.assertLess(int(view[300:600, 300:500, 2].mean()), 100)
        self.assertTrue((sb.read_view(slide, 9000.0, 9000.0, 0.125, 256) == 255).all())  # entirely outside
        right = sb.read_view(slide, 4500.0, 2500.0, 0.125, 1024)  # past the right and bottom edges
        self.assertTrue((right[-100:, -100:] == 255).all())
        self.assertGreater(int(right[:30, :30, 2].mean()), 150)  # the image's blue corner

    def test_unreadable_files_are_refused(self):
        bad = self.dir / "x.jpg"
        bad.write_bytes(b"not a picture")
        with self.assertRaises(ViewerError):
            self.slides.open(str(bad), viewer=1)


if __name__ == "__main__":
    unittest.main()
