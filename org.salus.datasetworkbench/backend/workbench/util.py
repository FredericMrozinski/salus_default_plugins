from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
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
# opens or closes a connection; this takes microseconds, the queries themselves still run in parallel.
_open_close = threading.Lock()


class Conn(sqlite3.Connection):
    """`with connect(...) as conn:` closes the connection at the end (plain sqlite3 only commits)."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            with _open_close:
                self.close()


def connect(path: Path, create: bool = False) -> sqlite3.Connection:
    if not create and not Path(path).exists():
        raise NotFound(f"Database {path} does not exist.")
    with _open_close:
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False, factory=Conn)
    conn.row_factory = sqlite3.Row
    if create:
        conn.execute("PRAGMA journal_mode=WAL")  # persistent: stored in the database file
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
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
