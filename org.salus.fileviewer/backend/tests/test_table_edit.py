import csv
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import ViewerError  # noqa: E402
from table_edit import EditSession, Edits  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            s.close()
        self.tmp.cleanup()

    def make(self, name: str, content: bytes | str) -> Path:
        path = self.dir / name
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        return path

    def open(self, path: Path) -> EditSession:
        session = EditSession(path, owner=1)
        self.sessions.append(session)
        session.load()
        self.assertEqual(session.state, "ready", session.error)
        return session

    def cell_ids(self, session):
        return [c["id"] for c in session.columns]

    def read_back(self, path: Path, delimiter=","):
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.reader(f, delimiter=delimiter))


class RoundTrip(Base):
    def test_special_characters_never_break_the_file(self):
        path = self.make("a.csv", "id,note\n1,plain\n2,other\n")
        s = self.open(path)
        nasty = ['has,comma', 'has "quotes" inside', "two\nlines", 'all, of "them"\nat once', " padded ", "", ",", '"', "é ü 日本語"]
        rows = s.page(10, 0)["rows"]
        s.set_cells([{"row": rows[0][0], "col": s.columns[1]["id"], "value": nasty[0]}, {"row": rows[1][0], "col": s.columns[1]["id"], "value": nasty[1]}])
        for value in nasty[2:]:
            new = s.insert_rows("end")[0]
            s.set_cells([{"row": new, "col": s.columns[0]["id"], "value": "x"}, {"row": new, "col": s.columns[1]["id"], "value": value}])
        self.assertEqual(s.save(), {"saved": str(path), "name": "a.csv", "rows": s.rows, "columns": 2})
        back = self.read_back(path)
        self.assertEqual(back[0], ["id", "note"])
        self.assertEqual([r[1] for r in back[1:]], nasty)  # every value comes back exactly
        self.assertTrue(all(len(r) == 2 for r in back))  # no row grew an extra column

    def test_a_comma_in_a_header_name_is_quoted_too(self):
        path = self.make("a.csv", "a,b\n1,2\n")
        s = self.open(path)
        s.rename_column(s.columns[0]["id"], "last, first")
        s.save()
        self.assertEqual(self.read_back(path)[0], ["last, first", "b"])

    def test_values_stay_text(self):
        path = self.make("a.csv", "zip,price,flag\n007,1.50,TRUE\n00123,2.0,no\n")
        s = self.open(path)
        s.set_cells([{"row": s.page(1, 0)["rows"][0][0], "col": s.columns[2]["id"], "value": "yes"}])
        s.save()
        self.assertEqual(self.read_back(path), [["zip", "price", "flag"], ["007", "1.50", "yes"], ["00123", "2.0", "no"]])

    def test_format_of_the_original_is_kept(self):
        original = b"\xef\xbb\xbfa;b\r\n1;2\r\n3;4"  # BOM, semicolons, CRLF, no final line break
        path = self.make("a.csv", original)
        s = self.open(path)
        s.set_cells([{"row": s.page(1, 0)["rows"][0][0], "col": s.columns[0]["id"], "value": "x;y"}])
        s.save()
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(data, b'\xef\xbb\xbfa;b\r\n"x;y";2\r\n3;4')
        self.assertFalse(data.endswith(b"\n"))

    def test_tsv_with_tabs_in_a_cell(self):
        path = self.make("a.tsv", "a\tb\n1\t2\n")
        s = self.open(path)
        s.set_cells([{"row": s.page(1, 0)["rows"][0][0], "col": s.columns[0]["id"], "value": "left\tright"}])
        s.save()
        self.assertEqual(self.read_back(path, "\t"), [["a", "b"], ["left\tright", "2"]])

    def test_excel_export_with_unnamed_trailing_columns_survives(self):
        original = "First Name;Last Name;Born;;;;\r\nMax;Mustermann;1.3.1989;;;;\r\nHeike;Musterfrau;23.9.1998;;;;\r\n"
        path = self.make("a.csv", original)
        s = self.open(path)
        self.assertEqual(len(s.columns), 7)
        s.save()
        self.assertEqual(path.read_bytes(), original.encode())  # byte for byte: nothing changed, nothing lost

    def test_ragged_rows_blank_lines_and_wide_rows(self):
        path = self.make("a.csv", "a,b\n1\n\n2,3,4\n")
        s = self.open(path)
        self.assertEqual(s.skipped_blank, 1)
        self.assertEqual([c["name"] for c in s.columns], ["a", "b", ""])  # the wide row got an unnamed third column
        self.assertEqual([r[1:] for r in s.page(10, 0)["rows"]], [["1", "", ""], ["2", "3", "4"]])

    def test_refuses_what_it_cannot_edit_faithfully(self):
        path = self.make("a.csv", b"caf\xe9,x\n")  # latin-1, not UTF-8
        s = EditSession(path, owner=1)
        self.sessions.append(s)
        s.load()
        self.assertEqual(s.state, "error")
        self.assertIn("not UTF-8", s.error)
        with self.assertRaises(ViewerError):
            Edits().start(str(self.make("a.json", "{}")), owner=1)


