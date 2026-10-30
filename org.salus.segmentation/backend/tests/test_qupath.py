import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import qupath  # noqa: E402
from annotations import Annotations  # noqa: E402
from project import Project  # noqa: E402

SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]


def feature(cls=None, geometry=None, kind="annotation"):
    props = {"objectType": kind}
    if cls:
        props["classification"] = {"name": cls, "color": [255, 0, 0]}
    return {"type": "Feature", "id": "6f1c3a2e-0000-4000-8000-000000000000",
            "geometry": geometry or {"type": "Polygon", "coordinates": [SQUARE]}, "properties": props}


# What QuPath exports (by default a plain list of features, not a FeatureCollection)
QUPATH = [
    feature("Tumor"), feature("Tumor"), feature("Stroma"), feature(None),
    feature("Tumor", kind="detection"),
    feature("Tumor", {"type": "Point", "coordinates": [5, 5]}),
    feature("Tumor", {"type": "LineString", "coordinates": [[0, 0], [5, 5]]}),
]


class QuPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        for rel in ("a.svs", "case1/b.svs", "case2/b.svs", "case1/c.svs"):
            (self.dir / "images" / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / "images" / rel).write_bytes(b"x")
        self.project = Project.create("P", self.dir / "images", ".svs", self.dir / "save")
        self.tumor = self.project.add_class("Tumor", "#ff0000")["id"]
        self.other = self.project.add_class("Other", "#00ff00")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, data=QUPATH):
        path = self.dir / "qupath" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_reads_every_geojson_shape(self):
        self.assertEqual(len(qupath.read(self.write("list.geojson"))), 7)
        self.assertEqual(len(qupath.read(self.write("fc.geojson", {"type": "FeatureCollection", "features": QUPATH}))), 7)
        self.assertEqual(len(qupath.read(self.write("one.geojson", feature("Tumor")))), 1)

    def test_summary_counts_area_annotations_per_class(self):
        classes, skipped = qupath.summarize(QUPATH)
        self.assertEqual(classes, {"Tumor": 2, "Stroma": 1, "": 1})
        self.assertEqual(skipped, 3)  # the detection, the point and the line

    def test_derived_class_names(self):
        f = feature()
        f["properties"]["classification"] = {"names": ["Tumor", "Positive"]}
        self.assertEqual(qupath.class_name(f), "Tumor: Positive")

    def test_only_matched_classes_are_imported(self):
        shapes = qupath.shapes_for(QUPATH, {"Tumor": self.tumor, "": self.other, "Stroma": None})
        self.assertEqual(sorted(c for c, _ in shapes), [self.tumor, self.tumor, self.other])

    def test_merge_and_replace_are_one_undo_step(self):
        store = Annotations(self.project, "a.svs")
        store.add_polygon(SQUARE[:4], self.other)
        shapes = qupath.shapes_for(QUPATH, {"Tumor": self.tumor})
        store.import_shapes(shapes, replace=False)
        self.assertEqual(len(store.shapes), 3)
        store.import_shapes(shapes, replace=True)
        self.assertEqual(len(store.shapes), 2)
        store.undo()
        self.assertEqual(len(store.shapes), 3)
        self.assertEqual(len(Annotations(self.project, "a.svs").shapes), 3)  # written

    def test_folder_matching_by_path_and_name(self):
        self.write("a.geojson")                 # name: a.svs
        self.write("case1/b.svs.geojson")       # path with extension
        self.write("case2/b.json")              # path without extension
        self.write("c.geojson")                 # name: only case1/c.svs
        self.write("b.geojson")                 # name: ambiguous (case1/b.svs and case2/b.svs)
        self.write("zzz.geojson")               # nothing
        matches, left_out = qupath.match_folder(self.dir / "qupath", self.project.images())
        self.assertEqual(sorted(image for _, image in matches), ["a.svs", "case1/b.svs", "case1/c.svs", "case2/b.svs"])
        reasons = {Path(e["file"]).name: e["reason"] for e in left_out}
        self.assertIn("several images", reasons["b.geojson"])
        self.assertIn("no image", reasons["zzz.geojson"])

    def test_two_files_for_one_image(self):
        self.write("a.geojson")
        self.write("x/a.svs.geojson")
        matches, left_out = qupath.match_folder(self.dir / "qupath", self.project.images())
        self.assertEqual(len(matches), 1)
        self.assertIn("already", left_out[0]["reason"])

    def test_qupath_file_is_not_changed(self):
        path = self.write("a.geojson")
        before = path.read_bytes()
        Annotations(self.project, "a.svs").import_shapes(qupath.shapes_for(qupath.read(path), {"Tumor": self.tumor}), replace=False)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
