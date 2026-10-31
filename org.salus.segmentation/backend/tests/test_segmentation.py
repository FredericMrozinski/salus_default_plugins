import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from annotations import Annotations, count_shapes  # noqa: E402
from errors import ViewerError  # noqa: E402
from project import Project, Registry  # noqa: E402

SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "images/sub").mkdir(parents=True)
        for name in ("a.svs", "sub/b.SVS", "sub/c.tif", ".hidden.svs"):
            (self.dir / "images" / name).write_bytes(b"x")
        self.project = Project.create("P", self.dir / "images", ".svs", self.dir / "save")
        self.cls = self.project.add_class("Tumor", "#ff0000")

    def tearDown(self):
        self.tmp.cleanup()


class ProjectTest(Base):
    def test_images_are_found_by_type_recursively(self):
        self.assertEqual(self.project.images(), ["a.svs", "sub/b.SVS"])
        self.project.data["extensions"] = [".svs", ".tif"]
        self.assertEqual(self.project.images(rescan=True), ["a.svs", "sub/b.SVS", "sub/c.tif"])

    def test_linked_folders_are_followed_once(self):
        (self.dir / "elsewhere/deep").mkdir(parents=True)
        (self.dir / "elsewhere/deep/d.svs").write_bytes(b"x")
        (self.dir / "images/linked").symlink_to(self.dir / "elsewhere")
        (self.dir / "elsewhere/loop").symlink_to(self.dir / "images")  # a cycle back to the root
        images = self.project.images(rescan=True)
        self.assertEqual(images, ["a.svs", "linked/deep/d.svs", "sub/b.SVS"])
        self.assertTrue(self.project.image_path("linked/deep/d.svs").is_file())

    def test_project_reopens_from_its_save_folder(self):
        again = Project.open(self.dir / "save")
        self.assertEqual(again.id, self.project.id)
        self.assertEqual(again.data["classes"][0]["name"], "Tumor")
        with self.assertRaises(ViewerError):
            Project.create("Q", self.dir / "images", ".svs", self.dir / "save")

    def test_invalid_projects_are_refused(self):
        with self.assertRaises(ViewerError):
            Project.create("", self.dir / "images", ".svs", self.dir / "s2")
        with self.assertRaises(ViewerError):
            Project.create("X", self.dir / "nope", ".svs", self.dir / "s2")
        with self.assertRaises(ViewerError):
            Project.create("X", self.dir / "images", ".svs", self.dir / "images")
        with self.assertRaises(ViewerError):
            Project.create("X", self.dir / "images", "", self.dir / "s2")

    def test_class_rules(self):
        with self.assertRaises(ViewerError):
            self.project.add_class("tumor", "#00ff00")  # same name
        with self.assertRaises(ViewerError):
            self.project.add_class("Stroma", "green")
        self.project.update_class(self.cls["id"], {"name": "Tumour", "color": "#ff00ff"})
        self.assertEqual(self.project.cls(self.cls["id"])["name"], "Tumour")

    def test_old_export_colors_become_export_settings(self):
        data = json.loads((self.dir / "save/project.json").read_text())
        data["classes"][0]["export_color"] = "#010101"
        (self.dir / "save/project.json").write_text(json.dumps(data))
        again = Project.open(self.dir / "save")
        self.assertNotIn("export_color", again.data["classes"][0])
        self.assertEqual(again.export_settings()["classes"][0]["color"], "#010101")

    def test_paths_cannot_leave_the_root(self):
        for bad in ("../x.svs", "/etc/passwd", "a/../../x", ""):
            with self.assertRaises(ViewerError):
                self.project.image_path(bad)

    def test_registry(self):
        reg = Registry(self.dir / "reg.json")
        reg.remember(self.project)
        reg.remember(self.project)
        self.assertEqual(len(reg.load()), 1)
        reg.forget(self.project.id)
        self.assertEqual(reg.load(), [])


class AnnotationTest(Base):
    def store(self):
        return Annotations(self.project, "a.svs")

    def test_polygon_is_saved_as_geojson_in_pixels(self):
        store = self.store()
        store.add_polygon(SQUARE, self.cls["id"])
        file = self.dir / "save/annotations/a.svs.geojson"
        data = json.loads(file.read_text())
        self.assertEqual(data["type"], "FeatureCollection")
        feature = data["features"][0]
        self.assertEqual(feature["geometry"]["type"], "Polygon")
        self.assertEqual(feature["properties"]["classification"], {"name": "Tumor", "color": [255, 0, 0]})
        self.assertEqual(count_shapes(file), 1)
        self.assertEqual(len(self.store().shapes), 1)  # read back

    def test_self_intersecting_polygon_is_repaired(self):
        store = self.store()
        store.add_polygon([[0, 0], [100, 100], [100, 0], [0, 100]], self.cls["id"])  # a bow tie
        self.assertTrue(store.shapes[0].geometry.is_valid)
        self.assertAlmostEqual(store.shapes[0].geometry.area, 5000, delta=1)

    def test_overlaps_are_allowed(self):
        store = self.store()
        store.add_polygon(SQUARE, self.cls["id"])
        store.add_polygon([[50, 50], [150, 50], [150, 150], [50, 150]], self.cls["id"])
        self.assertEqual(len(store.shapes), 2)
        self.assertEqual(store.shapes[0].geometry.area, 10000)

    def test_eraser_cuts_splits_and_removes(self):
        store = self.store()
        store.add_polygon(SQUARE, self.cls["id"])
        store.erase([[50, -10], [50, 110]], 5)  # a cut through the middle: two parts
        self.assertEqual(store.shapes[0].geometry.geom_type, "MultiPolygon")
        self.assertAlmostEqual(store.shapes[0].geometry.area, 9000, delta=20)
        store.erase([[0, 0], [100, 100], [0, 100], [100, 0]], 200)  # everything
        self.assertEqual(store.shapes, [])
        self.assertEqual(store.erase([[500, 500]], 5), 0)  # nothing there

    def test_eraser_makes_holes(self):
        store = self.store()
        store.add_polygon(SQUARE, self.cls["id"])
        store.erase([[50, 50]], 10)
        self.assertEqual(len(store.shapes[0].geometry.interiors), 1)

    def test_undo_and_redo(self):
        store = self.store()
        store.add_polygon(SQUARE, self.cls["id"])
        store.erase([[50, 50]], 10)
        self.assertTrue(store.undo())
        self.assertEqual(len(store.shapes[0].geometry.interiors), 0)
        self.assertTrue(store.undo())
        self.assertEqual(store.shapes, [])
        self.assertFalse(store.undo())
        self.assertTrue(store.redo())
        self.assertEqual(len(store.shapes), 1)
        self.assertEqual(len(self.store().shapes), 1)  # the file follows

    def test_polygon_needs_a_known_class_and_three_points(self):
        store = self.store()
        with self.assertRaises(ViewerError):
            store.add_polygon(SQUARE, 99)
        with self.assertRaises(ViewerError):
            store.add_polygon([[0, 0], [1, 1]], self.cls["id"])

    def test_files_of_other_tools_match_classes_by_name(self):
        file = self.dir / "save/annotations/a.svs.geojson"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [SQUARE + [SQUARE[0]]]},
             "properties": {"classification": {"name": "Tumor"}}}]}))
        self.assertEqual(self.store().shapes[0].class_id, self.cls["id"])


if __name__ == "__main__":
    unittest.main()
