"""Streamed strokes (plugin.py, ws://smartbrush): order, merged points, the commit after the last point, cancel."""
import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.argv = sys.argv[:1] + ["unused-socket"]  # the SDK's Plugin() wants a socket path; nothing connects in the tests

import plugin  # noqa: E402
import salus_sdk as salus  # noqa: E402


class FakeStroke:
    def __init__(self, slow=0.0):
        self.lock = threading.Lock()
        self.points = []
        self.slide = None
        self.slow = slow

    def to_dab(self, read, x, y):
        time.sleep(self.slow)
        self.points.append((x, y))
        return [{"x": x, "y": y}]


def Message(frontend_id, data):
    """What the SDK hands a channel handler."""
    return salus.ChannelMessage(frontend_id, "smartbrush", salus.Payload.json(data))


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.strokes = []
        self.committed = []
        self.originals = (plugin.stream_send, plugin.new_stroke, plugin.commit_stroke, plugin.smartbrush_module)
        plugin.stream_send = lambda fe, stroke, message: self.sent.append((fe, stroke, message))
        plugin.smartbrush_module = lambda: type("sb", (), {"slide_reader": staticmethod(lambda slide: None)})
        self.slow = 0.0

        def new_stroke(sb, data, x, y, fe):
            stroke = FakeStroke(self.slow)
            self.strokes.append(stroke)
            return stroke
        plugin.new_stroke = new_stroke

        def commit(stroke, class_id):
            self.committed.append((list(stroke.points), class_id))
            return {"changed": 1}
        plugin.commit_stroke = commit

    def tearDown(self):
        plugin.stream_send, plugin.new_stroke, plugin.commit_stroke, plugin.smartbrush_module = self.originals

    def send(self, *messages, fe=7):
        async def go():
            for data in messages:
                await plugin.smart_stream(Message(fe, data))
        asyncio.run(go())

    def wait_done(self, stroke="s1", timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(m["type"] in ("done", "error") for _, s, m in self.sent if s == stroke):
                return
            time.sleep(0.01)
        self.fail("no answer")

    def test_points_in_order_then_the_commit(self):
        self.send({"type": "start", "stroke": "s1", "x": 0, "y": 0},
                  *[{"type": "point", "stroke": "s1", "x": i, "y": 0} for i in range(1, 6)],
                  {"type": "end", "stroke": "s1", "class_id": 3})
        self.wait_done()
        points, class_id = self.committed[0]
        self.assertEqual(class_id, 3)
        self.assertEqual(points[0], (0.0, 0.0))
        self.assertEqual(points[-1], (5.0, 0.0), "the last point is done before the commit")
        self.assertEqual(points, sorted(points), "in the order sent")
        self.assertEqual(self.sent[-1][2]["type"], "done")
        self.assertTrue(all(fe == 7 and m["type"] == "dabs" for fe, _, m in self.sent[:-1]))

    def test_points_waiting_behind_a_slow_one_are_merged(self):
        self.slow = 0.05
        self.send({"type": "start", "stroke": "s1", "x": 0, "y": 0},
                  *[{"type": "point", "stroke": "s1", "x": i, "y": 0} for i in range(1, 40)],
                  {"type": "end", "stroke": "s1", "class_id": 3})
        self.wait_done()
        points, _ = self.committed[0]
        self.assertEqual(points[-1], (39.0, 0.0))
        self.assertLess(len(points), 40, "a slow server catches up instead of working through every point")

    def test_late_messages_and_cancel(self):
        self.send({"type": "point", "stroke": "nope", "x": 1, "y": 1})  # no start: ignored
        self.assertEqual(self.strokes, [])
        self.send({"type": "start", "stroke": "s2", "x": 0, "y": 0}, {"type": "cancel", "stroke": "s2"},
                  {"type": "end", "stroke": "s2", "class_id": 1})
        time.sleep(0.2)
        self.assertEqual(self.committed, [])
        with plugin.stream_lock:
            self.assertNotIn((7, "s2"), plugin.stream_queues)

    def test_errors_reach_the_viewer(self):
        def broken(sb, data, x, y, fe):
            raise plugin.ViewerError("This stroke belongs to another image.", 409)
        plugin.new_stroke = broken
        self.send({"type": "start", "stroke": "s1", "x": 0, "y": 0})
        self.wait_done()
        self.assertEqual(self.sent[-1][2], {"type": "error", "message": "This stroke belongs to another image."})


if __name__ == "__main__":
    unittest.main()
