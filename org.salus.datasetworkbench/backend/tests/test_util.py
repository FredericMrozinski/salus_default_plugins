import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench.util import CHECKPOINT_AFTER_CHANGES, Tx, connect  # noqa: E402


class CheckpointTests(unittest.TestCase):
    """A connection that wrote a lot checkpoints the WAL itself before it closes, outside the lock every open and close
    takes, so the close (under that lock) does not hold up the other threads with a large checkpoint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        with connect(self.path, create=True) as conn:
            conn.execute("CREATE TABLE t (n INTEGER)")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows: int) -> list:
        """Writes `rows` rows; returns what happened when the connection closed: ("checkpoint", lock held?), ("close", ...)."""
        from unittest import mock
        import workbench.util as util
        seen = []
        execute, close = util.Conn.execute, util.Conn.close

        def spy_execute(conn, sql, *args):
            if "wal_checkpoint" in sql:
                seen.append(("checkpoint", util._open_close.locked()))
            return execute(conn, sql, *args)

        def spy_close(conn):
            seen.append(("close", util._open_close.locked()))
            return close(conn)

        with mock.patch.object(util.Conn, "execute", spy_execute), mock.patch.object(util.Conn, "close", spy_close):
            with connect(self.path) as conn:
                with Tx(conn):
                    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
        return seen

    def test_a_large_write_checkpoints_before_closing_and_outside_the_lock(self):
        self.assertEqual(self.write(CHECKPOINT_AFTER_CHANGES + 10), [("checkpoint", False), ("close", True)])

    def test_a_small_write_just_closes(self):
        self.assertEqual(self.write(10), [("close", True)])

    def test_the_data_is_there(self):
        self.write(CHECKPOINT_AFTER_CHANGES + 10)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], CHECKPOINT_AFTER_CHANGES + 10)

if __name__ == "__main__":
    unittest.main()
