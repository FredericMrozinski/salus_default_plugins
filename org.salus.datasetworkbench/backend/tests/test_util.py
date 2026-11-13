import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench import util  # noqa: E402
from workbench.util import CHECKPOINT_AFTER_CHANGES, Tx, close_idle, connect  # noqa: E402


class CheckpointTests(unittest.TestCase):
    """A connection that wrote a lot checkpoints the WAL when it is given back, outside the lock every open and close
    takes; then it goes back to the pool, not closed (closing the last connection would checkpoint again, under that lock)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        with connect(self.path, create=True) as conn:
            conn.execute("CREATE TABLE t (n INTEGER)")

    def tearDown(self):
        close_idle()
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

    def test_a_large_write_checkpoints_outside_the_lock_and_stays_open(self):
        self.assertEqual(self.write(CHECKPOINT_AFTER_CHANGES + 10), [("checkpoint", False)])

    def test_a_small_write_neither_checkpoints_nor_closes(self):
        self.assertEqual(self.write(10), [])

    def test_only_the_changes_since_the_checkout_count(self):
        self.write(CHECKPOINT_AFTER_CHANGES + 10)
        self.assertEqual(self.write(10), [])  # the same pooled connection, which has many changes in total

    def test_the_data_is_there(self):
        self.write(CHECKPOINT_AFTER_CHANGES + 10)
        with connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], CHECKPOINT_AFTER_CHANGES + 10)


class PoolTests(unittest.TestCase):
    """Connections are given back to a pool and reused, but never for another file than they were opened on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.make(self.dir / "a" / "t.db", 1)

    def tearDown(self):
        close_idle()
        self.tmp.cleanup()

    @staticmethod
    def make(path: Path, value: int) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with connect(path, create=True) as conn:
            conn.execute("CREATE TABLE t (n INTEGER)")
            conn.execute("INSERT INTO t VALUES (?)", (value,))
        return path

    @staticmethod
    def value(path: Path) -> int:
        with connect(path) as conn:
            return conn.execute("SELECT n FROM t").fetchone()[0]

    def test_a_connection_is_reused(self):
        with connect(self.path) as first:
            pass
        with connect(self.path) as second:
            self.assertIs(second, first)

    def test_two_at_once_are_different_connections(self):
        with connect(self.path) as first, connect(self.path) as second:
            self.assertIsNot(first, second)

    def test_a_replaced_database_is_not_read_through_an_old_connection(self):
        self.assertEqual(self.value(self.path), 1)
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        self.make(self.path, 2)
        self.assertEqual(self.value(self.path), 2)

    def test_close_idle_closes_the_connections_under_a_folder(self):
        other = self.make(self.dir / "b" / "t.db", 3)
        with connect(self.path) as a:
            pass
        with connect(other) as b:
            pass
        close_idle(self.dir / "a")
        with connect(self.path) as again_a, connect(other) as again_b:
            self.assertIsNot(again_a, a)
            self.assertIs(again_b, b)

    def test_a_connection_comes_back_without_a_transaction(self):
        with connect(self.path) as conn:  # as plain sqlite3: the end of the block commits
            conn.execute("BEGIN")
            conn.execute("INSERT INTO t VALUES (9)")
        with self.assertRaises(RuntimeError):
            with connect(self.path) as conn:  # an error rolls back
                conn.execute("BEGIN")
                conn.execute("INSERT INTO t VALUES (10)")
                raise RuntimeError
        with connect(self.path) as conn:
            self.assertFalse(conn.in_transaction)
            self.assertEqual(sorted(r[0] for r in conn.execute("SELECT n FROM t")), [1, 9])

    def test_the_pool_is_bounded(self):
        paths = [self.make(self.dir / f"p{i}" / "t.db", i) for i in range(util.MAX_IDLE_TOTAL + 10)]
        for p in paths:
            self.value(p)
        self.assertLessEqual(sum(len(c) for c in util._idle.values()), util.MAX_IDLE_TOTAL)


if __name__ == "__main__":
    unittest.main()
