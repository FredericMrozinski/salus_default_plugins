"""Hold to hide (ws://hide, broadcast on "events") and the fill opacity from the settings."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.argv = sys.argv[:1] + ["unused-socket"]  # the SDK's Plugin() wants a socket path; nothing connects in the tests

import plugin  # noqa: E402
import salus_sdk as salus  # noqa: E402


class HideTest(unittest.TestCase):
    def setUp(self):
        self.sent = []

        async def send(data, channel=None, to=None):
            self.sent.append((channel, to, data.as_json()))
        self.original = plugin.app.send
        plugin.app.send = send
        plugin.hide_holders.clear()

    def tearDown(self):
        plugin.app.send = self.original
        plugin.hide_holders.clear()

    def hide(self, frontend, on):
        asyncio.run(plugin.hide(salus.ChannelMessage(frontend, "hide", salus.Payload.json({"on": on}))))

    def test_held_down_and_released(self):
        self.hide(5, True)
        self.assertEqual(self.sent, [("events", salus.ALL, {"type": "hide", "on": True})])
        self.hide(5, False)
        self.assertEqual(self.sent[-1][2], {"type": "hide", "on": False})

    def test_two_panels_holding_and_a_panel_that_goes_away(self):
        self.hide(5, True)
        self.hide(6, True)  # still hidden: no second message
        self.assertEqual(len(self.sent), 1)
        self.hide(5, False)  # 6 still holds
        self.assertEqual(len(self.sent), 1)
        asyncio.run(plugin.detached(6))  # the panel holding it is closed: shown again
        self.assertEqual(self.sent[-1][2], {"type": "hide", "on": False})
        self.assertEqual(plugin.hide_holders, set())


class FillOpacityTest(unittest.TestCase):
    def test_from_the_setting_in_percent(self):
        original = plugin.app.config.get_blocking
        try:
            for stored, expected in ((35, 0.35), (0, 0.0), (100, 1.0), (250, 1.0), ("x", 0.35)):
                plugin.app.config.get_blocking = lambda key, default=None, v=stored: v
                self.assertEqual(plugin.fill_opacity(), expected)
        finally:
            plugin.app.config.get_blocking = original
        self.assertEqual(plugin.fill_opacity(), 0.35, "not readable (no Salus here): the default")

    def test_the_viewer_cannot_set_it_any_more(self):
        self.assertNotIn("opacity", plugin.state)


if __name__ == "__main__":
    unittest.main()
