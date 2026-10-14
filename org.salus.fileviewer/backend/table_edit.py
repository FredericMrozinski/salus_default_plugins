"""Table editor: CSV/TSV files are edited as raw text in a working copy and written back atomically.

Nothing is locked. The working copy is private; a save checks that the file is still the one that was loaded (size and
modification time), and if it is not, the caller decides: save somewhere else, overwrite anyway, or discard and reload.

The values stay exactly as text, so `007` stays `007`. The delimiter, the quote character, the line endings, the byte order
mark and a missing final newline of the original are kept when it is written back. A field that holds the delimiter, a quote
or a line break is quoted by the csv writer, so the file stays a valid table whatever the user types.
"""
from __future__ import annotations

import codecs
import csv
import io
import os
import shutil
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path

from errors import ViewerError

csv.field_size_limit(64 * 1024 * 1024)  # cells can be long; the default limit is 128 KB

EDITABLE = {".csv": ",", ".tsv": "\t"}
MAX_EDIT_BYTES = 300 * 1024 * 1024
BATCH = 5000
_open_close = threading.Lock()  # opening and closing SQLite connections at once can hang inside SQLite (see workbench/util.py)


def fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class EditSession:
    """One file being edited by one viewer."""

    def __init__(self, path: Path, owner: int):
        self.id = uuid.uuid4().hex[:12]
        self.path = path
        self.owner = owner
        self.state = "loading"  # loading | ready | error
        self.error: str | None = None
        self.progress = 0
        self.modified = 0  # number of edits since loading or the last save
        self.columns: list[dict] = []  # [{id, name}] in file order; the cell of column k is the physical column c<k>
        self.rows = 0
        self.fingerprint: tuple[int, int] | None = None
        self._next_column = 1
        self._lock = threading.RLock()
        self._dir = tempfile.mkdtemp(prefix="fileviewer-edit-")
        self._db: sqlite3.Connection | None = None
        # the format of the original, written back the same way
        self.delimiter = EDITABLE[path.suffix.lower()]
        self.quotechar = '"'
        self.newline = "\n"
        self.bom = False
        self.final_newline = True
        self.skipped_blank = 0

    # ---- loading
    def load(self) -> None:
        try:
            with self._lock:
                self._load()
            self.state = "ready"
        except ViewerError as e:
            self.state, self.error = "error", str(e)
        except (OSError, csv.Error, sqlite3.Error, UnicodeError) as e:
            self.state, self.error = "error", f"{type(e).__name__}: {e}"

    def _load(self) -> None:
        path = self.path
        before = fingerprint(path)
        if before is None:
            raise ViewerError("The file does not exist on the server.", 404)
        if before[0] > MAX_EDIT_BYTES:
            raise ViewerError(f"This file is too large to edit ({before[0] // (1024 * 1024)} MB; the limit is {MAX_EDIT_BYTES // (1024 * 1024)} MB).", 413)
        raw = path.read_bytes()
        self.bom = raw.startswith(codecs.BOM_UTF8)
        try:
            text = raw.decode("utf-8-sig" if self.bom else "utf-8")
        except UnicodeDecodeError:
            raise ViewerError("This file is not UTF-8 text, so it cannot be edited here (it can still be viewed).", 415) from None
        del raw
        self.newline = "\r\n" if "\r\n" in text[:65536] else "\r" if "\r" in text[:65536] and "\n" not in text[:65536] else "\n"
        self.final_newline = text.endswith(("\n", "\r")) or not text
        self.delimiter = self._detect_delimiter(text[:65536])

        if self._db is not None:
            with _open_close:
                self._db.close()
        db_path = os.path.join(self._dir, "work.sqlite")
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass
        with _open_close:
            self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=OFF")
        self._db.execute("PRAGMA synchronous=OFF")

        reader = csv.reader(io.StringIO(text, newline=""), self._dialect())
        header = next(reader, [])
        self.columns, self._next_column = [], 1
        for name in header:
            self._new_column(name)
        self._create_table()
        batch: list[list[str]] = []
        count = 0
        self.skipped_blank = 0

        def flush():
            if batch:
                marks = ",".join("?" * (len(self.columns) + 1))
                self._db.executemany(f"INSERT INTO rows(_ord, {self._column_list()}) VALUES({marks})" if self.columns
                                     else "INSERT INTO rows(_ord) VALUES(?)", batch)
                batch.clear()

        self._db.execute("BEGIN")
        for row in reader:
            if not row:  # a blank line
                self.skipped_blank += 1
                continue
            if len(row) > len(self.columns):  # a row wider than the header: the extra values get unnamed columns
                flush()
                for _ in range(len(row) - len(self.columns)):
                    self._add_physical_column(self._new_column(""))
            count += 1
            batch.append([float(count), *row, *[""] * (len(self.columns) - len(row))])
            if len(batch) >= BATCH:
                flush()
                self.progress = count
        flush()
        self._db.execute("COMMIT")
        self.rows = count
        self.modified = 0
        self.fingerprint = before

    def _detect_delimiter(self, sample: str) -> str:
        default = EDITABLE[self.path.suffix.lower()]
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter if sample.strip() else default
        except csv.Error:
            return default

    def _dialect(self):
        delimiter, quotechar, newline = self.delimiter, self.quotechar, self.newline

        class Dialect(csv.Dialect):
            pass

        Dialect.delimiter, Dialect.quotechar, Dialect.doublequote = delimiter, quotechar, True
        Dialect.skipinitialspace, Dialect.lineterminator, Dialect.quoting = False, newline, csv.QUOTE_MINIMAL
        return Dialect

    # ---- the working copy
    def _new_column(self, name: str) -> dict:
        column = {"id": self._next_column, "name": name}
        self._next_column += 1
        self.columns.append(column)
        return column

    def _column_list(self) -> str:
        return ", ".join(f"c{c['id']}" for c in self.columns)

    def _create_table(self) -> None:
        cells = "".join(f", c{c['id']} TEXT NOT NULL DEFAULT ''" for c in self.columns)
        self._db.execute(f"CREATE TABLE rows (_id INTEGER PRIMARY KEY, _ord REAL NOT NULL{cells})")
        self._db.execute("CREATE INDEX rows_ord ON rows(_ord)")

    def _add_physical_column(self, column: dict) -> None:
        self._db.execute(f"ALTER TABLE rows ADD COLUMN c{column['id']} TEXT NOT NULL DEFAULT ''")

    def _ready(self) -> None:
        if self.state != "ready":
            raise ViewerError(self.error or "The file is still loading.", 409)

    def _column(self, column_id) -> dict:
        for c in self.columns:
            if c["id"] == column_id:
                return c
        raise ViewerError("No such column.", 404)

    def info(self) -> dict:
        return {"id": self.id, "file": str(self.path), "name": self.path.name, "state": self.state, "error": self.error,
                "progress": self.progress, "columns": self.columns, "rows": self.rows, "modified": self.modified,
                "skipped_blank": self.skipped_blank}

    # ---- reading
    def page(self, limit: int, offset: int) -> dict:
        with self._lock:
            self._ready()
            if not self.columns:
                return {"total": self.rows, "offset": offset, "rows": []}
            cur = self._db.execute(f"SELECT _id, {self._column_list()} FROM rows ORDER BY _ord LIMIT ? OFFSET ?", (limit, offset))
            return {"total": self.rows, "offset": offset, "rows": [list(r) for r in cur.fetchall()]}

    # ---- editing (each call changes the working copy only)
    def set_cells(self, edits: list) -> None:
        with self._lock:
            self._ready()
            if not isinstance(edits, list):
                raise ViewerError("Invalid edits.")
            self._db.execute("BEGIN")
            try:
                for edit in edits:
                    column = self._column(edit.get("col"))
                    value = edit.get("value")
                    if not isinstance(value, str):
                        raise ViewerError("A cell holds text.")
                    done = self._db.execute(f"UPDATE rows SET c{column['id']}=? WHERE _id=?", (value, edit.get("row")))
                    if done.rowcount == 0:
                        raise ViewerError("No such row.", 404)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self.modified += len(edits)

    def insert_rows(self, where, count: int = 1) -> list[int]:
        """`where`: "end", "start", {"after": row id} or {"before": row id}. Returns the new row ids."""
        with self._lock:
            self._ready()
            if not self.columns:
                raise ViewerError("Add a column first.")
            count = max(1, min(int(count), 10000))
            low, high = self._gap(where)
            ids = []
            for i in range(count):
                order = low + (high - low) * (i + 1) / (count + 1)
                cur = self._db.execute("INSERT INTO rows(_ord) VALUES(?)", (order,))
                ids.append(cur.lastrowid)
            if (high - low) / (count + 1) < 1e-7:  # the gaps got tiny: spread the orders out again
                self._db.execute("WITH n AS (SELECT _id AS i, ROW_NUMBER() OVER (ORDER BY _ord) AS k FROM rows) "
                                 "UPDATE rows SET _ord = (SELECT k FROM n WHERE n.i = rows._id)")
            self.rows += count
            self.modified += count
            return ids

    def _gap(self, where) -> tuple[float, float]:
        def order_of(row_id):
            r = self._db.execute("SELECT _ord FROM rows WHERE _id=?", (row_id,)).fetchone()
            if r is None:
                raise ViewerError("No such row.", 404)
            return r[0]

        top = self._db.execute("SELECT MIN(_ord), MAX(_ord) FROM rows").fetchone()
        smallest, largest = (top[0], top[1]) if top[0] is not None else (1.0, 0.0)
        if where == "end" or where is None:
            return largest, largest + 1.0 if top[0] is not None else 1.0
        if where == "start":
            return smallest - 1.0, smallest
        if isinstance(where, dict) and "after" in where:
            here = order_of(where["after"])
            nxt = self._db.execute("SELECT MIN(_ord) FROM rows WHERE _ord > ?", (here,)).fetchone()[0]
            return here, nxt if nxt is not None else here + 1.0
        if isinstance(where, dict) and "before" in where:
            here = order_of(where["before"])
            prev = self._db.execute("SELECT MAX(_ord) FROM rows WHERE _ord < ?", (here,)).fetchone()[0]
            return prev if prev is not None else here - 1.0, here
        raise ViewerError("Invalid position.")

    def delete_rows(self, ids: list) -> int:
        with self._lock:
            self._ready()
            if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
                raise ViewerError("Invalid rows.")
            removed = 0
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                removed += self._db.execute(f"DELETE FROM rows WHERE _id IN ({','.join('?' * len(chunk))})", chunk).rowcount
            self.rows -= removed
            self.modified += removed
            return removed

    def add_column(self, name: str, after=None) -> dict:
        """A new empty column; at the end, or right after the column `after`."""
        with self._lock:
            self._ready()
            name = self._check_name(name)
            column = {"id": self._next_column, "name": name}
            self._next_column += 1
            position = len(self.columns)
            if after is not None:
                position = self.columns.index(self._column(after)) + 1
            self._add_physical_column(column)
            self.columns.insert(position, column)
            self.modified += 1
            return column

    def rename_column(self, column_id, name: str) -> None:
        with self._lock:
            self._ready()
            self._column(column_id)["name"] = self._check_name(name)
            self.modified += 1

    def delete_column(self, column_id) -> None:
        with self._lock:
            self._ready()
            column = self._column(column_id)
            self._db.execute(f"ALTER TABLE rows DROP COLUMN c{column['id']}")
            self.columns.remove(column)
            self.modified += 1

    @staticmethod
    def _check_name(name) -> str:
        if not isinstance(name, str) or "\n" in name or "\r" in name:
            raise ViewerError("A column name is one line of text.")
        return name

    # ---- saving
    def path_info(self, target: str) -> dict:
        """What the viewer should tell the user before saving to `target`."""
        path = self._target(target)
        folder = path.parent
        others = [p.name for p in folder.glob("*.csv") if p != path and not p.name.startswith(".")] if folder.is_dir() else []
        kind = self._source_kind(folder)
        # Only a tabular source cohort is spoiled by a second CSV; in a general one it is just another file.
        return {"path": str(path), "exists": path.exists(), "source_kind": kind, "folder_is_source": kind == "table",
                "csv_in_folder": others[:5]}

    @staticmethod
    def _source_kind(folder: Path) -> str | None:
        """'file' or 'table' if the folder is a source cohort of the Dataset Workbench, else None."""
        db = folder / "source.sqlite"
        if not db.exists():
            return None
        try:
            with _open_close:
                conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key='type'").fetchone()
            finally:
                with _open_close:
                    conn.close()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def _target(self, target: str) -> Path:
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = self.path.parent / path
        if path.suffix.lower() not in EDITABLE:
            raise ViewerError("Save as a .csv or .tsv file.")
        if path.name.startswith(".") or not path.name:
            raise ViewerError("That name is not allowed.")
        if not path.parent.is_dir():
            raise ViewerError("That folder does not exist on the server.")
        if path.is_dir():
            raise ViewerError("That is a folder.")
        return path

    def save(self, target: str | None = None, overwrite: bool = False) -> dict:
        """Writes the working copy to the file. Returns {"conflict": "changed" | "deleted"} instead of writing if the file was
        changed since it was loaded (unless `overwrite`). With `target`, writes a new file there (never replacing one)."""
        with self._lock:
            self._ready()
            if target is None:
                path = self.path
                now = fingerprint(path)
                if not overwrite and now != self.fingerprint:
                    return {"conflict": "deleted" if now is None else "changed"}
            else:
                path = self._target(target)
                if path.exists() and path != self.path:
                    raise ViewerError(f"{path.name} already exists there.", 409)
                if path == self.path and not overwrite and fingerprint(path) != self.fingerprint:
                    return {"conflict": "changed"}
            self._write(path)
            self.path = path
            self.fingerprint = fingerprint(path)
            self.modified = 0
            return {"saved": str(path), "name": path.name, "rows": self.rows, "columns": len(self.columns)}

    def _write(self, path: Path) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(temp, "w", encoding="utf-8-sig" if self.bom else "utf-8", newline="") as handle:
                if self.columns:
                    writer = csv.writer(handle, self._dialect())
                    writer.writerow([c["name"] for c in self.columns])
                    cursor = self._db.execute(f"SELECT {self._column_list()} FROM rows ORDER BY _ord")
                    while True:
                        chunk = cursor.fetchmany(BATCH)
                        if not chunk:
                            break
                        writer.writerows(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if not self.final_newline and self.columns:  # the original did not end with a line break
                with open(temp, "r+b") as handle:
                    handle.seek(0, os.SEEK_END)
                    handle.truncate(max(0, handle.tell() - len(self.newline)))
            if path.exists():
                shutil.copymode(path, temp)
            os.replace(temp, path)  # atomic: readers see the old file or the new one, never a half-written one
        finally:
            if temp.exists():
                temp.unlink()

    def discard(self) -> None:
        """Throws the working copy away and loads the file again."""
        with self._lock:
            self.state = "loading"
            try:
                self._load()
                self.state = "ready"
            except ViewerError as e:
                self.state, self.error = "error", str(e)
                raise

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                with _open_close:
                    self._db.close()
                self._db = None
        shutil.rmtree(self._dir, ignore_errors=True)


class Edits:
    """The open edit sessions."""

    def __init__(self):
        self._sessions: dict[str, EditSession] = {}
        self._lock = threading.Lock()

    def start(self, file: str, owner: int) -> EditSession:
        path = Path(file).expanduser()
        if path.suffix.lower() not in EDITABLE:
            raise ViewerError("Only .csv and .tsv files can be edited.", 415)
        if not path.is_file():
            raise ViewerError("That file does not exist on the server.", 404)
        session = EditSession(path, owner)
        with self._lock:
            self._sessions[session.id] = session
        threading.Thread(target=session.load, daemon=True, name=f"edit-load-{path.name}").start()
        return session

    def get(self, session_id: str) -> EditSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ViewerError("This editing session is over; reload the viewer.", 404)
        return session

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            session.close()

    def close_owned(self, owner: int) -> None:
        """The viewer was closed: its working copies go away (unsaved edits are lost)."""
        with self._lock:
            mine = [s for s in self._sessions.values() if s.owner == owner]
            for s in mine:
                del self._sessions[s.id]
        for s in mine:
            s.close()
