import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_settings import FakeConfig  # noqa: E402
from workbench.active import NOT_SET_UP, Active  # noqa: E402
from workbench.settings import RootSettings  # noqa: E402
from workbench.util import WorkbenchError  # noqa: E402


class ActiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        self.events = []
        self.config = FakeConfig()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self) -> Active:
        active = Active(RootSettings(self.config), self.events.append)
        active.load()
        return active

    def test_nothing_set_means_no_workbench_and_data_routes_say_so(self):
        active = self.make()
        status = active.status()
        self.assertEqual((status["root"], status["active"], status["error"]), (None, False, None))
        for use in (lambda: active.catalog, lambda: active.datasets.list(), lambda: active.uploads):
            with self.assertRaises(WorkbenchError) as raised:
                use()
            self.assertEqual(raised.exception.status, NOT_SET_UP)

    def test_the_set_folder_is_created_and_used_at_start(self):
        root = self.dir / "deep" / "wb"
        self.config.values["root"] = str(root)
        active = self.make()
        self.assertTrue((root / "sources").is_dir() and (root / "datasets").is_dir())
        self.assertEqual((active.root, active.status()["active"]), (root, True))
        self.assertEqual(active.datasets.list(), [])

    def test_setting_the_folder_later_is_followed_and_announced(self):
        active = self.make()
        self.config.values["root"] = str(self.dir / "wb")
        self.assertEqual(active.status()["active"], True)
        self.assertEqual(active.catalog.root, self.dir / "wb")  # the routes now reach the workbench
        self.assertIn({"type": "config_changed"}, self.events)

    def test_an_existing_workbench_is_taken_over_with_its_data(self):
        self.config.values["root"] = str(self.dir / "wb")
        self.make().catalog.create_group("", "kept")
        other = self.make()  # another start, same folder
        self.assertEqual([n["name"] for n in other.catalog.tree()], ["kept"])

    def test_switching_folders(self):
        a, b = self.dir / "a", self.dir / "b"
        self.config.values["root"] = str(a)
        active = self.make()
        active.catalog.create_group("", "only_in_a")
        self.config.values["root"] = str(b)
        active.status()
        self.assertEqual(active.catalog.tree(), [])
        self.config.values["root"] = str(a)
        active.status()
        self.assertEqual([n["name"] for n in active.catalog.tree()], ["only_in_a"])

    def test_a_folder_that_cannot_be_used_is_reported_not_fatal(self):
        blocker = self.dir / "blocker"
        blocker.write_text("x")
        self.config.values["root"] = str(blocker / "wb")  # below a file
        active = self.make()
        status = active.status()
        self.assertEqual((status["active"], status["root"]), (False, str(blocker / "wb")))
        self.assertIn("cannot be used", status["error"])
        self.config.values["root"] = str(self.dir / "fixed")  # fixed in the settings
        status = active.status()
        self.assertEqual((status["active"], status["error"]), (True, None))

    def test_a_relative_folder_is_reported(self):
        self.config.values["root"] = "relative/wb"
        status = self.make().status()
        self.assertEqual(status["active"], False)
        self.assertIn("full path", status["error"])

    def test_clearing_the_setting_turns_the_workbench_off(self):
        self.config.values["root"] = str(self.dir / "wb")
        active = self.make()
        del self.config.values["root"]
        self.assertEqual(active.status()["active"], False)
        with self.assertRaises(WorkbenchError):
            active.catalog


if __name__ == "__main__":
    unittest.main()
