import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.settings import SETTINGS_FILE, RootSettings  # noqa: E402
from workbench.util import WorkbenchError  # noqa: E402


class FakeConfig:
    """What the SDK's config directory offers, kept in a folder."""

    def __init__(self, folder: Path):
        self.folder = folder

    def read_json(self, name, default=None):
        try:
            return json.loads((self.folder / name).read_text())
        except FileNotFoundError:
            return default

    def write_json(self, name, value):
        (self.folder / name).write_text(json.dumps(value))


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        (self.dir / "config").mkdir()
        self.settings = RootSettings(FakeConfig(self.dir / "config"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_is_set_at_first(self):
        self.assertIsNone(self.settings.stored())

    def test_save_and_read_back_and_keep_other_settings(self):
        (self.dir / "config" / SETTINGS_FILE).write_text(json.dumps({"other": 1}))
        self.settings.save(self.dir / "wb")
        self.assertEqual(self.settings.stored(), str(self.dir / "wb"))
        self.assertEqual(json.loads((self.dir / "config" / SETTINGS_FILE).read_text()), {"other": 1, "root": str(self.dir / "wb")})

    def test_damaged_or_empty_settings_mean_not_set(self):
        for content in ('{"root": ""}', '{"root": 5}', "[]", '{"root": "   "}'):
            (self.dir / "config" / SETTINGS_FILE).write_text(content)
            self.assertIsNone(self.settings.stored(), content)

    def test_normalize(self):
        self.assertEqual(self.settings.normalize(f"  {self.dir}/a/../wb/  "), self.dir / "wb")
        self.assertEqual(self.settings.normalize("~/wb"), Path.home() / "wb")
        for bad in ("", "   ", "relative/path", "wb", "/", None, 5, "/a\0b"):
            with self.assertRaises(WorkbenchError, msg=repr(bad)):
                self.settings.normalize(bad)

    def test_check_a_new_folder(self):
        info = self.settings.check(str(self.dir / "new" / "wb"))
        self.assertEqual((info["problem"], info["exists"], info["will_create"]), (None, False, True))

    def test_check_an_existing_workbench_and_other_folders(self):
        (self.dir / "mine" / "sources").mkdir(parents=True)
        self.assertTrue(self.settings.check(str(self.dir / "mine"))["has_workbench"])
        (self.dir / "photos").mkdir()
        (self.dir / "photos" / "a.jpg").write_bytes(b"x")
        info = self.settings.check(str(self.dir / "photos"))
        self.assertEqual((info["has_workbench"], info["has_files"], info["problem"]), (False, True, None))
        (self.dir / "empty").mkdir()
        info = self.settings.check(str(self.dir / "empty"))
        self.assertEqual((info["has_workbench"], info["has_files"]), (False, False))

    def test_check_refuses_files_and_unwritable_places(self):
        (self.dir / "file.txt").write_text("x")
        self.assertIn("file", self.settings.check(str(self.dir / "file.txt"))["problem"])
        locked = self.dir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            if os.access(locked, os.W_OK):  # running as root: nothing to test
                self.skipTest("the folder is writable anyway")
            self.assertIn("may not", self.settings.check(str(locked))["problem"])
            self.assertIn("cannot be created", self.settings.check(str(locked / "wb"))["problem"])
        finally:
            locked.chmod(0o700)
        self.assertIn("full path", self.settings.check("relative")["problem"])

    def test_suggestion_is_the_old_default(self):
        self.assertEqual(self.settings.suggestion(), Path.home() / "SalusWorkbench")


if __name__ == "__main__":
    unittest.main()
