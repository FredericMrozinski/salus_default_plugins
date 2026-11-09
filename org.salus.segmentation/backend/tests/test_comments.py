import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from annotations import Annotations  # noqa: E402
from comments import Comments, current_user  # noqa: E402
from errors import ViewerError  # noqa: E402
from project import Project  # noqa: E402

SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]


class CommentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "images/sub").mkdir(parents=True)
        (self.dir / "images/sub/a.svs").write_bytes(b"x")
        self.project = Project.create("P", self.dir / "images", ".svs", self.dir / "save")
        self.cls = self.project.add_class("Tumor", "#ff0000")

    def tearDown(self):
        self.tmp.cleanup()

    def test_comments_are_kept_per_shape_in_order(self):
        comments = Comments(self.project, "sub/a.svs")
        self.assertEqual(comments.all(), {})
        first = comments.add("s1", "  Is this tumor?\nLooks like stroma.  ", author="alice")
        comments.add("s1", "Agreed, stroma.", author="bob")
        comments.add("s2", "Fine.", author="alice")
        again = Comments(self.project, "sub/a.svs").all()  # read back from the file
        self.assertEqual([(c["author"], c["text"]) for c in again["s1"]], [("alice", "Is this tumor?\nLooks like stroma."), ("bob", "Agreed, stroma.")])
        self.assertEqual(again["s1"][0], first)
        self.assertIn("T", first["time"])
        data = json.loads((self.dir / "save/comments/sub/a.svs.json").read_text())
        self.assertEqual(data["image"], "sub/a.svs")

    def test_author_is_the_system_user(self):
        with mock.patch.dict(os.environ, {"USER": "carol"}):
            self.assertEqual(current_user(), "carol")
            self.assertEqual(Comments(self.project, "sub/a.svs").add("s1", "hi")["author"], "carol")

    def test_empty_long_and_bad_paths_are_refused(self):
        comments = Comments(self.project, "sub/a.svs")
        for text in ("", "   ", None, 5, "x" * 10_001):
            with self.assertRaises(ViewerError):
                comments.add("s1", text, author="a")
        with self.assertRaises(ViewerError):
            Comments(self.project, "../x.svs")

    def test_only_the_author_edits_and_deletes(self):
        comments = Comments(self.project, "sub/a.svs")
        mine = comments.add("s1", "Tumour", author="alice")
        theirs = comments.add("s1", "Sure?", author="bob")
        edited = comments.edit("s1", mine["id"], " Tumor ", author="alice")
        self.assertEqual((edited["text"], edited["time"]), ("Tumor", mine["time"]))
        self.assertIn("edited", edited)
        with self.assertRaises(ViewerError) as e:
            comments.edit("s1", theirs["id"], "No", author="alice")
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(ViewerError):
            comments.delete("s1", theirs["id"], author="alice")
        with self.assertRaises(ViewerError):
            comments.edit("s1", mine["id"], "  ", author="alice")
        comments.delete("s1", mine["id"], author="alice")
        self.assertEqual([c["text"] for c in comments.all()["s1"]], ["Sure?"])
        comments.delete("s1", theirs["id"], author="bob")
        self.assertEqual(comments.all(), {})  # the last one takes the shape's entry with it
        with self.assertRaises(ViewerError) as e:
            comments.delete("s1", theirs["id"], author="bob")
        self.assertEqual(e.exception.status, 404)

    def test_conversations_can_be_resolved_and_reopened(self):
        comments = Comments(self.project, "sub/a.svs")
        with self.assertRaises(ViewerError):
            comments.resolve("s1", user="alice")  # nothing to resolve
        first = comments.add("s1", "Check the border", author="alice")
        entry = comments.resolve("s1", user="bob")
        self.assertEqual((entry["kind"], entry["author"]), ("resolved", "bob"))
        self.assertIsNone(comments.resolve("s1", user="bob"))  # resolved already
        self.assertEqual(Comments(self.project, "sub/a.svs").resolved(), {"s1": entry})
        comments.edit("s1", first["id"], "Check the left border", author="alice")
        self.assertIn("s1", comments.resolved())  # editing keeps it resolved
        with self.assertRaises(ViewerError):
            comments.edit("s1", entry["id"], "x", author="bob")  # entries are not comments
        comments.add("s1", "Not fixed yet", author="alice")
        self.assertEqual(comments.resolved(), {})  # a new comment opens it again
        comments.resolve("s1", user="bob")
        reopened = comments.resolve("s1", False, user="carol")
        self.assertEqual(comments.resolved(), {})
        kinds = [(c.get("kind"), c["author"]) for c in comments.all()["s1"]]
        self.assertEqual(kinds, [(None, "alice"), ("resolved", "bob"), (None, "alice"), ("resolved", "bob"), ("reopened", "carol")])
        self.assertEqual(comments.all()["s1"][-1], reopened)

    def test_deleting_the_last_comment_takes_the_entries_along(self):
        comments = Comments(self.project, "sub/a.svs")
        only = comments.add("s1", "Hm", author="alice")
        comments.resolve("s1", user="bob")
        comments.delete("s1", only["id"], author="alice")
        self.assertEqual(comments.all(), {})

    def test_resolved_map_of_earlier_files_becomes_an_entry(self):
        file = self.dir / "save/comments/sub/a.svs.json"
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps({"shapes": {"s1": [{"id": "c", "author": "a", "time": "t", "text": "x"}]},
                                    "resolved": {"s1": {"by": "b", "time": "t2"}}}))
        resolved = Comments(self.project, "sub/a.svs").resolved()
        self.assertEqual((resolved["s1"]["kind"], resolved["s1"]["author"]), ("resolved", "b"))

    def test_undo_of_drawing_leaves_comments_alone(self):
        store = Annotations(self.project, "sub/a.svs")
        shape_id = store.add_polygon(SQUARE, self.cls["id"])
        Comments(self.project, "sub/a.svs").add(shape_id, "Check this", author="a")
        store.erase([[50, 50]], 10)
        store.undo()
        store.undo()
        store.redo()
        self.assertEqual(Comments(self.project, "sub/a.svs").all()[shape_id][0]["text"], "Check this")
        self.assertFalse((self.dir / "save/annotations/sub/a.svs.geojson").read_text().count("Check this"))


if __name__ == "__main__":
    unittest.main()