class Editing(Base):
    def test_rows_columns_and_order(self):
        path = self.make("a.csv", "a,b\n1,2\n3,4\n")
        s = self.open(path)
        first, second = [r[0] for r in s.page(10, 0)["rows"]]
        a, b = self.cell_ids(s)
        mid = s.insert_rows({"after": first})[0]
        top = s.insert_rows("start")[0]
        bottom = s.insert_rows({"before": second}, count=2)
        end = s.insert_rows("end")[0]
        self.assertEqual([r[0] for r in s.page(20, 0)["rows"]], [top, first, mid, *bottom, second, end])
        s.delete_rows([top, *bottom])
        self.assertEqual(s.rows, 4)
        new = s.add_column("c", after=a)
        s.rename_column(b, "B")
        s.delete_column(a)
        self.assertEqual([c["name"] for c in s.columns], ["c", "B"])
        s.set_cells([{"row": first, "col": new["id"], "value": "n"}])
        s.save()
        self.assertEqual(self.read_back(path), [["c", "B"], ["n", "2"], ["", ""], ["", "4"], ["", ""]][:1] + [["n", "2"], ["", ""], ["", "4"], ["", ""]])

    def test_many_inserts_in_the_same_spot_keep_their_order(self):
        s = self.open(self.make("a.csv", "a\n1\n2\n"))
        first = s.page(1, 0)["rows"][0][0]
        ids = []
        for _ in range(60):  # 60 times right after the first row: the gap halves each time
            ids.append(s.insert_rows({"after": first})[0])
        order = [r[0] for r in s.page(100, 0)["rows"]]
        self.assertEqual(order[0], first)
        self.assertEqual(order[1:61], list(reversed(ids)))

    def test_bad_input(self):
        s = self.open(self.make("a.csv", "a,b\n1,2\n"))
        with self.assertRaises(ViewerError):
            s.set_cells([{"row": 999, "col": 1, "value": "x"}])
        with self.assertRaises(ViewerError):
            s.set_cells([{"row": 1, "col": 1, "value": 5}])
        with self.assertRaises(ViewerError):
            s.rename_column(1, "two\nlines")
        with self.assertRaises(ViewerError):
            s.delete_column(404)
        self.assertEqual(s.modified, 0)  # a failed batch changes nothing

    def test_modified_counter_and_discard(self):
        path = self.make("a.csv", "a\n1\n")
        s = self.open(path)
        self.assertEqual(s.modified, 0)
        s.insert_rows("end")
        s.add_column("b")
        self.assertEqual(s.modified, 2)
        s.discard()
        self.assertEqual((s.modified, s.rows, len(s.columns)), (0, 1, 1))


class Saving(Base):
    def test_conflict_when_the_file_changed_meanwhile(self):
        path = self.make("a.csv", "a\n1\n")
        s = self.open(path)
        s.insert_rows("end")
        time.sleep(0.01)
        path.write_text("a\n1\n2\n3\n")  # somebody else
        self.assertEqual(s.save(), {"conflict": "changed"})
        self.assertEqual(path.read_text(), "a\n1\n2\n3\n")  # untouched
        self.assertEqual(s.modified, 1)  # the edits are still there
        result = s.save(overwrite=True)
        self.assertEqual(result["saved"], str(path))
        self.assertEqual(self.read_back(path), [["a"], ["1"], [""]])
        self.assertEqual(s.save(), {"saved": str(path), "name": "a.csv", "rows": 2, "columns": 1})  # in step again

    def test_conflict_when_the_file_was_deleted(self):
        path = self.make("a.csv", "a\n1\n")
        s = self.open(path)
        path.unlink()
        self.assertEqual(s.save(), {"conflict": "deleted"})
        self.assertFalse(path.exists())

    def test_save_elsewhere(self):
        path = self.make("a.csv", "a\n1\n")
        s = self.open(path)
        s.insert_rows("end")
        result = s.save("a.edited.csv")  # relative: next to the original
        self.assertEqual(result["saved"], str(self.dir / "a.edited.csv"))
        self.assertEqual(path.read_text(), "a\n1\n")  # the original is untouched
        self.assertEqual(self.read_back(self.dir / "a.edited.csv"), [["a"], ["1"], [""]])
        self.assertEqual(s.path, self.dir / "a.edited.csv")  # the session continues on the new file
        s.insert_rows("end")
        self.assertIn("saved", s.save())

    def test_save_elsewhere_is_careful(self):
        path = self.make("a.csv", "a\n1\n")
        other = self.make("other.csv", "x\n")
        s = self.open(path)
        for bad in ("other.csv", "a.txt", ".hidden.csv", "missing_folder/x.csv", str(self.dir)):
            with self.assertRaises(ViewerError, msg=bad):
                s.save(bad)
        self.assertEqual(other.read_text(), "x\n")

    def test_path_info_warns_only_about_tabular_source_folders(self):
        import sqlite3
        s = self.open(self.make("a.csv", "a\n1\n"))
        self.assertEqual(s.path_info("b.csv")["source_kind"], None)
        db = self.dir / "source.sqlite"
        for kind, warns in (("file", False), ("table", True)):
            db.unlink(missing_ok=True)
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO meta VALUES ('type', ?)", (kind,))
            conn.close()
            info = s.path_info("b.csv")
            self.assertEqual((info["source_kind"], info["folder_is_source"]), (kind, warns))
            self.assertEqual(info["csv_in_folder"], ["a.csv"])
            self.assertFalse(info["exists"])

    def test_no_temp_files_are_left_and_permissions_are_kept(self):
        path = self.make("a.csv", "a\n1\n")
        path.chmod(0o640)
        s = self.open(path)
        s.insert_rows("end")
        s.save()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.csv"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)


class Registry(Base):
    def test_sessions_by_owner(self):
        edits = Edits()
        a = edits.start(str(self.make("a.csv", "a\n1\n")), owner=7)
        b = edits.start(str(self.make("b.csv", "b\n1\n")), owner=8)
        deadline = time.time() + 5
        while (a.state == "loading" or b.state == "loading") and time.time() < deadline:
            time.sleep(0.01)
        edits.close_owned(7)
        with self.assertRaises(ViewerError):
            edits.get(a.id)
        self.assertEqual(edits.get(b.id).state, "ready")
        edits.close(b.id)
        with self.assertRaises(ViewerError):
            edits.get(b.id)


if __name__ == "__main__":
    unittest.main()
