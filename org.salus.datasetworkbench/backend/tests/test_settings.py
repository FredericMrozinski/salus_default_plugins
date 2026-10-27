import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.settings import RootSettings  # noqa: E402
from workbench.util import WorkbenchError  # noqa: E402


class FakeConfig:
    """What the SDK's settings offer to a worker thread, kept in a dict."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def get_blocking(self, key, default=None):
        return self.values.get(key, default)


class Settings(unittest.TestCase):
    def test_nothing_is_set_at_first(self):
        self.assertIsNone(RootSettings(FakeConfig()).stored())

    def test_the_setting_is_read_each_time(self):
        config = FakeConfig()
        settings = RootSettings(config)
        config.values["root"] = "/data/wb"
        self.assertEqual(settings.stored(), "/data/wb")

    def test_empty_or_wrong_settings_mean_not_set(self):
        for value in ("", "   ", 5, None, []):
            self.assertIsNone(RootSettings(FakeConfig({"root": value})).stored(), repr(value))

    def test_normalize(self):
        self.assertEqual(RootSettings.normalize("  /a/b/../wb/  "), Path("/a/wb"))
        self.assertEqual(RootSettings.normalize("~/wb"), Path.home() / "wb")
        for bad in ("", "   ", "relative/path", "wb", "/", None, 5, "/a\0b"):
            with self.assertRaises(WorkbenchError, msg=repr(bad)):
                RootSettings.normalize(bad)
            self.assertIsNone(RootSettings.normalize_or_none(bad))


if __name__ == "__main__":
    unittest.main()
