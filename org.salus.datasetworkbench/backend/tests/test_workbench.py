import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.dataset import coerce_value  # noqa: E402
from workbench.service import Workbench  # noqa: E402
from workbench.util import WorkbenchError  # noqa: E402


def write(path: Path, data: bytes | str = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.wb = Workbench(Path(self.tmp.name) / "root", self.events.append, background=False)

    def tearDown(self):
        self.tmp.cleanup()

    def file_source(self, name="wsi", group=""):
        return self.wb.catalog.create_source(group, name, "file")

    def table_source(self, name="patients", csv="id,name,age\np1,Ann,30\np2,Bob,41\np3,Cy,\n"):
        source = self.wb.catalog.create_source("", name, "table")
        write(source.path / "patients.csv", csv)
        return source

    def upload(self, source, rel, data, overwrite=False):
        info = self.wb.uploads.start(source.id, rel, len(data), overwrite)
        for offset in range(0, len(data), 5):
            self.wb.uploads.append(info["upload_id"], offset, data[offset:offset + 5])
        return self.wb.finish_upload(info["upload_id"])


class CatalogTests(Base):
    def test_groups_and_sources_form_a_tree(self):
        self.wb.catalog.create_group("", "clinic")
        self.wb.catalog.create_source("clinic", "wsi", "file")
        tree = self.wb.catalog.tree()
        self.assertEqual(tree[0]["type"], "group")
        self.assertEqual(tree[0]["children"][0]["name"], "wsi")

    def test_rejects_unsafe_names(self):
        for bad in ("../x", "a/b", ".hidden", "", "x" * 200):
            with self.assertRaises(WorkbenchError):
                self.wb.catalog.create_group("", bad)
        with self.assertRaises(WorkbenchError):
            self.wb.catalog.create_group("../..", "ok")

    def test_source_id_survives_a_move(self):
        source = self.file_source()
        sid = source.id
        new_dir = self.wb.catalog.sources_dir / "renamed"
        source.path.rename(new_dir)
        self.assertEqual(self.wb.catalog.find_source(sid).path, new_dir)

    def test_remove_source_keeps_files(self):
        source = self.file_source()
        write(source.path / "a.txt")
        self.wb.catalog.remove_source(source.id)
        self.assertTrue((source.path / "a.txt").exists())
        with self.assertRaises(WorkbenchError):
            self.wb.catalog.find_source(source.id)


class MoveTests(Base):
    def test_move_source_between_groups_keeps_its_id_and_references(self):
        self.wb.catalog.create_group("", "a")
        self.wb.catalog.create_group("", "b")
        source = self.wb.catalog.create_source("a", "s", "file")
        sid = source.id
        write(source.path / "x.txt")
        source.scan()
        self.assertEqual(self.wb.catalog.move("a/s", "b"), "b/s")
        self.assertEqual(self.wb.catalog.find_source(sid).path, self.wb.catalog.sources_dir / "b" / "s")
        self.assertEqual(self.wb.catalog.move("b/s", ""), "s")  # out of the group, to the top level
        self.assertTrue((self.wb.catalog.sources_dir / "s" / "x.txt").exists())
        self.assertEqual(self.wb.catalog.move("s", ""), "s")  # already there: nothing happens

    def test_move_group_with_content(self):
        self.wb.catalog.create_group("", "outer")
        self.wb.catalog.create_group("", "target")
        source = self.wb.catalog.create_source("outer", "s", "file")
        sid = source.id
        self.assertEqual(self.wb.catalog.move("outer", "target"), "target/outer")
        self.assertTrue(self.wb.catalog.find_source(sid).path.match("*/target/outer/s"))

    def test_move_refuses_nonsense(self):
        self.wb.catalog.create_group("", "g")
        self.wb.catalog.create_group("g", "inner")
        a = self.wb.catalog.create_source("", "a", "file")
        write(a.path / "sub" / "f.txt")
        self.wb.catalog.create_source("g", "a", "file")  # same name inside g
        cases = [("g", "g"), ("g", "g/inner"),  # into itself or below
                 ("a", "g"),  # name already taken there
                 ("g/inner", "a"), ("g/inner", "a/sub"),  # into a source or its subfolder
                 ("nope", ""), ("../x", ""), ("g", "../..")]
        for rel, to in cases:
            with self.assertRaises(WorkbenchError, msg=f"{rel} -> {to}"):
                self.wb.catalog.move(rel, to)
        self.assertTrue((self.wb.catalog.sources_dir / "g" / "inner").exists())


class DeleteTests(Base):
    def test_remove_source_can_delete_files(self):
        source = self.file_source()
        write(source.path / "a.txt")
        self.wb.catalog.remove_source(source.id, delete_files=True)
        self.assertFalse(source.path.exists())

    def test_delete_group_needs_force_when_not_empty_and_reports_sources(self):
        self.wb.catalog.create_group("", "cohort")
        source = self.wb.catalog.create_source("cohort", "wsi", "file")
        sid = source.id
        with self.assertRaises(WorkbenchError):
            self.wb.catalog.delete_group("cohort")
        self.assertEqual(self.wb.catalog.delete_group("cohort", force=True), [sid])
        self.assertFalse((self.wb.catalog.sources_dir / "cohort").exists())
        with self.assertRaises(WorkbenchError):
            self.wb.catalog.find_source(sid)

    def test_empty_group_and_unsafe_paths(self):
        self.wb.catalog.create_group("", "empty")
        self.wb.catalog.delete_group("empty")
        for bad in ("", "../x"):
            with self.assertRaises(WorkbenchError):
                self.wb.catalog.delete_group(bad, force=True)


class SearchTests(Base):
    def test_global_search_spans_sources_words_and_csv(self):
        self.wb.catalog.create_group("", "cohort")
        a = self.wb.catalog.create_source("cohort", "wsi", "file")
        b = self.wb.catalog.create_source("", "reports", "file")
        for source, rel in ((a, "2024/case_17.svs"), (a, "other/Case_18.svs"), (b, "case_17_report.pdf"), (b, "notes.txt")):
            write(source.path / rel)
        a.scan()
        b.scan()
        table = self.table_source()
        found = self.wb.catalog.search("case")["results"]
        self.assertEqual(sorted(h["path"] for h in found), ["2024/case_17.svs", "case_17_report.pdf", "other/Case_18.svs"])  # case-insensitive
        hit = next(h for h in found if h["path"] == "case_17_report.pdf")
        self.assertEqual((hit["source_name"], hit["source_path"], hit["kind"]), ("reports", "reports", "file"))
        self.assertTrue(hit["abs_path"].endswith("case_17_report.pdf"))
        self.assertEqual([h["path"] for h in self.wb.catalog.search("case 17 svs")["results"]], ["2024/case_17.svs"])  # every word must match
        self.assertEqual([h["path"] for h in self.wb.catalog.search("patients")["results"]], ["patients.csv"])
        self.assertEqual(self.wb.catalog.search("   ")["results"], [])
        self.assertEqual(self.wb.catalog.search("100%")["results"], [])  # LIKE wildcards are plain text

    def test_search_limit_and_missing_files(self):
        source = self.file_source()
        for i in range(5):
            write(source.path / f"f{i}.txt")
        source.scan()
        (source.path / "f0.txt").unlink()
        source.scan()
        result = self.wb.catalog.search("f", limit=3)
        self.assertEqual((len(result["results"]), result["more"]), (3, True))
        self.assertNotIn("f0.txt", [h["path"] for h in self.wb.catalog.search("f")["results"]])


class RenameDeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.files = self.file_source("wsi", "")
        write(self.files.path / "a.svs", b"aaaa")
        write(self.files.path / "sub/b.svs", b"bb")
        write(self.files.path / "sub/deep/c.pdf", b"c")
        self.files.scan()
        self.table = self.table_source()
        self.table.configure(self.table.inspect()["columns"])
        self.ds = self.wb.datasets.create("D")
        self.ds.set_schema([
            {"name": "slide", "kind": "file", "sources": [self.files.id], "extensions": ["svs"], "required": True},
            {"name": "patient", "kind": "row", "sources": [self.table.id], "required": True}])
        self.slot = {s["name"]: s["id"] for s in self.ds.slots()}
        self.entry = self.ds.create_entry()["id"]

    def ids(self):
        with self.files.connect() as conn:
            return {r["rel_path"]: r["id"] for r in conn.execute("SELECT id, rel_path FROM items")}

    def point(self, path):
        self.ds.set_cell(self.entry, self.slot["slide"], {"source": self.files.id, "item": self.ids()[path]})
        self.ds.set_cell(self.entry, self.slot["patient"], {"source": self.table.id, "item": "p1"})

    def state(self, name):
        return next(c["state"] for c in self.ds.get_entry(self.entry)["cells"] if c["name"] == name)

    def test_renaming_a_file_keeps_entries_valid_and_updates_the_label(self):
        self.point("a.svs")
        new = self.wb.rename_item(self.files.id, "a.svs", "renamed.svs")
        self.assertEqual(new, "renamed.svs")
        entry = self.ds.get_entry(self.entry)
        self.assertEqual(entry["state"], "valid")
        self.assertEqual(entry["cells"][0]["ref"]["label"], "renamed.svs")
        self.assertTrue((self.files.path / "renamed.svs").exists())
        self.assertFalse((self.files.path / "a.svs").exists())

    def test_renaming_the_extension_flags_the_entry_invalid(self):
        self.point("a.svs")
        self.wb.rename_item(self.files.id, "a.svs", "a.txt")
        self.assertEqual(self.state("slide"), "invalid")

    def test_renaming_a_folder_moves_all_items_below(self):
        self.point("sub/b.svs")
        self.wb.rename_item(self.files.id, "sub", "renamed")
        self.assertEqual(sorted(self.ids()), ["a.svs", "renamed/b.svs", "renamed/deep/c.pdf"])
        self.assertEqual(self.ds.get_entry(self.entry)["cells"][0]["ref"]["label"], "renamed/b.svs")
        self.assertEqual(self.state("slide"), "ok")

    def test_rename_refuses_collisions_and_bad_names(self):
        for new in ("sub", "../x", "x/y", ".hidden", "", "source.sqlite", "x.part"):
            with self.assertRaises(WorkbenchError, msg=new):
                self.files.rename_item("a.svs", new)
        self.assertTrue((self.files.path / "a.svs").exists())

    def test_case_only_rename(self):
        self.files.rename_item("a.svs", "A.svs")
        self.assertIn("A.svs", self.ids())

    def test_deleting_forgets_unused_items_and_keeps_used_ones_as_missing(self):
        self.point("sub/b.svs")
        usage = self.wb.source_usage(self.files.id, "sub")
        self.assertEqual(usage["entries"], 1)
        self.assertEqual(usage["datasets"][0]["name"], "D")
        self.assertEqual(self.wb.source_usage(self.files.id, "a.svs")["entries"], 0)
        self.wb.delete_item(self.files.id, "sub")
        self.assertFalse((self.files.path / "sub").exists())
        self.assertEqual(self.state("slide"), "source_missing")
        self.assertEqual(sorted(self.ids()), ["a.svs", "sub/b.svs"])  # c.pdf was unused and is forgotten; b.svs stays missing
        self.assertEqual(self.files.info()["missing"], 1)

    def test_rename_source_and_group_keep_references(self):
        self.point("a.svs")
        self.wb.catalog.create_group("", "cohort")
        files_id = self.files.id
        moved = self.wb.catalog.rename_source(files_id, "slides")
        self.assertEqual((moved.path.name, moved.name, moved.id), ("slides", "slides", files_id))
        self.assertEqual(self.ds.get_entry(self.entry)["state"], "valid")
        self.assertEqual(self.ds.get_entry(self.entry)["cells"][0]["ref"]["source_name"], "slides")
        self.assertEqual(self.wb.catalog.rename_group("cohort", "renamed"), "renamed")
        with self.assertRaises(WorkbenchError):
            self.wb.catalog.rename_source(files_id, "patients")
        with self.assertRaises(WorkbenchError):
            self.wb.catalog.rename_group("", "x")

    def test_group_usage_and_rename_of_nested_group(self):
        self.wb.catalog.create_group("", "outer")
        self.wb.catalog.create_group("outer", "inner")
        nested = self.wb.catalog.create_source("outer/inner", "n", "file")
        write(nested.path / "z.svs")
        nested.scan()
        nested_id = nested.id
        self.assertEqual(self.wb.catalog.rename_group("outer/inner", "renamed"), "outer/renamed")
        self.assertTrue(self.wb.catalog.find_source(nested_id).path.match("*/outer/renamed/n"))
        self.assertEqual(self.wb.group_usage("outer")["entries"], 0)

    def test_table_csv_rename_and_delete(self):
        self.point("a.svs")
        self.table.rename_item("patients.csv", "people.csv")
        self.assertEqual(self.table.csv_file().name, "people.csv")
        self.wb.source_changed(self.table.id)
        self.assertEqual(self.state("patient"), "ok")
        self.wb.delete_item(self.table.id, "people.csv")
        self.assertEqual(self.table.state(), "no_csv")
        self.assertEqual(self.state("patient"), "source_missing")


class FileSourceTests(Base):
    def test_scan_finds_files_and_ignores_internal_ones(self):
        source = self.file_source()
        write(source.path / "a.svs")
        write(source.path / "sub/b.pdf", b"yy")
        write(source.path / "c.txt.upload-1.part")
        write(source.path / ".hidden")
        result = source.scan()
        self.assertEqual(result["new"], 2)
        listing = source.list_dir("", "", 100, 0)
        self.assertEqual([f["name"] for f in listing["files"]], ["a.svs"])
        self.assertEqual(listing["folders"][0]["name"], "sub")
        self.assertEqual([f["name"] for f in source.list_dir("sub", "", 100, 0)["files"]], ["b.pdf"])

    def test_moved_file_keeps_its_id_and_deleted_file_is_missing(self):
        source = self.file_source()
        a = write(source.path / "a.svs", b"aaaa")
        write(source.path / "b.svs", b"bb")
        source.scan()
        ids = {f["name"]: f["id"] for f in source.list_dir("", "", 10, 0)["files"]}
        (source.path / "moved").mkdir()
        a.rename(source.path / "moved" / "a2.svs")
        (source.path / "b.svs").unlink()
        result = source.scan()
        self.assertEqual((result["moved"], result["missing"], result["new"]), (1, 1, 0))
        self.assertEqual(source.item(ids["a.svs"])["path"], "moved/a2.svs")
        self.assertEqual(source.item(ids["b.svs"])["status"], "missing")
        self.assertEqual(source.info()["missing"], 1)

    def test_search_filters_by_extension(self):
        source = self.file_source()
        write(source.path / "a.svs")
        write(source.path / "b.pdf")
        source.scan()
        hits = source.search("", 10, 0, extensions=["svs"])
        self.assertEqual([h["label"] for h in hits], ["a.svs"])


class TableSourceTests(Base):
    def test_inspect_infers_types_and_key(self):
        source = self.table_source()
        columns = {c["name"]: c for c in source.inspect()["columns"]}
        self.assertEqual(columns["id"]["is_key"], True)
        self.assertEqual(columns["age"]["type"], "integer")
        self.assertEqual(columns["name"]["type"], "text")

    def test_ingest_reports_bad_and_duplicate_rows(self):
        source = self.table_source(csv="id,age\na,1\nb,x\na,3\n,4\nc,5\nd\n")
        result = source.configure([{"name": "id", "type": "text", "is_key": True}, {"name": "age", "type": "integer"}])
        self.assertEqual(result["rows"], 1)  # only c is usable: a is duplicated, b/age is bad
        codes = sorted(i["code"] for i in source.issues(100, 0))
        self.assertEqual(codes, ["column_count", "duplicate_key", "duplicate_key", "empty_key", "type"])
        self.assertEqual(source.info()["rows"], 1)

    def test_changed_csv_is_reingested(self):
        source = self.table_source()
        source.configure(source.inspect()["columns"])
        self.assertFalse(source.refresh())
        time.sleep(0.01)
        write(source.path / "patients.csv", "id,name,age\np1,Ann,31\n")
        self.assertTrue(source.refresh())
        self.assertEqual(source.info()["rows"], 1)

    def test_columns_changing_in_csv_requires_setup(self):
        source = self.table_source()
        source.configure(source.inspect()["columns"])
        write(source.path / "patients.csv", "id,other\np1,1\n")
        source.refresh()
        self.assertEqual(source.state(), "needs_setup")

    def test_unnamed_columns_from_excel_are_ignored(self):
        source = self.table_source(csv="First Name;Last Name;Born;;;;\r\nMax;Mustermann;1.3.1989;;;;\r\nHeike;Musterfrau;23.9.1998;;;;\r\n")
        suggestion = source.inspect()
        self.assertEqual([c["name"] for c in suggestion["columns"]], ["First Name", "Last Name", "Born"])
        self.assertEqual(suggestion["ignored"], 4)
        result = source.configure(suggestion["columns"])
        self.assertEqual((result["rows"], result["issues"]), (2, 0))
        self.assertEqual(source.fingerprints(["Max"])["Max"][1], "Max")

    def test_generated_keys_are_row_numbers(self):
        source = self.table_source(csv="a,b\nx,1\nx,1\n\ny,2\n")
        suggestion = source.inspect()
        self.assertTrue(suggestion["auto_key"])  # no column is unique
        result = source.configure(suggestion["columns"], auto_key=True)
        self.assertEqual((result["rows"], result["issues"]), (3, 0))
        self.assertTrue(source.info()["auto_key"])
        self.assertEqual(sorted(source.fingerprints(["1", "2", "4"])), ["1", "2", "4"])  # the blank line keeps its number
        self.assertEqual([h["item"] for h in source.search("y", 10, 0)], ["4"])
        source.configure(source.inspect()["columns"], auto_key=True)  # reconfiguring is fine
        with self.assertRaises(WorkbenchError):
            source.configure([{"name": "a", "type": "text"}, {"name": "b", "type": "text"}])

    def test_several_csv_files_are_reported_not_guessed(self):
        source = self.table_source()
        source.configure(source.inspect()["columns"])
        write(source.path / "patients (edited).csv", "id,name,age\np9,Zed,1\n")  # sorts before patients.csv: must not take over
        info = source.info()
        self.assertEqual(info["state"], "ambiguous")
        self.assertEqual([f["name"] for f in info["csv_files"]], ["patients (edited).csv", "patients.csv"])
        self.assertIsNone(info["csv"])
        self.assertIn("2 CSV files", info["error"])
        for call in (source.inspect, source.ingest):
            with self.assertRaises(WorkbenchError):
                call()
        self.assertFalse(source.refresh())  # no silent re-ingest of one of them
        self.assertEqual(source.fingerprints(["p1"])["p1"][1], "p1")  # the earlier data still answers
        with self.assertRaises(WorkbenchError):
            self.wb.uploads.start(source.id, "new.csv", 5, True)
        with self.assertRaises(WorkbenchError):
            self.wb.uploads.check(source.id, ["new.csv"])
        self.assertEqual(sorted(h["path"] for h in source.find("patients", 10)), ["patients (edited).csv", "patients.csv"])
        source.rename_item("patients (edited).csv", "scratch.csv")  # any of them can be renamed or deleted by name
        source.delete_item("scratch.csv", set())
        self.assertEqual(source.info()["state"], "ready")  # back to exactly one; the data was kept
        self.assertEqual(source.info()["rows"], 3)

    def test_configure_requires_exactly_one_key(self):
        source = self.table_source()
        columns = source.inspect()["columns"]
        columns[1]["is_key"] = True
        with self.assertRaises(WorkbenchError):
            source.configure(columns)


class UploadTests(Base):
    def test_chunked_upload_creates_item(self):
        source = self.file_source()
        result = self.upload(source, "slides/one.svs", b"0123456789abc")
        self.assertEqual((source.path / "slides/one.svs").read_bytes(), b"0123456789abc")
        self.assertEqual(result["item"]["path"], "slides/one.svs")
        self.assertFalse(list(source.path.rglob("*.part")))

    def test_collision_needs_overwrite_and_overwrite_changes_fingerprint(self):
        source = self.file_source()
        self.upload(source, "a.bin", b"one")
        with self.assertRaises(WorkbenchError):
            self.wb.uploads.start(source.id, "a.bin", 3, False)
        before = source.fingerprints([source.list_dir("", "", 1, 0)["files"][0]["id"]])
        time.sleep(0.01)
        self.upload(source, "a.bin", b"three", overwrite=True)
        after = source.fingerprints(list(before))
        self.assertNotEqual(before, after)

    def test_check_reports_existing_paths(self):
        source = self.file_source()
        self.upload(source, "a/one.svs", b"x")
        self.assertEqual(self.wb.uploads.check(source.id, ["a/one.svs", "a/two.svs", "b/one.svs"]), ["a/one.svs"])
        self.assertEqual(self.wb.uploads.check(source.id, []), [])
        with self.assertRaises(WorkbenchError):
            self.wb.uploads.check(source.id, ["../x"])
        table = self.wb.catalog.create_source("", "t", "table")
        self.assertEqual(self.wb.uploads.check(table.id, ["new.csv"]), [])
        self.upload(table, "first.csv", b"id\n1\n")
        self.assertEqual(self.wb.uploads.check(table.id, ["other.csv"]), ["first.csv"])  # replaced in place, whatever its name

    def test_rejects_dangerous_destinations(self):
        source = self.file_source()
        for bad in ("../x", "/etc/passwd", "a/../../b", "source.sqlite", "x.part", ".hidden"):
            with self.assertRaises(WorkbenchError, msg=bad):
                self.wb.uploads.start(source.id, bad, 1)

    def test_resync_and_incomplete_commit(self):
        source = self.file_source()
        info = self.wb.uploads.start(source.id, "a.bin", 10)
        self.assertTrue(self.wb.uploads.append(info["upload_id"], 5, b"xxxxx").get("resync"))
        self.wb.uploads.append(info["upload_id"], 0, b"xxxxx")
        with self.assertRaises(WorkbenchError):
            self.wb.uploads.commit(info["upload_id"])
        self.wb.uploads.abort(info["upload_id"])
        self.assertFalse(list(source.path.rglob("*.part")))

    def test_table_source_takes_one_csv_and_replaces_it(self):
        source = self.wb.catalog.create_source("", "t", "table")
        self.upload(source, "first.csv", b"id,v\n1,2\n")
        with self.assertRaises(WorkbenchError):
            self.wb.uploads.start(source.id, "other.csv", 3, False)
        self.upload(source, "other.csv", b"id,v\n1,3\n", overwrite=True)
        self.assertEqual([p.name for p in source.path.glob("*.csv")], ["first.csv"])
        with self.assertRaises(WorkbenchError):
            self.wb.uploads.start(source.id, "x.txt", 3, True)


class DatasetTests(Base):
    def setUp(self):
        super().setUp()
        self.files = self.file_source()
        write(self.files.path / "a.svs", b"aaaa")
        write(self.files.path / "b.pdf", b"bbbbb")
        self.files.scan()
        self.table = self.table_source()
        self.table.configure(self.table.inspect()["columns"])
        self.ds = self.wb.datasets.create("WSI VQA")

    def schema(self):
        return self.ds.set_schema([
            {"name": "patient", "kind": "row", "sources": [self.table.id], "columns": ["id", "name"], "required": True},
            {"name": "slide", "kind": "file", "sources": [self.files.id], "extensions": ["svs"], "required": True},
            {"name": "question", "kind": "value", "value_type": "text", "required": True},
            {"name": "score", "kind": "value", "value_type": "integer", "constraints": {"min": 0, "max": 10}},
        ])

    def cell(self, entry, name):
        return next(c for c in entry["cells"] if c["name"] == name)

    def slot_id(self, name):
        return next(s["id"] for s in self.ds.slots() if s["name"] == name)

    def test_new_entry_is_incomplete_and_filling_it_makes_it_valid(self):
        self.schema()
        entry = self.ds.create_entry()
        self.assertEqual(entry["state"], "incomplete")
        self.ds.set_cell(entry["id"], self.slot_id("patient"), {"source": self.table.id, "item": "p1"})
        slide = self.files.list_dir("", "", 10, 0)["files"][0]
        self.ds.set_cell(entry["id"], self.slot_id("slide"), {"source": self.files.id, "item": slide["id"]})
        entry = self.ds.set_cell(entry["id"], self.slot_id("question"), {"value": "What is shown?"})
        self.assertEqual(entry["state"], "valid")
        self.assertEqual(self.cell(entry, "slide")["ref"]["label"], "a.svs")
        self.assertTrue(self.cell(entry, "slide")["ref"]["path"].endswith("a.svs"))
        self.assertEqual(self.cell(entry, "patient")["ref"]["values"]["name"], "Ann")

    def fill(self):
        self.schema()
        entry = self.ds.create_entry()
        slide = next(f for f in self.files.list_dir("", "", 10, 0)["files"] if f["ext"] == "svs")
        self.ds.set_cell(entry["id"], self.slot_id("patient"), {"source": self.table.id, "item": "p1"})
        self.ds.set_cell(entry["id"], self.slot_id("slide"), {"source": self.files.id, "item": slide["id"]})
        return self.ds.set_cell(entry["id"], self.slot_id("question"), {"value": "q"})

    def test_edited_source_file_corrupts_entry_until_marked_valid(self):
        entry = self.fill()
        time.sleep(0.01)
        write(self.files.path / "a.svs", b"changed!")
        self.wb.source_changed(self.files.id)
        entry = self.ds.get_entry(entry["id"])
        self.assertEqual(entry["state"], "corrupted")
        self.assertEqual(self.cell(entry, "slide")["state"], "source_changed")
        entry = self.ds.mark_valid(entry["id"], self.slot_id("slide"))
        self.assertEqual(entry["state"], "valid")

    def test_deleted_file_is_missing_and_mark_valid_clears_the_reference(self):
        entry = self.fill()
        (self.files.path / "a.svs").unlink()
        self.wb.source_changed(self.files.id)
        entry = self.ds.get_entry(entry["id"])
        self.assertEqual(self.cell(entry, "slide")["state"], "source_missing")
        entry = self.ds.mark_valid(entry["id"], self.slot_id("slide"))
        self.assertEqual(self.cell(entry, "slide")["state"], "incomplete")
        self.assertEqual(entry["state"], "incomplete")

    def test_row_fingerprint_covers_only_the_selected_columns(self):
        entry = self.fill()
        # age is not a selected column of the slot, name is
        write(self.table.path / "patients.csv", "id,name,age\np1,Ann,99\np2,Bob,41\n")
        self.table.refresh()
        self.wb.source_changed(self.table.id)
        self.assertEqual(self.ds.get_entry(entry["id"])["state"], "valid")
        write(self.table.path / "patients.csv", "id,name,age\np1,Anna,99\np2,Bob,41\n")
        self.table.refresh()
        self.wb.source_changed(self.table.id)
        self.assertEqual(self.ds.get_entry(entry["id"])["state"], "corrupted")

    def test_removed_source_marks_references_missing(self):
        entry = self.fill()
        files_id = self.files.id
        self.wb.catalog.remove_source(files_id)
        self.wb.source_changed(files_id)
        self.assertEqual(self.cell(self.ds.get_entry(entry["id"]), "slide")["state"], "source_missing")
        self.assertEqual(self.ds.detail()["schema"][1]["dangling_sources"], [files_id])

    def test_schema_edit_makes_entries_invalid_instead_of_blocking(self):
        entry = self.fill()
        self.ds.set_cell(entry["id"], self.slot_id("score"), {"value": 7})
        slots = self.ds.slots()
        for s in slots:
            if s["name"] == "score":
                s["constraints"] = {"max": 5}
            if s["name"] == "slide":
                s["extensions"] = ["tif"]
        self.ds.set_schema(slots)
        entry = self.ds.get_entry(entry["id"])
        self.assertEqual(self.cell(entry, "score")["state"], "invalid")
        self.assertEqual(self.cell(entry, "slide")["state"], "invalid")
        self.assertEqual(entry["state"], "incomplete")

    def test_adding_required_slot_makes_existing_entries_incomplete(self):
        entry = self.fill()
        slots = self.ds.slots() + [{"name": "answer", "kind": "value", "value_type": "text", "required": True}]
        self.ds.set_schema(slots)
        self.assertEqual(self.ds.get_entry(entry["id"])["state"], "incomplete")
        self.assertEqual(self.ds.summary()["states"]["incomplete"], 1)

    def test_changing_slot_kind_clears_cells_and_removing_slot_drops_them(self):
        entry = self.fill()
        slots = self.ds.slots()
        patient = next(s for s in slots if s["name"] == "patient")
        patient.update(kind="value", value_type="text")
        self.ds.set_schema([s for s in slots if s["name"] != "score"])
        entry = self.ds.get_entry(entry["id"])
        self.assertIsNone(self.cell(entry, "patient")["ref"])
        self.assertNotIn("score", [c["name"] for c in entry["cells"]])

    def test_values_are_validated_on_input(self):
        self.schema()
        entry = self.ds.create_entry()
        with self.assertRaises(WorkbenchError):
            self.ds.set_cell(entry["id"], self.slot_id("score"), {"value": "abc"})
        entry = self.ds.set_cell(entry["id"], self.slot_id("score"), {"value": "11"})
        self.assertEqual(self.cell(entry, "score")["state"], "invalid")
        entry = self.ds.set_cell(entry["id"], self.slot_id("score"), {"value": "4"})
        self.assertEqual(self.cell(entry, "score")["state"], "ok")

    def test_reference_must_exist_and_be_allowed(self):
        self.schema()
        entry = self.ds.create_entry()
        with self.assertRaises(WorkbenchError):
            self.ds.set_cell(entry["id"], self.slot_id("patient"), {"source": self.table.id, "item": "nope"})
        with self.assertRaises(WorkbenchError):
            self.ds.set_cell(entry["id"], self.slot_id("patient"), {"source": self.files.id, "item": "x"})

    def test_unique_slot_flags_duplicates_on_both_entries(self):
        self.ds.set_schema([{"name": "code", "kind": "value", "value_type": "text", "unique": True}])
        a, b = self.ds.create_entry(), self.ds.create_entry()
        slot = self.slot_id("code")
        self.ds.set_cell(a["id"], slot, {"value": "x"})
        self.ds.set_cell(b["id"], slot, {"value": "x"})
        self.assertEqual(self.cell(self.ds.get_entry(a["id"]), "code")["state"], "invalid")
        self.assertEqual(self.cell(self.ds.get_entry(b["id"]), "code")["state"], "invalid")
        self.ds.set_cell(b["id"], slot, {"value": "y"})
        self.assertEqual(self.cell(self.ds.get_entry(a["id"]), "code")["state"], "ok")

    def test_listing_filter_search_and_stats(self):
        entry = self.fill()
        self.ds.create_entry()
        self.assertEqual(self.ds.list_entries()["total"], 2)
        self.assertEqual(self.ds.list_entries(state="valid")["total"], 1)
        hits = self.ds.list_entries(q="a.svs")
        self.assertEqual([e["id"] for e in hits["entries"]], [entry["id"]])
        stats = self.ds.stats()
        self.assertEqual(stats["states"], {"valid": 1, "incomplete": 1, "corrupted": 0})
        slide = next(s for s in stats["slots"] if s["name"] == "slide")
        self.assertEqual((slide["filled"], slide["states"]["ok"], slide["states"]["incomplete"]), (1, 1, 1))

    def test_candidates_respect_slot_rules(self):
        self.schema()
        slide_slot = self.slot_id("slide")
        self.assertEqual([c["label"] for c in self.ds.candidates(slide_slot, "")], ["a.svs"])
        rows = self.ds.candidates(self.slot_id("patient"), "bo")
        self.assertEqual([c["item"] for c in rows], ["p2"])

    def test_check_dataset_picks_up_changed_csv(self):
        entry = self.fill()
        time.sleep(0.01)
        write(self.table.path / "patients.csv", "id,name,age\np1,Changed,1\n")
        self.wb.check_dataset(self.ds.id)
        self.assertEqual(self.ds.get_entry(entry["id"])["state"], "corrupted")
        self.assertTrue(any(e["type"] == "job_done" and e["ok"] for e in self.events))

    def test_default_values_and_choices(self):
        self.ds.set_schema([{"name": "kind", "kind": "value", "value_type": "choice",
                             "constraints": {"choices": ["a", "b"], "default": "a"}}])
        entry = self.ds.create_entry()
        self.assertEqual(self.cell(entry, "kind")["value"], "a")
        entry = self.ds.set_cell(entry["id"], self.slot_id("kind"), {"value": "zzz"})
        self.assertEqual(self.cell(entry, "kind")["state"], "invalid")

    def test_dataset_names_become_unique_folders_and_delete_works(self):
        other = self.wb.datasets.create("WSI VQA")
        self.assertNotEqual(other.id, self.ds.id)
        self.wb.datasets.delete(other.id)
        self.assertEqual([d["id"] for d in self.wb.datasets.list()], [self.ds.id])


class ValueTests(unittest.TestCase):
    def test_coerce(self):
        self.assertEqual(coerce_value("integer", "5"), 5)
        self.assertEqual(coerce_value("integer", 5.0), 5)
        self.assertEqual(coerce_value("boolean", "yes"), True)
        self.assertEqual(coerce_value("date", "2024-02-29"), "2024-02-29")
        for kind, bad in (("integer", "1.5"), ("integer", True), ("date", "2024-13-01"), ("float", "nan"), ("boolean", "maybe")):
            with self.assertRaises(ValueError):
                coerce_value(kind, bad)


if __name__ == "__main__":
    unittest.main()
