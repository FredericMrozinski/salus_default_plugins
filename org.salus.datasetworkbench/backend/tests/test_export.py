import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench import export  # noqa: E402
from workbench.service import Workbench  # noqa: E402
from workbench.util import WorkbenchError  # noqa: E402


def write(path: Path, data: bytes | str = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


class Base(unittest.TestCase):
    """Two entries: #1 is valid (slide a/s1.svs, patient P1), #2 is incomplete (the required case is empty)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.events = []
        self.wb = Workbench(self.base / "root", self.events.append, background=False)
        self.group = self.wb.catalog.create_group("", "cohorts")
        self.slides = self.wb.catalog.create_source("cohorts", "slides", "file")
        write(self.slides.path / "a" / "s1.svs", b"slide one")
        write(self.slides.path / "s2.svs", b"slide two!")
        write(self.slides.path / "unused.svs", b"not exported")
        self.slides.scan()
        self.patients = self.wb.catalog.create_source("", "patients", "table")
        write(self.patients.path / "patients.csv", "id,age\nP1,30\nP2,41\n")
        self.patients.configure(self.patients.inspect()["columns"])
        self.ds = self.wb.datasets.create("D")
        self.ds.set_schema([
            {"name": "slide", "kind": "file", "sources": [self.slides.id]},
            {"name": "case", "kind": "value", "value_type": "text", "required": True},
            {"name": "patient", "kind": "row", "sources": [self.patients.id]},
            {"name": "score", "kind": "value", "value_type": "integer"}])
        self.slot = {s["name"]: s["id"] for s in self.ds.slots()}
        items = {i["path"]: i["id"] for i in self.slides.list_dir("", "", 100, 0)["files"]}
        items.update({i["path"]: i["id"] for i in self.slides.list_dir("a", "", 100, 0)["files"]})
        one = self.ds.create_entry()["id"]
        self.cell(one, "slide", {"source": self.slides.id, "item": items["a/s1.svs"]})
        self.cell(one, "case", {"value": "C1"})
        self.cell(one, "patient", {"source": self.patients.id, "item": "P1"})
        self.cell(one, "score", {"value": 3})
        two = self.ds.create_entry()["id"]
        self.cell(two, "slide", {"source": self.slides.id, "item": items["s2.svs"]})
        self.out = self.base / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def cell(self, entry, name, payload):
        self.ds.set_cell(entry, self.slot[name], payload)

    def options(self, **changes):
        return {"format": "json", "destination": str(self.out / "d.json"), **changes}

    def export(self, overwrite=False, **changes):
        return export.run(self.ds, self.options(**changes), overwrite)


class JsonTests(Base):
    def test_valid_entries_with_absolute_paths_and_row_values(self):
        result = self.export()
        doc = json.loads((self.out / "d.json").read_text())
        self.assertEqual(doc["format"], export.EXPORT_FORMAT)
        self.assertEqual(result["entries"], 1)
        self.assertEqual(doc["entries"], [{"slide": str(self.slides.path.resolve() / "a" / "s1.svs"), "case": "C1",
                                           "patient": {"key": "P1", "values": {"id": "P1", "age": 30}}, "score": 3}])
        self.assertEqual([f["key"] for f in doc["fields"]], ["slide", "case", "patient", "score"])
        self.assertEqual({s["name"]: s["location"] for s in doc["sources"]},
                         {"slides": str(self.slides.path.resolve()), "patients": str(self.patients.path.resolve())})

    def test_incomplete_entries_and_renamed_keys(self):
        self.export(include={"incomplete": True}, keys={str(self.slot["slide"]): "image", str(self.slot["case"]): " id "})
        doc = json.loads((self.out / "d.json").read_text())
        self.assertEqual(len(doc["entries"]), 2)
        self.assertEqual(set(doc["entries"][1]), {"image", "id", "patient", "score"})
        self.assertIsNone(doc["entries"][1]["id"])
        self.assertEqual(doc["states"], ["valid", "incomplete"])

    def test_keys_must_be_unique_and_not_empty(self):
        with self.assertRaisesRegex(WorkbenchError, "keys must differ"):
            self.export(keys={str(self.slot["case"]): "slide"})
        with self.assertRaisesRegex(WorkbenchError, "needs a key"):
            self.export(keys={str(self.slot["case"]): "  "})

    def test_relative_paths_with_and_without_prefix(self):
        self.export(paths="relative", include={"incomplete": True}, prefixes={self.slides.id: "/data/slides/"})
        doc = json.loads((self.out / "d.json").read_text())
        self.assertEqual([e["slide"] for e in doc["entries"]], ["/data/slides/a/s1.svs", "/data/slides/s2.svs"])
        self.export(overwrite=True, paths="relative", prefixes={self.slides.id: "D:\\slides"})
        self.assertEqual(json.loads((self.out / "d.json").read_text())["entries"][0]["slide"], "D:\\slides/a/s1.svs")
        self.export(overwrite=True, paths="relative")
        doc = json.loads((self.out / "d.json").read_text())
        self.assertEqual(doc["entries"][0]["slide"], "a/s1.svs")
        self.assertIsNone(next(s for s in doc["sources"] if s["name"] == "slides")["location"])

    def test_a_deleted_file_is_exported_as_null(self):
        (self.slides.path / "a" / "s1.svs").unlink()
        self.slides.scan()
        self.ds.recompute()
        result = self.export(include={"corrupted": True})
        doc = json.loads((self.out / "d.json").read_text())
        self.assertIsNone(doc["entries"][0]["slide"])
        self.assertEqual(result["missing"], 1)

    def test_copies_need_a_folder_or_zip(self):
        with self.assertRaisesRegex(WorkbenchError, "folder or a zip"):
            self.export(copy_sources=True)


class CopyTests(Base):
    def test_folder_with_copies(self):
        result = self.export(format="folder", destination=str(self.out / "exp"), copy_sources=True, include={"incomplete": True})
        folder = self.out / "exp"
        doc = json.loads((folder / "dataset.json").read_text())
        self.assertEqual([e["slide"] for e in doc["entries"]], ["sources/cohorts/slides/a/s1.svs", "sources/cohorts/slides/s2.svs"])
        for e in doc["entries"]:
            self.assertTrue((folder / e["slide"]).is_file())
        self.assertEqual((folder / "sources/cohorts/slides/a/s1.svs").read_bytes(), b"slide one")
        self.assertFalse((folder / "sources/cohorts/slides/unused.svs").exists())  # only what the entries use
        self.assertEqual((folder / "sources/patients/patients.csv").read_text(), "id,age\nP1,30\nP2,41\n")
        self.assertEqual(result["files"], 3)
        self.assertEqual([p.name for p in self.out.iterdir()], ["exp"])  # no temporary leftovers

    def test_zip_with_copies(self):
        self.export(format="zip", destination=str(self.out / "exp.zip"), copy_sources=True)
        with zipfile.ZipFile(self.out / "exp.zip") as z:
            self.assertEqual(sorted(z.namelist()), ["dataset.json", "sources/cohorts/slides/a/s1.svs", "sources/patients/patients.csv"])
            self.assertEqual(z.read("sources/cohorts/slides/a/s1.svs"), b"slide one")
            self.assertEqual(json.loads(z.read("dataset.json"))["entries"][0]["slide"], "sources/cohorts/slides/a/s1.svs")

    def test_zip_without_copies_holds_only_the_json(self):
        self.export(format="zip", destination=str(self.out / "exp.zip"))
        with zipfile.ZipFile(self.out / "exp.zip") as z:
            self.assertEqual(z.namelist(), ["dataset.json"])

    def test_modes_follow_the_umask(self):
        old = os.umask(0o027)
        try:
            self.export(format="folder", destination=str(self.out / "exp"), copy_sources=True)
            self.export(format="zip", destination=str(self.out / "exp.zip"))
            self.export()
        finally:
            os.umask(old)
        mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
        self.assertEqual(mode(self.out / "exp"), 0o750)
        self.assertEqual(mode(self.out / "exp" / "sources" / "cohorts"), 0o750)
        self.assertEqual(mode(self.out / "exp" / "dataset.json"), 0o640)
        self.assertEqual(mode(self.out / "exp" / "sources/cohorts/slides/a/s1.svs"), 0o640)
        self.assertEqual(mode(self.out / "exp.zip"), 0o640)
        self.assertEqual(mode(self.out / "d.json"), 0o640)


class DestinationTests(Base):
    def test_an_existing_file_is_only_replaced_when_asked(self):
        write(self.out / "d.json", "old")
        state = export.check_destination(self.ds, export.normalize_options(self.options(), self.ds, self.ds.slots()))
        self.assertEqual(state["replace"], "file")
        with self.assertRaises(WorkbenchError) as caught:
            self.export()
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual((self.out / "d.json").read_text(), "old")
        self.export(overwrite=True)
        self.assertIn("entries", json.loads((self.out / "d.json").read_text()))

    def test_only_an_earlier_export_folder_is_replaced(self):
        write(self.out / "exp" / "notes.txt", "mine")
        with self.assertRaisesRegex(WorkbenchError, "holds no earlier export"):
            self.export(overwrite=True, format="folder", destination=str(self.out / "exp"))
        self.assertTrue((self.out / "exp" / "notes.txt").exists())
        (self.out / "exp" / "notes.txt").unlink()
        self.export(format="folder", destination=str(self.out / "exp"))  # empty: nothing to ask
        write(self.out / "exp" / "extra.txt", "added later")
        with self.assertRaises(WorkbenchError):
            self.export(format="folder", destination=str(self.out / "exp"))
        self.export(overwrite=True, format="folder", destination=str(self.out / "exp"))
        self.assertEqual(sorted(p.name for p in (self.out / "exp").iterdir()), ["dataset.json"])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["exp"])

    def test_refused_destinations(self):
        for destination, problem in [("relative/d.json", "absolute"), (str(self.slides.path / "d.json"), "source cohorts"),
                                     (str(self.wb.catalog.datasets_dir), "datasets")]:
            with self.assertRaisesRegex(WorkbenchError, problem):
                self.export(destination=destination)
        write(self.out / "dir" / "x", "")
        with self.assertRaisesRegex(WorkbenchError, "folder with this name"):
            self.export(overwrite=True, destination=str(self.out / "dir"))

    def test_cancel_leaves_the_destination_as_it_was(self):
        write(self.out / "exp" / "dataset.json", "earlier")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(export.Cancelled):
            export.run(self.ds, self.options(format="folder", destination=str(self.out / "exp"), copy_sources=True), True, cancel=cancel)
        self.assertEqual((self.out / "exp" / "dataset.json").read_text(), "earlier")
        self.assertEqual([p.name for p in self.out.iterdir()], ["exp"])


class PreviewAndJobTests(Base):
    def test_preview(self):
        p = export.preview(self.ds, {"format": "folder", "copy_sources": True, "include": {"incomplete": True}})
        self.assertIsNone(p["error"])
        self.assertEqual(p["counts"], {"valid": 1, "incomplete": 1, "corrupted": 0})
        self.assertEqual(p["entries"], 2)
        self.assertEqual(p["files"], 3)
        self.assertEqual(p["bytes"], len(b"slide one") + len(b"slide two!") + len("id,age\nP1,30\nP2,41\n"))
        self.assertEqual(p["destination"]["path"], str(self.wb.catalog.root.resolve() / "exports" / self.ds.id))
        self.assertEqual(p["sample"]["case"], "C1")
        self.assertEqual({s["folder"] for s in p["sources"]}, {"cohorts/slides", "patients"})

    def test_preview_reports_bad_keys_and_still_counts(self):
        p = export.preview(self.ds, {"keys": {str(self.slot["case"]): "slide"}})
        self.assertIn("keys must differ", p["error"])
        self.assertEqual(p["entries"], 1)

    def test_job_and_status(self):
        self.assertEqual(self.wb.export_status(self.ds.id), {"state": "idle"})
        self.wb.export_dataset(self.ds.id, self.options())
        status = self.wb.export_status(self.ds.id)
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["result"]["entries"], 1)
        done = [e for e in self.events if e.get("type") == "job_done" and e["kind"] == "export"]
        self.assertTrue(done[-1]["ok"])
        with self.assertRaises(WorkbenchError) as caught:
            self.wb.export_dataset(self.ds.id, self.options())
        self.assertEqual(caught.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
