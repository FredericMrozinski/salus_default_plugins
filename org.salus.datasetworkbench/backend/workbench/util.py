from __future__ import annotations

import hashlib
import os
import re
from collections import OrderedDict
import sqlite3
import threading
import time
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path


class WorkbenchError(Exception):
    """An error with an HTTP-like status that is safe to show to the user."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class NotFound(WorkbenchError):
    def __init__(self, message: str):
        super().__init__(message, 404)


RESERVED_SOURCE_FILES = {"source.sqlite", "source.sqlite-wal", "source.sqlite-shm", "source.sqlite-journal"}
PART_SUFFIX = ".part"
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .()+-]{0,99}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def check_name(name, what: str = "name") -> str:
    """A name for a folder (folder, source cohort, dataset). Plain characters only, so it is a safe path segment."""
    if not isinstance(name, str) or not _NAME.match(name.strip()) or name.strip().endswith("."):
        raise WorkbenchError(f"Invalid {what}: use letters, digits, spaces and _ . ( ) + - (at most 100 characters).")
    return name.strip()


def check_identifier(name, what: str = "name") -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise WorkbenchError(f"Invalid {what} '{name}': use letters, digits and underscores, starting with a letter.")
    return name


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "dataset"


def check_file_name(name) -> str:
    """A single file or folder name inside a source."""
    if not isinstance(name, str):
        raise WorkbenchError("Invalid name.")
    name = name.strip()
    if (not name or len(name) > 200 or name in (".", "..") or any(c in name for c in "/\\\0") or name.startswith(".")
            or name in RESERVED_SOURCE_FILES or name.endswith(PART_SUFFIX)):
        raise WorkbenchError("Invalid name: no slashes, no leading dot, at most 200 characters.")
    return name


def rename_on_disk(src: Path, dest: Path) -> None:
    """Renames a file or folder; refuses to replace anything (a case-only rename of the same item is fine)."""
    import os
    if dest.exists() and not (dest.samefile(src) and dest.name != src.name):
        raise WorkbenchError(f"'{dest.name}' already exists here.", 409)
    os.rename(src, dest)


def safe_rel_path(rel: str, allow_empty: bool = False) -> str:
    """Normalizes a relative path with `/` separators and rejects anything that could leave its base."""
    if not isinstance(rel, str):
        raise WorkbenchError("Invalid path.")
    rel = rel.replace("\\", "/").strip("/")
    if not rel:
        if allow_empty:
            return ""
        raise WorkbenchError("Empty path.")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", "..") or "\0" in part or part.startswith("/") or (len(part) == 2 and part[1] == ":"):
            raise WorkbenchError(f"Invalid path '{rel}'.")
    return "/".join(parts)


def resolve_inside(base: Path, rel: str, allow_empty: bool = True) -> Path:
    rel = safe_rel_path(rel, allow_empty=allow_empty)
    path = (base / rel) if rel else base
    base_real = base.resolve()
    real = path.resolve()
    if real != base_real and base_real not in real.parents:
        raise WorkbenchError(f"Path '{rel}' leaves its folder.")
    return path


# Opening and closing SQLite connections from many threads at once can hang inside SQLite (seen with the Python/SQLite
# builds of conda: several threads stuck in connect/close, none holding a transaction). So only one thread at a time
# opens or closes a connection; the queries themselves still run in parallel.
#
# That is only quick if closing is quick. Closing the last connection to a database in WAL mode checkpoints the WAL into
# the database file, and after a large write (a scan that recorded many files, an automation that created many entries)
# that is a lot of writing, slow on a network filesystem; done under this lock it held up every request of the backend
# (504s from Salus long after a Refresh). So a connection that changed many rows checkpoints first, outside the lock
# (PASSIVE: never waits for readers), and the close under the lock has next to nothing left to do.
_open_close = threading.Lock()
CHECKPOINT_AFTER_CHANGES = 1000
SLOW_SECONDS = 2.0
log = logging.getLogger("workbench.db")

# Connections are kept open after use and handed out again (a small pool per database file). Every request used to open
# and close several connections, one at a time under `_open_close`; and closing the last connection to a database makes
# SQLite checkpoint the WAL into the database file and delete the WAL, with fsyncs: on an HDD that is tens of ms or more
# per close, behind which every other request of the backend waited. A database with an idle connection is never "last
# closed", so that cost is gone; the WAL is still checkpointed as it grows (SQLite's automatic checkpoint at commit, in the
# writing thread). A pooled connection belongs to the file it was opened on (device and inode): one whose file was deleted
# or replaced is closed instead of reused, and catalog operations that delete or move databases close the idle ones under
# that folder first (close_idle).
MAX_IDLE_PER_DB = 2
MAX_IDLE_TOTAL = 64
_idle: "OrderedDict[str, list[Conn]]" = OrderedDict()  # least recently used first
_idle_guard = threading.Lock()


def _file_ident(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _close(conns) -> None:
    for conn in conns:
        started = time.monotonic()
        try:
            with _open_close:
                sqlite3.Connection.close(conn)
        except sqlite3.Error:
            pass
        took = time.monotonic() - started
        if took > SLOW_SECONDS:
            log.warning("closing a database took %.1f s", took)


class Conn(sqlite3.Connection):
    """`with connect(...) as conn:` gives the connection back at the end (to the pool, or closes it). The connection must
    not be used after the block: another thread may have it by then."""
    _key = ""
    _ident = None
    _changes_at_checkout = 0

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self._release()

    def _release(self) -> None:
        try:
            if self.in_transaction:
                self.execute("ROLLBACK")
            if self.total_changes - self._changes_at_checkout >= CHECKPOINT_AFTER_CHANGES:
                started = time.monotonic()
                try:
                    self.execute("PRAGMA wal_checkpoint(PASSIVE)")  # keeps the WAL small after a big write; never waits for readers
                except sqlite3.Error as e:
                    log.warning("checkpoint failed: %s", e)
                took = time.monotonic() - started
                if took > SLOW_SECONDS:
                    log.warning("checkpoint after %d changes took %.1f s", self.total_changes - self._changes_at_checkout, took)
        except sqlite3.ProgrammingError:
            return  # closed already
        except sqlite3.Error:
            _close([self])
            return
        if self._ident is None or _file_ident(self._key) != self._ident:
            _close([self])  # the file was deleted or replaced meanwhile
            return
        evicted = []
        with _idle_guard:
            conns = _idle.setdefault(self._key, [])
            _idle.move_to_end(self._key)
            if len(conns) < MAX_IDLE_PER_DB:
                conns.append(self)
            else:
                evicted.append(self)
            while sum(len(c) for c in _idle.values()) > MAX_IDLE_TOTAL:
                _, oldest = _idle.popitem(last=False)
                evicted += oldest
        _close(evicted)

    def close(self) -> None:
        _close([self])


def _take_idle(key: str, ident) -> "Conn | None":
    stale, found = [], None
    with _idle_guard:
        conns = _idle.get(key)
        while conns:
            conn = conns.pop()
            if conn._ident == ident:
                found = conn
                break
            stale.append(conn)
        if conns is not None and not conns:
            del _idle[key]
    _close(stale)
    return found


def close_idle(under: Path | None = None) -> None:
    """Closes the pooled connections to the databases in a folder (all of them without one): before that folder, or a
    database in it, is deleted, moved or renamed."""
    prefix = None if under is None else str(under).rstrip("/") + "/"
    gone = []
    with _idle_guard:
        for key in [k for k in _idle if prefix is None or k.startswith(prefix)]:
            gone += _idle.pop(key)
    _close(gone)


def connect(path: Path, create: bool = False) -> sqlite3.Connection:
    key = str(path)
    ident = _file_ident(key)
    if ident is None and not create:
        raise NotFound(f"Database {path} does not exist.")
    if ident is not None:
        conn = _take_idle(key, ident)
        if conn is not None:
            conn._changes_at_checkout = conn.total_changes
            return conn
    with _open_close:
        conn = sqlite3.connect(key, timeout=30, isolation_level=None, check_same_thread=False, factory=Conn)
    conn.row_factory = sqlite3.Row
    if create:
        conn.execute("PRAGMA journal_mode=WAL")  # persistent: stored in the database file
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # In WAL mode NORMAL still never corrupts the database; a power cut can only lose the last commits. It saves an fsync
    # per commit, which on a slow disk (a server's HDD) is what most writes wait for.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn._key, conn._ident = key, ident or _file_ident(key)
    conn._changes_at_checkout = conn.total_changes
    return conn


class Tx:
    """`with Tx(conn): ...` is one transaction (isolation_level=None means we manage it)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    """One writer lock per database file (the backend is a single process)."""
    key = str(Path(path).resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def chunks(items, size: int = 500):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def meta_get(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, None if value is None else str(value)))
