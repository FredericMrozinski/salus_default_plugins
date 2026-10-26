import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_settings import FakeConfig  # noqa: E402
from workbench.active import NOT_SET_UP, Active  # noqa: E402
from workbench.settings import SETTINGS_FILE, RootSettings  # noqa: E402
from workbench.util import WorkbenchError  # noqa: E402


class ActiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.config_dir = self.dir / "config"
        self.config_dir.mkdir()
        self.events = []
        self.settings = RootSettings(FakeConfig(self.config_dir))

    def tearDown(self):
        self.tmp.cleanup()

    def make(self) -> Active:
        active = Active(self.settings, self.events.append)
        active.load()
        return active

    def stored(self):
        return json.loads((self.config_dir / SETTINGS_FILE).read_text())

    def test_nothing_set_means_no_workbench_and_data_routes_say_so(self):
        active = self.make()
        status = active.status()
        self.assertEqual((status["root"], status["active"], status["error"]), (None, False, None))
        self.assertEqual(status["suggestion"], str(Path.home() / "SalusWorkbench"))
        for use in (lambda: active.catalog, lambda: active.datasets.list(), lambda: active.uploads):
            with self.assertRaises(WorkbenchError) as raised:
                use()
            self.assertEqual(raised.exception.status, NOT_SET_UP)

    def test_choosing_a_folder_creates_it_stores_it_and_starts_the_workbench(self):
        active = self.make()
        root = self.dir / "deep" / "wb"
        self.assertEqual(active.use(str(root)), root)
        self.assertTrue((root / "sources").is_dir() and (root / "datasets").is_dir())
        self.assertEqual(self.stored(), {"root": str(root)})
        self.assertEqual(active.catalog.root, root)  # the routes now reach the workbench
        self.assertEqual(active.datasets.list(), [])
        self.assertEqual(active.status()["active"], True)
        self.assertIn({"type": "config_changed"}, self.events)

    def test_the_stored_folder_is_used_at_the_next_start(self):
        root = self.dir / "wb"
        self.make().use(str(root))
        second = self.make()
        self.assertEqual((second.root, second.status()["active"]), (root, True))

    def test_an_existing_workbench_is_taken_over_with_its_data(self):
        first = self.make()
        first.use(str(self.dir / "wb"))
        first.catalog.create_group("", "kept")
        (self.dir / "config2").mkdir()
        other = Active(RootSettings(FakeConfig(self.dir / "config2")), lambda e: None)  # another installation, same folder
        other.use(str(self.dir / "wb"))
        self.assertEqual([n["name"] for n in other.catalog.tree()], ["kept"])

    def test_a_bad_choice_leaves_the_working_folder_alone(self):
        active = self.make()
        good = self.dir / "wb"
        active.use(str(good))
        (self.dir / "a_file").write_text("x")
        for bad in ("relative", "", str(self.dir / "a_file"), "/"):
            with self.assertRaises(WorkbenchError, msg=bad):
                active.use(bad)
        self.assertEqual((active.root, self.stored()), (good, {"root": str(good)}))
        self.assertEqual(active.datasets.list(), [])

    def test_switching_folders(self):
        active = self.make()
        a, b = self.dir / "a", self.dir / "b"
        active.use(str(a))
        active.catalog.create_group("", "only_in_a")
        active.use(str(b))
        self.assertEqual(active.catalog.tree(), [])
        self.assertEqual(self.stored(), {"root": str(b)})
        active.use(str(a))
        self.assertEqual([n["name"] for n in active.catalog.tree()], ["only_in_a"])

    def test_a_stored_folder_that_cannot_be_used_is_reported_not_fatal(self):
        blocker = self.dir / "blocker"
        blocker.write_text("x")
        (self.config_dir / SETTINGS_FILE).write_text(json.dumps({"root": str(blocker / "wb")}))  # below a file
        active = self.make()
        status = active.status()
        self.assertEqual((status["active"], status["root"]), (False, str(blocker / "wb")))
        self.assertIn("cannot be used", status["error"])
        self.assertEqual(active.use(str(self.dir / "fixed")), self.dir / "fixed")  # and the user can fix it
        self.assertEqual((active.status()["active"], active.status()["error"]), (True, None))

    def test_a_config_file_edited_by_hand_is_followed(self):
        active = self.make()
        active.use(str(self.dir / "a"))
        self.events.clear()
        (self.config_dir / SETTINGS_FILE).write_text(json.dumps({"root": str(self.dir / "b")}))
        self.assertEqual(active.status()["active"], True)
        self.assertEqual(active.root, self.dir / "b")
        self.assertIn({"type": "config_changed"}, self.events)

    def test_without_a_config_directory_it_says_so(self):
        active = Active(None, lambda e: None, "SALUS_CONFIG_DIR is not set")
        active.load()
        self.assertEqual((active.status()["active"], active.status()["error"]), (False, "SALUS_CONFIG_DIR is not set"))
        with self.assertRaises(WorkbenchError) as raised:
            active.use(str(self.dir / "wb"))
        self.assertIn("SALUS_CONFIG_DIR", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
