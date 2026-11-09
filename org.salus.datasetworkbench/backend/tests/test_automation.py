import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.automation import derive_text, expand  # noqa: E402
from workbench.service import Workbench  # noqa: E402
from workbench.util import WorkbenchError  # noqa: E402


def write(path: Path, data: bytes | str = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


class TemplateTests(unittest.TestCase):
    def test_numbered_and_named_groups(self):
        m = re.search(r"(?P<patient>P\d+)_(\w+)\.svs", "cohort/P0042_HE.svs")
        self.assertEqual(expand(r"\1-\2", m), "P0042-HE")
        self.assertEqual(expand(r"\g<patient>/\g<0>", m), "P0042/P0042_HE.svs")

    def test_backslashes_and_unknown_groups(self):
        m = re.search(r"(a)", "a")
        self.assertEqual(expand(r"x\\1", m), r"x\1")  # \\ is a literal backslash
        self.assertEqual(expand(r"\n", m), r"\n")  # other escapes stay as they are
        with self.assertRaises(ValueError):
            expand(r"\2", m)
        with self.assertRaises(ValueError):
            expand(r"\g<nope>", m)

    def test_optional_group_that_did_not_match_is_empty(self):
        m = re.search(r"(a)(b)?", "a")
        self.assertEqual(expand(r"\1[\2]", m), "a[]")

    def test_derive_searches_and_returns_only_the_template(self):
        self.assertEqual(derive_text(r"_(\d+)", r"\1", "slide_17.svs"), "17")  # not 'slide17.svs' as sed would give
        with self.assertRaisesRegex(ValueError, "does not match"):
            derive_text(r"^X", r"\g<0>", "slide.svs")
        with self.assertRaisesRegex(ValueError, "empty"):
            derive_text(r"(a?)", r"\1", "zzz")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.wb = Workbench(Path(self.tmp.name) / "root", self.events.append, background=False)
        self.slides = self.wb.catalog.create_source("", "slides", "file")
        self.masks = self.wb.catalog.create_source("", "masks", "file")
        self.patients = self.wb.catalog.create_source("", "patients", "table")
        write(self.patients.path / "patients.csv", "id,age\nP1,30\nP2,41\n")
        self.patients.configure(self.patients.inspect()["columns"])
        self.ds = self.wb.datasets.create("D")
        self.ds.set_schema([
            {"name": "slide", "kind": "file", "sources": [self.slides.id], "extensions": ["svs"], "unique": True},
            {"name": "case", "kind": "value", "value_type": "text"},
            {"name": "number", "kind": "value", "value_type": "integer"},
            {"name": "patient", "kind": "row", "sources": [self.patients.id]},
            {"name": "mask", "kind": "file", "sources": [self.masks.id]},
            {"name": "note", "kind": "value", "value_type": "text"}])
        self.slot = {s["name"]: s["id"] for s in self.ds.slots()}

    def tearDown(self):
        self.tmp.cleanup()

    def rules(self, **rules):
        return {"rules": {str(self.slot[name]): rule for name, rule in rules.items()}}

    def standard(self):
        """slide is created from the cohort; case, number, patient and mask derive from it (patient and mask in a chain)."""
        return self.rules(
            slide={"mode": "source"},
            case={"mode": "derive", "from": self.slot["slide"], "read": r"(P\d+)_(\d+)\.svs$", "value": r"\1-\2"},
            number={"mode": "derive", "from": self.slot["slide"], "read": r"_(\d+)\.svs$", "value": r"\1"},
            patient={"mode": "derive", "from": self.slot["case"], "read": r"^(P\d+)-", "value": r"\1"},
            mask={"mode": "derive", "from": self.slot["case"], "read": r"^(?P<c>.+)$", "value": r"\g<c>.png"})

    def add_slide(self, rel):
        write(self.slides.path / rel, rel.encode())

    def entries(self):
        out = []
        for row in self.ds.list_entries(limit=500)["entries"]:
            entry = self.ds.get_entry(row["id"])
            cells = {c["name"]: c for c in entry["cells"]}
            out.append({name: (c["ref"]["label"] if c["ref"] else c["value"]) for name, c in cells.items()} |
                       {"_state": entry["state"], "_cells": cells})
        return sorted(out, key=lambda e: e["slide"] or "")


class SettingsTests(Base):
    def test_settings_persist_with_the_dataset(self):
        self.wb.set_automation(self.ds.id, self.standard())
        again = self.wb.datasets.get(self.ds.id)
        self.assertEqual(again.automation()["rules"][str(self.slot["slide"])], {"mode": "source"})
        self.assertEqual(again.detail()["automation"], again.automation())

    def test_at_most_one_source_field(self):
        with self.assertRaisesRegex(WorkbenchError, "Only one"):
            self.ds.set_automation(self.rules(slide={"mode": "source"}, mask={"mode": "source"}))

    def test_a_value_field_cannot_be_the_source(self):
        with self.assertRaises(WorkbenchError):
            self.ds.set_automation(self.rules(case={"mode": "source"}))

    def test_bad_patterns_unknown_groups_and_cycles_are_refused(self):
        derive = lambda origin, read=r"(.)", value=r"\1": {"mode": "derive", "from": self.slot[origin], "read": read, "value": value}
        with self.assertRaisesRegex(WorkbenchError, "invalid read pattern"):
            self.ds.set_automation(self.rules(case=derive("slide", read="(")))
        with self.assertRaisesRegex(WorkbenchError, "no group 2"):
            self.ds.set_automation(self.rules(case=derive("slide", value=r"\2")))
        with self.assertRaisesRegex(WorkbenchError, "cycle"):
            self.ds.set_automation(self.rules(case=derive("note"), note=derive("case")))
        with self.assertRaisesRegex(WorkbenchError, "itself"):
            self.ds.set_automation(self.rules(case=derive("case")))

    def test_rules_of_deleted_fields_are_dropped(self):
        self.ds.set_automation(self.standard())
        schema = [s for s in self.ds.slots() if s["name"] != "case"]
        self.ds.set_schema(schema)
        rules = self.ds.automation()["rules"]
        self.assertNotIn(str(self.slot["case"]), rules)
        self.assertNotIn(str(self.slot["patient"]), rules)  # it read the deleted field
        self.assertIn(str(self.slot["number"]), rules)


class RunTests(Base):
    def test_refresh_creates_one_entry_per_new_source_item_and_derives_in_a_chain(self):
        self.add_slide("P1_01.svs")
        self.add_slide("sub/P2_02.svs")
        self.add_slide("readme.txt")  # not an allowed extension: no entry
        write(self.masks.path / "P1-01.png")
        self.masks.scan()
        self.wb.set_automation(self.ds.id, self.standard())  # nothing scanned yet: no entries
        self.assertEqual(self.ds.summary()["entries"], 0)

        self.wb.scan_all()  # the Refresh button
        entries = self.entries()
        self.assertEqual([e["slide"] for e in entries], ["P1_01.svs", "sub/P2_02.svs"])
        first, second = entries
        self.assertEqual((first["case"], first["number"], first["patient"], first["mask"]), ("P1-01", 1, "P1", "P1-01.png"))
        self.assertEqual(first["_state"], "valid")
        self.assertEqual((second["case"], second["patient"]), ("P2-02", "P2"))
        self.assertIsNone(second["mask"])  # no P2-02.png: flagged
        self.assertEqual(second["_cells"]["mask"]["state"], "invalid")
        self.assertIn("automation: no item 'P2-02.png'", second["_cells"]["mask"]["detail"])
        self.assertEqual(second["_state"], "incomplete")
        self.assertTrue(any(e == {"type": "dataset_changed", "dataset": self.ds.id} for e in self.events))

        # a second refresh adds nothing; a new slide and the missing mask are picked up
        self.wb.scan_all()
        self.assertEqual(self.ds.summary()["entries"], 2)
        self.add_slide("P3_03.svs")
        self.upload_mask("P2-02.png")
        entries = {e["slide"]: e for e in self.entries()}
        self.assertEqual(sorted(entries), ["P1_01.svs", "P3_03.svs", "sub/P2_02.svs"])
        self.assertEqual(entries["sub/P2_02.svs"]["mask"], "P2-02.png")
        self.assertEqual(entries["sub/P2_02.svs"]["_state"], "valid")

    def upload_mask(self, rel):
        data = b"mask"
        info = self.wb.uploads.start(self.masks.id, rel, len(data), False)
        self.wb.uploads.append(info["upload_id"], 0, data)
        self.wb.finish_upload(info["upload_id"])
        self.wb.scan_all()

    def test_rows_of_a_table_can_be_the_source(self):
        self.ds.set_automation(self.rules(patient={"mode": "source"},
                                          note={"mode": "derive", "from": self.slot["patient"], "read": r"P(\d)", "value": r"patient \1"}))
        self.wb.check_dataset(self.ds.id)
        self.assertEqual(sorted((e["patient"], e["note"]) for e in self.entries()), [("P1", "patient 1"), ("P2", "patient 2")])

    def test_a_non_matching_regex_leaves_the_cell_empty_and_flags_it_until_filled_by_hand(self):
        self.add_slide("odd.svs")
        self.wb.set_automation(self.ds.id, self.standard())
        self.wb.scan_all()
        [entry] = self.entries()
        case = entry["_cells"]["case"]
        self.assertIsNone(case["value"])
        self.assertEqual(case["state"], "invalid")
        self.assertIn("does not match", case["detail"])
        self.assertEqual(entry["_cells"]["patient"]["state"], "ok")  # it reads the empty case: nothing to do, no flag
        entry_id = self.ds.list_entries()["entries"][0]["id"]
        self.ds.set_cell(entry_id, self.slot["case"], {"value": "P1-99"})
        self.assertEqual(self.ds.get_entry(entry_id)["cells"][1]["state"], "ok")
        self.ds.run_automation()  # the chain continues from the value typed by hand, which stays
        [entry] = self.entries()
        self.assertEqual((entry["case"], entry["patient"], entry["number"]), ("P1-99", "P1", None))
        self.assertIn("does not match", entry["_cells"]["number"]["detail"])

    def test_values_are_converted_to_the_field_type(self):
        self.add_slide("P1_01.svs")
        self.ds.set_automation(self.rules(slide={"mode": "source"},
                                          number={"mode": "derive", "from": self.slot["slide"], "read": r"(P\d)", "value": r"\1"}))
        self.ds.run_automation()
        self.wb.scan_all()
        [entry] = self.entries()
        self.assertIn("not an integer", entry["_cells"]["number"]["detail"])

    def test_values_typed_by_hand_are_not_overwritten(self):
        self.add_slide("P1_01.svs")
        self.ds.set_automation(self.standard())
        self.wb.scan_all()
        entry_id = self.ds.list_entries()["entries"][0]["id"]
        self.ds.set_cell(entry_id, self.slot["case"], {"value": "mine"})
        self.ds.run_automation()
        self.assertEqual(self.entries()[0]["case"], "mine")

    def test_without_settings_refresh_creates_nothing(self):
        self.add_slide("P1_01.svs")
        self.wb.scan_all()
        self.assertEqual(self.ds.summary()["entries"], 0)


class PreviewTests(Base):
    def test_preview_shows_samples_results_and_problems_without_saving(self):
        from workbench.automation import preview
        self.add_slide("P1_01.svs")
        self.slides.scan()
        rules = self.standard()
        rules["rules"][str(self.slot["note"])] = {"mode": "derive", "from": self.slot["slide"], "read": "(", "value": "x"}
        out = preview(self.ds, rules, None)
        self.assertEqual(out["samples"], ["P1_01.svs"])
        self.assertEqual(out["sample"], "P1_01.svs")
        results = out["results"]
        self.assertEqual(results[str(self.slot["case"])]["text"], "P1-01")
        self.assertEqual(results[str(self.slot["patient"])]["text"], "P1")
        self.assertEqual(results[str(self.slot["patient"])]["note"], "links an existing item")
        self.assertIn("no such item", results[str(self.slot["mask"])]["note"])
        self.assertIn("invalid read pattern", results[str(self.slot["note"])]["error"])
        self.assertIn("invalid read pattern", out["error"])
        out = preview(self.ds, rules, "nothing")
        self.assertIn("does not match", results_of(out, self.slot["case"])["error"])
        self.assertEqual(self.ds.automation(), {"rules": {}})
        self.assertEqual(self.ds.summary()["entries"], 0)


def results_of(out, slot_id):
    return out["results"][str(slot_id)]


if __name__ == "__main__":
    unittest.main()
