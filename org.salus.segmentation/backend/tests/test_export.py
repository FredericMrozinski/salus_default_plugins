import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pyvips  # noqa: E402
from shapely.geometry import Polygon  # noqa: E402

import export  # noqa: E402
from annotations import Annotations, Shape  # noqa: E402
from errors import ViewerError  # noqa: E402
from project import Project  # noqa: E402

RED, GREEN, BLUE = "#ff0000", "#00ff00", "#0000ff"


def square(x, y, size):
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def pixel(image, x, y):
    return [int(v) for v in image.getpoint(x, y)]


class RenderTest(unittest.TestCase):
    def test_colors_priority_holes_and_background(self):
        shapes = [
            Shape("a", 1, square(10, 10, 50)),
            Shape("b", 2, square(40, 40, 50)),  # overlaps a at 40..60
            Shape("c", 1, Polygon(square(100, 0, 60).exterior.coords, [square(120, 20, 20).exterior.coords])),  # with a hole
        ]
        classes = [{"id": 2, "color": GREEN}, {"id": 1, "color": RED}]  # class 2 first: it wins
        image = export.render(shapes, (200, 120), 1, classes, BLUE)
        self.assertEqual((image.width, image.height, image.bands), (200, 120, 3))
        self.assertEqual(pixel(image, 20, 20), [255, 0, 0])
        self.assertEqual(pixel(image, 50, 50), [0, 255, 0])  # the overlap
        self.assertEqual(pixel(image, 80, 80), [0, 255, 0])
        self.assertEqual(pixel(image, 5, 5), [0, 0, 255])  # background
        self.assertEqual(pixel(image, 110, 10), [255, 0, 0])
        self.assertEqual(pixel(image, 130, 30), [0, 0, 255])  # the hole
        classes.reverse()  # class 1 first now
        self.assertEqual(pixel(export.render(shapes, (200, 120), 1, classes, BLUE), 50, 50), [255, 0, 0])

    def test_excluded_classes_and_downsampling(self):
        shapes = [Shape("a", 1, square(100, 100, 100)), Shape("b", 2, square(300, 300, 50))]
        image = export.render(shapes, (401, 400), 4, [{"id": 1, "color": RED}], "#000000")
        self.assertEqual((image.width, image.height), (101, 100))
        self.assertEqual(pixel(image, 35, 35), [255, 0, 0])
        self.assertEqual(pixel(image, 80, 80), [0, 0, 0])  # class 2 is not exported

    def test_tiles_join_seamlessly(self):
        size = export.TILE + 300
        shapes = [Shape("a", 1, square(export.TILE - 100, export.TILE - 100, 200))]  # across four tiles
        image = export.render(shapes, (size, size), 1, [{"id": 1, "color": RED}], "#000000")
        for x, y in [(export.TILE - 50, export.TILE - 50), (export.TILE + 50, export.TILE - 50), (export.TILE + 50, export.TILE + 50)]:
            self.assertEqual(pixel(image, x, y), [255, 0, 0])
        self.assertEqual(pixel(image, 10, 10), [0, 0, 0])
        self.assertEqual((image.width, image.height), (size, size))  # no padding from the tile grid


class ExportJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "images/sub").mkdir(parents=True)
        for rel in ("one.tif", "sub/two.tif"):  # small tiled TIFFs that OpenSlide opens as generic-tiff
            pyvips.Image.black(1200, 900, bands=3).cast("uchar").tiffsave(str(self.dir / "images" / rel), tile=True, pyramid=True)
        self.project = Project.create("P", self.dir / "images", ".tif", self.dir / "save")
        self.tumor = self.project.add_class("Tumor", RED)["id"]
        self.stroma = self.project.add_class("Stroma", GREEN)["id"]
        Annotations(self.project, "one.tif").add_polygon([[100, 100], [400, 100], [400, 400], [100, 400]], self.tumor)

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, **changes):
        base = self.project.export_settings()
        base.update(folder=str(self.dir / "out"), **changes)
        return self.project.save_export_settings(base)

    def run_job(self, images, overwrite=False, **changes):
        job = export.ExportJob(self.project, images, self.settings(**changes), overwrite, lambda i: export.load_shapes(self.project, i))
        job.start()
        job.thread.join(60)
        return job

    def test_exports_pyramidal_maps_and_a_legend(self):
        job = self.run_job(["one.tif", "sub/two.tif"])
        self.assertEqual([i["status"] for i in job.items], ["done", "done"])
        out = pyvips.Image.new_from_file(str(self.dir / "out/one.tif"))
        self.assertEqual((out.width, out.height), (1200, 900))
        self.assertGreater(out.get("n-pages"), 1)  # a pyramid
        self.assertEqual(pixel(out, 200, 200), [255, 0, 0])
        self.assertTrue((self.dir / "out/sub/two.tif").exists())
        legend = json.loads((self.dir / "out/classes.json").read_text())
        self.assertEqual([c["name"] for c in legend["classes"]], ["Tumor", "Stroma"])
        self.assertEqual(legend["classes"][0]["rgb"], [255, 0, 0])
        self.assertEqual(legend["background"]["color"], "#000000")
        self.assertEqual(oct((self.dir / "out/classes.json").stat().st_mode & 0o777), "0o644")
        self.assertFalse(list((self.dir / "out").rglob("*.part")))

    def test_existing_files_are_skipped_unless_overwriting(self):
        self.run_job(["one.tif"])
        self.assertEqual(self.run_job(["one.tif"]).items[0]["status"], "skipped")
        self.assertEqual(self.run_job(["one.tif"], overwrite=True).items[0]["status"], "done")

    def test_settings_are_checked_and_kept(self):
        classes = self.project.export_settings()["classes"]
        classes[1]["color"] = RED
        with self.assertRaises(ViewerError):
            self.settings(classes=classes)  # two classes with one color
        classes[1]["include"] = False
        self.settings(classes=classes)  # fine when one of them is not exported
        with self.assertRaises(ViewerError):
            self.settings(background=RED, classes=self.project.export_settings()["classes"])
        with self.assertRaises(ViewerError):
            self.settings(downsample=3)
        again = Project.open(self.dir / "save").export_settings()
        self.assertFalse(again["classes"][1]["include"])
        self.assertEqual(again["folder"], str(self.dir / "out"))

    def test_cancel(self):
        job = export.ExportJob(self.project, ["one.tif", "sub/two.tif"], self.settings(), False, lambda i: export.load_shapes(self.project, i))
        job.cancel()
        job.start()
        job.thread.join(30)
        self.assertEqual(job.state, "cancelled")
        self.assertFalse((self.dir / "out/one.tif").exists())


if __name__ == "__main__":
    unittest.main()
