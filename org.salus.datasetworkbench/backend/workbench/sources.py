"""Sources: file sources (items are files) and table sources (items are the rows of one CSV)."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .util import (PART_SUFFIX, RESERVED_SOURCE_FILES, NotFound, Tx, WorkbenchError, check_file_name, chunks, connect,
                   like_escape, lock_for, meta_get, meta_set, new_id, now_iso, rename_on_disk, safe_rel_path, sha1_text)

SOURCE_DB = "source.sqlite"
SCHEMA_VERSION = "1"
Progress = Callable[[dict], None]

COLUMN_TYPES = ("text", "integer", "float", "boolean", "date", "datetime")
_TRUE = {"true", "t", "yes", "y"}
_FALSE = {"false", "f", "no", "n"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RESERVED_COLUMNS = {"_key", "_row", "_hash"}
MAX_STORED_ISSUES = 5000


def file_fingerprint(st: os.stat_result) -> str:
    return f"{st.st_size}:{st.st_mtime_ns}"


def hidden(name: str) -> bool:
    return name.startswith(".")


def is_internal(name: str) -> bool:
    return name in RESERVED_SOURCE_FILES or name.endswith(PART_SUFFIX)


# --------------------------------------------------------------------------- value conversion

def convert(kind: str, text: str):
    """Converts CSV text of a non-empty cell to its typed value; raises ValueError with a readable message."""
    text = text.strip()
    if kind == "text":
        return text
    if kind == "integer":
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"'{text}' is not an integer") from None
    if kind == "float":
        try:
            value = float(text)
        except ValueError:
            raise ValueError(f"'{text}' is not a number") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"'{text}' is not a finite number")
        return value
    if kind == "boolean":
        lowered = text.lower()
        if lowered in _TRUE or lowered == "1":
            return 1
        if lowered in _FALSE or lowered == "0":
            return 0
        raise ValueError(f"'{text}' is not a boolean")
    if kind == "date":
        if not _DATE.match(text):
            raise ValueError(f"'{text}' is not a date (YYYY-MM-DD)")
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            raise ValueError(f"'{text}' is not a valid date") from None
    if kind == "datetime":
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(sep="T")
        except ValueError:
            raise ValueError(f"'{text}' is not a datetime (ISO 8601)") from None
    raise ValueError(f"unknown type {kind}")


def infer_type(values: list[str]) -> str:
    values = [v.strip() for v in values if v.strip()]
    if not values:
        return "text"
    for kind in ("integer", "float", "date", "datetime"):
        if all(_accepts(kind, v) for v in values):
            return kind
    if all(v.lower() in _TRUE | _FALSE for v in values):
        return "boolean"
    return "text"


def _accepts(kind: str, value: str) -> bool:
    try:
        convert(kind, value)
        return True
    except ValueError:
        return False


SQL_TYPES = {"text": "TEXT", "integer": "INTEGER", "float": "REAL", "boolean": "INTEGER", "date": "TEXT", "datetime": "TEXT"}


def q(identifier: str) -> str:
    """Quotes an SQL identifier."""
    return '"' + identifier.replace('"', '""') + '"'


# --------------------------------------------------------------------------- base

class Source:
    kind = ""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.db_path = self.path / SOURCE_DB

    # -- database
    def connect(self):
        return connect(self.db_path)

    @property
    def lock(self):
        return lock_for(self.db_path)

    @property
    def id(self) -> str:
        with self.connect() as conn:
            return meta_get(conn, "source_id")

    @property
    def name(self) -> str:
        with self.connect() as conn:
            return meta_get(conn, "name", self.path.name)

    def meta(self) -> dict:
        with self.connect() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}

    @staticmethod
    def create(path: Path, name: str, kind: str) -> "Source":
        path.mkdir(parents=True, exist_ok=True)
        db = path / SOURCE_DB
        if db.exists():
            raise WorkbenchError("This folder already is a source.")
        with connect(db, create=True) as conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            for k, v in {"source_id": new_id(), "name": name, "type": kind, "created_at": now_iso(),
                         "schema_version": SCHEMA_VERSION}.items():
                meta_set(conn, k, v)
            if kind == "file":
                conn.execute("""CREATE TABLE items (
                    id TEXT PRIMARY KEY, rel_path TEXT UNIQUE NOT NULL, ext TEXT, size INTEGER, mtime_ns INTEGER,
                    hash TEXT, metadata TEXT, status TEXT NOT NULL DEFAULT 'ok', first_seen TEXT, last_seen TEXT)""")
                conn.execute("CREATE INDEX items_fp ON items(size, mtime_ns)")
            else:
                conn.execute("""CREATE TABLE columns (position INTEGER, name TEXT PRIMARY KEY, type TEXT NOT NULL,
                    is_key INTEGER NOT NULL DEFAULT 0)""")
                conn.execute("CREATE TABLE row_issues (row_number INTEGER, key TEXT, column_name TEXT, code TEXT, message TEXT)")
                conn.execute("CREATE TABLE ingest (csv_name TEXT, size INTEGER, mtime_ns INTEGER, ingested_at TEXT)")
        return open_source(path)

    # -- subclass API
    def info(self) -> dict:
        raise NotImplementedError

    def fingerprints(self, item_ids, columns=None) -> dict:
        """{item id: (fingerprint, label)} for the items that currently exist."""
        raise NotImplementedError

    def search(self, q: str, limit: int, offset: int, **options) -> list[dict]:
        raise NotImplementedError

    def describe(self, item_id: str, columns=None) -> dict | None:
        raise NotImplementedError

    def refresh(self, progress: Progress | None = None) -> bool:
        """Brings the metadata up to date if that is cheap. Returns True if something changed."""
        return False

    def find(self, q: str, limit: int) -> list[dict]:
        return []

    def item_ids_under(self, rel_path: str | None) -> set[str] | None:
        """Ids of the items at or below a path (None: no item-level information, the whole source is meant)."""
        return None

    def rename_item(self, rel_path: str, new_name: str) -> str:
        raise WorkbenchError("Items of this source cannot be renamed.")

    def delete_item(self, rel_path: str, referenced: set[str]) -> None:
        raise WorkbenchError("Items of this source cannot be deleted.")


def open_source(path: Path) -> Source:
    db = Path(path) / SOURCE_DB
    if not db.exists():
        raise NotFound(f"{path} is not a source.")
    with connect(db) as conn:
        kind = meta_get(conn, "type")
    if kind == "file":
        return FileSource(path)
    if kind == "table":
        return TableSource(path)
    raise WorkbenchError(f"Unknown source type '{kind}'.", 500)


# --------------------------------------------------------------------------- file source

class FileSource(Source):
    kind = "file"

    def info(self) -> dict:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM items WHERE status!='missing'").fetchone()[0]
            missing = conn.execute("SELECT COUNT(*) FROM items WHERE status='missing'").fetchone()[0]
            meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        return {"id": meta["source_id"], "name": meta["name"], "kind": "file", "items": total, "missing": missing,
                "last_scan": meta.get("last_scan"), "state": "ready"}

    def _walk(self, progress: Progress | None):
        stack = [""]
        count = 0
        while stack:
            rel_dir = stack.pop()
            directory = self.path / rel_dir if rel_dir else self.path
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue  # not accessible: its files simply are not seen
            for entry in entries:
                if hidden(entry.name) or is_internal(entry.name):
                    continue
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(rel)
                    elif entry.is_file():
                        st = entry.stat()
                        count += 1
                        if progress and count % 500 == 0:
                            progress({"phase": "scan", "files": count})
                        yield rel, st
                except OSError:
                    continue

    def scan(self, progress: Progress | None = None) -> dict:
        """Matches the files on disk to the items. Moved files keep their id (same size and mtime)."""
        with self.lock:
            seen = {rel: st for rel, st in self._walk(progress)}
            now = now_iso()
            with self.connect() as conn:
                existing = {r["rel_path"]: r for r in conn.execute(
                    "SELECT id, rel_path, size, mtime_ns, status FROM items")}
                new_paths = [p for p in seen if p not in existing]
                orphans = [r for p, r in existing.items() if p not in seen]
                pool: dict[tuple, list] = {}
                for r in orphans:
                    pool.setdefault((r["size"], r["mtime_ns"]), []).append(r)
                moved, created = [], []
                for rel in sorted(new_paths):
                    st = seen[rel]
                    candidates = pool.get((st.st_size, st.st_mtime_ns))
                    match = None
                    if candidates:
                        name = rel.rsplit("/", 1)[-1]
                        match = next((c for c in candidates if c["rel_path"].rsplit("/", 1)[-1] == name), candidates[0])
                        candidates.remove(match)
                    if match:
                        moved.append((rel, match["id"]))
                    else:
                        created.append(rel)
                moved_ids = {item_id for _, item_id in moved}
                vanished = [r["id"] for r in orphans if r["id"] not in moved_ids and r["status"] != "missing"]
                with Tx(conn):
                    for rel, item_id in moved:
                        st = seen[rel]
                        conn.execute("UPDATE items SET rel_path=?, ext=?, size=?, mtime_ns=?, status='ok', last_seen=? WHERE id=?",
                                     (rel, ext_of(rel), st.st_size, st.st_mtime_ns, now, item_id))
                    conn.executemany(
                        "INSERT INTO items(id, rel_path, ext, size, mtime_ns, status, first_seen, last_seen) VALUES(?,?,?,?,?,'ok',?,?)",
                        [(new_id(), rel, ext_of(rel), seen[rel].st_size, seen[rel].st_mtime_ns, now, now) for rel in created])
                    conn.executemany(
                        "UPDATE items SET ext=?, size=?, mtime_ns=?, status='ok', last_seen=? WHERE rel_path=?",
                        [(ext_of(rel), seen[rel].st_size, seen[rel].st_mtime_ns, now, rel) for rel in seen if rel in existing])
                    for ids in chunks(vanished):
                        conn.execute(f"UPDATE items SET status='missing' WHERE id IN ({','.join('?' * len(ids))})", ids)
                    meta_set(conn, "last_scan", now)
            return {"files": len(seen), "new": len(created), "moved": len(moved), "missing": len(vanished)}

    def register(self, rel_path: str, file_hash: str | None = None) -> dict:
        """Records one file (after an upload) as an item; an existing item keeps its id."""
        rel_path = safe_rel_path(rel_path)
        st = (self.path / rel_path).stat()
        now = now_iso()
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT id FROM items WHERE rel_path=?", (rel_path,)).fetchone()
            if row:
                conn.execute("UPDATE items SET ext=?, size=?, mtime_ns=?, hash=?, status='ok', last_seen=? WHERE id=?",
                             (ext_of(rel_path), st.st_size, st.st_mtime_ns, file_hash, now, row["id"]))
                item_id = row["id"]
            else:
                item_id = new_id()
                conn.execute("INSERT INTO items(id, rel_path, ext, size, mtime_ns, hash, status, first_seen, last_seen)"
                             " VALUES(?,?,?,?,?,?,'ok',?,?)",
                             (item_id, rel_path, ext_of(rel_path), st.st_size, st.st_mtime_ns, file_hash, now, now))
        return self.item(item_id)

    def _row_to_item(self, r) -> dict:
        return {"id": r["id"], "path": r["rel_path"], "name": r["rel_path"].rsplit("/", 1)[-1], "ext": r["ext"],
                "size": r["size"], "status": r["status"], "abs_path": str(self.path / r["rel_path"])}

    def item(self, item_id: str) -> dict:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not r:
            raise NotFound("No such item.")
        return self._row_to_item(r)

    def list_dir(self, directory: str, q: str, limit: int, offset: int) -> dict:
        directory = safe_rel_path(directory, allow_empty=True)
        with self.connect() as conn:
            if q.strip():
                rows = conn.execute("SELECT * FROM items WHERE rel_path LIKE ? ESCAPE '\\' ORDER BY rel_path LIMIT ? OFFSET ?",
                                    (f"%{like_escape(q.strip())}%", limit, offset)).fetchall()
                return {"dir": directory, "folders": [], "files": [self._row_to_item(r) for r in rows],
                        "more": len(rows) == limit}
            prefix = directory + "/" if directory else ""
            start = len(prefix) + 1
            bounds, args = "", []
            if prefix:
                bounds, args = "rel_path >= ? AND rel_path < ? AND", [prefix, prefix[:-1] + "0"]
            folders = conn.execute(
                f"SELECT substr(rel_path, {start}, instr(substr(rel_path, {start}), '/') - 1) AS name, COUNT(*) AS n,"
                f" SUM(status='missing') AS missing FROM items WHERE {bounds} instr(substr(rel_path, {start}), '/') > 0"
                " GROUP BY name ORDER BY name", args).fetchall()
            files = conn.execute(
                f"SELECT * FROM items WHERE {bounds} instr(substr(rel_path, {start}), '/') = 0"
                " ORDER BY rel_path LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return {"dir": directory,
                "folders": [{"name": f["name"], "path": prefix + f["name"], "count": f["n"], "missing": f["missing"] or 0}
                            for f in folders] if offset == 0 else [],
                "files": [self._row_to_item(r) for r in files], "more": len(files) == limit}

    def fingerprints(self, item_ids, columns=None) -> dict:
        found = {}
        with self.connect() as conn:
            for ids in chunks(item_ids):
                rows = conn.execute(f"SELECT id, rel_path FROM items WHERE id IN ({','.join('?' * len(ids))})", ids)
                for r in rows:
                    try:
                        st = (self.path / r["rel_path"]).stat()
                    except OSError:
                        continue
                    found[r["id"]] = (file_fingerprint(st), r["rel_path"])
        return found

    def describe(self, item_id: str, columns=None) -> dict | None:
        try:
            item = self.item(item_id)
        except NotFound:
            return None
        return {"label": item["path"], "path": item["abs_path"]}

    def find(self, q: str, limit: int) -> list[dict]:
        """Files whose path contains every word of `q` (the global search)."""
        words = q.split()
        if not words:
            return []
        clause = " AND ".join("rel_path LIKE ? ESCAPE '\\'" for _ in words)
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM items WHERE status!='missing' AND {clause} ORDER BY rel_path LIMIT ?",
                                [f"%{like_escape(w)}%" for w in words] + [limit]).fetchall()
        return [self._row_to_item(r) for r in rows]

    def search(self, q: str, limit: int, offset: int, extensions=None, **options) -> list[dict]:
        sql, args = "SELECT * FROM items WHERE status!='missing'", []
        if q.strip():
            sql += " AND rel_path LIKE ? ESCAPE '\\'"
            args.append(f"%{like_escape(q.strip())}%")
        if extensions:
            sql += f" AND ext IN ({','.join('?' * len(extensions))})"
            args += [e.lower() for e in extensions]
        sql += " ORDER BY rel_path LIMIT ? OFFSET ?"
        with self.connect() as conn:
            return [{"item": r["id"], "label": r["rel_path"], "detail": f"{r['size'] or 0:,} bytes"}
                    for r in conn.execute(sql, [*args, limit, offset])]

    def _range(self, rel_dir: str) -> tuple[str, str]:
        prefix = rel_dir + "/"
        return prefix, prefix[:-1] + "0"

    def _kind(self, rel: str) -> str:
        path = self.path / rel
        if path.is_dir():
            return "dir"
        if path.is_file():
            return "file"
        raise NotFound(f"'{rel}' does not exist.")

    def item_ids_under(self, rel_path: str | None) -> set[str] | None:
        with self.connect() as conn:
            if not rel_path:
                return {r[0] for r in conn.execute("SELECT id FROM items")}
            rel = safe_rel_path(rel_path)
            low, high = self._range(rel)
            rows = conn.execute("SELECT id FROM items WHERE rel_path=? OR (rel_path >= ? AND rel_path < ?)", (rel, low, high))
            return {r[0] for r in rows}

    def rename_item(self, rel_path: str, new_name: str) -> str:
        """Renames a file or folder on disk. Items keep their ids, so datasets keep pointing at them."""
        rel = safe_rel_path(rel_path)
        new_name = check_file_name(new_name)
        kind = self._kind(rel)
        parent = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
        dest_rel = parent + new_name
        if dest_rel == rel:
            return rel
        with self.lock:
            rename_on_disk(self.path / rel, self.path / dest_rel)
            with self.connect() as conn, Tx(conn):
                if kind == "file":
                    conn.execute("UPDATE items SET rel_path=?, ext=? WHERE rel_path=?", (dest_rel, ext_of(dest_rel), rel))
                else:
                    low, high = self._range(rel)
                    conn.execute("UPDATE items SET rel_path = ? || substr(rel_path, ?) WHERE rel_path >= ? AND rel_path < ?",
                                 (dest_rel + "/", len(rel) + 2, low, high))
        return dest_rel

    def delete_item(self, rel_path: str, referenced: set[str]) -> None:
        """Deletes a file or a folder with everything in it from disk. Items that datasets still reference stay
        as `missing`, so those entries show 'source missing'; the others are forgotten."""
        rel = safe_rel_path(rel_path)
        kind = self._kind(rel)
        with self.lock:
            target = self.path / rel
            if kind == "dir":
                shutil.rmtree(target)
            else:
                target.unlink()
            ids = self.item_ids_under(rel) or set()
            with self.connect() as conn, Tx(conn):
                for batch in chunks(ids):
                    marks = ",".join("?" * len(batch))
                    kept = [i for i in batch if i in referenced]
                    gone = [i for i in batch if i not in referenced]
                    if gone:
                        conn.execute(f"DELETE FROM items WHERE id IN ({','.join('?' * len(gone))})", gone)
                    if kept:
                        conn.execute(f"UPDATE items SET status='missing' WHERE id IN ({','.join('?' * len(kept))})", kept)


def ext_of(rel_path: str) -> str:
    name = rel_path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name.lstrip(".") else ""


# --------------------------------------------------------------------------- table source

def sniff_dialect(sample: str):
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


class TableSource(Source):
    kind = "table"

    # -- the CSV
    def csv_files(self) -> list[Path]:
        return sorted(p for p in self.path.iterdir() if p.is_file() and p.suffix.lower() == ".csv" and not hidden(p.name))

    def csv_file(self) -> Path | None:
        """The CSV of this source, or None if there is none or (ambiguous) several: a source is exactly one CSV, and one of
        several is never chosen silently."""
        files = self.csv_files()
        return files[0] if len(files) == 1 else None

    def the_csv(self) -> Path:
        """Like csv_file(), but says what is wrong."""
        files = self.csv_files()
        if not files:
            raise WorkbenchError("This source has no CSV file yet.")
        if len(files) > 1:
            names = ", ".join(p.name for p in files[:4]) + (" …" if len(files) > 4 else "")
            raise WorkbenchError(f"This source has {len(files)} CSV files ({names}). A tabular source cohort is exactly one CSV: "
                                 "remove or move the others.")
        return files[0]

    def _reader(self, handle):
        sample = handle.read(65536)
        handle.seek(0)
        return csv.reader(handle, sniff_dialect(sample))

    def _open(self, path: Path):
        return open(path, newline="", encoding="utf-8-sig", errors="replace")

    def inspect(self, sample_rows: int = 2000) -> dict:
        """Proposes columns (name, inferred type, key) from the header and a sample of rows."""
        path = self.the_csv()
        with self._open(path) as handle:
            reader = self._reader(handle)
            header = next(reader, None)
            if not header:
                raise WorkbenchError("The CSV file is empty.")
            all_names = [h.strip() for h in header]
            keep = kept_columns(all_names)  # unnamed columns (Excel leaves them behind) are ignored
            names = [all_names[i] for i in keep]
            if not names:
                raise WorkbenchError("The header of the CSV file has no column names.")
            check_header(names)
            rows = []
            for i, row in enumerate(reader):
                if i >= sample_rows:
                    break
                if len(row) == len(all_names):
                    rows.append([row[k] for k in keep])
        columns = []
        for i, name in enumerate(names):
            values = [r[i] for r in rows]
            columns.append({"name": name, "type": infer_type(values), "is_key": False})
        key = guess_key(names, rows)
        auto = key is None
        if not auto:
            columns[key]["is_key"] = True
        return {"csv": path.name, "columns": columns, "sampled": len(rows), "ignored": len(all_names) - len(names),
                "auto_key": auto}

    def auto_key(self) -> bool:
        with self.connect() as conn:
            return meta_get(conn, "key_mode") == "auto"

    def declared_columns(self) -> list[dict]:
        with self.connect() as conn:
            return [{"name": r["name"], "type": r["type"], "is_key": bool(r["is_key"])}
                    for r in conn.execute("SELECT * FROM columns ORDER BY position")]

    def state(self) -> str:
        """no_csv | ambiguous (several CSV files) | needs_setup | ready"""
        files = self.csv_files()
        if not files:
            return "no_csv"
        if len(files) > 1:
            return "ambiguous"
        return "ready" if self.declared_columns() else "needs_setup"

    def info(self) -> dict:
        csv_path = self.csv_file()
        state = self.state()
        with self.connect() as conn:
            meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
            rows = issues = 0
            if state == "ready":
                rows = conn.execute("SELECT COUNT(*) FROM data").fetchone()[0] if _has_table(conn, "data") else 0
                issues = int(meta.get("issue_total") or 0)
            ingest = conn.execute("SELECT * FROM ingest ORDER BY rowid DESC LIMIT 1").fetchone()
        return {"id": meta["source_id"], "name": meta["name"], "kind": "table", "state": state,
                "csv": csv_path.name if csv_path else None, "csv_path": str(csv_path) if csv_path else None,
                "csv_files": [{"name": p.name, "path": str(p), "size": p.stat().st_size} for p in self.csv_files()],
                "rows": rows, "issues": issues, "columns": self.declared_columns() if state == "ready" else [],
                "auto_key": meta.get("key_mode") == "auto",
                "ingested_at": ingest["ingested_at"] if ingest else None,
                "error": (self.the_csv_error() if state == "ambiguous" else meta.get("ingest_error"))}

    def the_csv_error(self) -> str:
        try:
            self.the_csv()
        except WorkbenchError as e:
            return e.message
        return ""

    def configure(self, columns: list[dict], progress: Progress | None = None, auto_key: bool = False) -> dict:
        """Declares the columns (types, key) and ingests the CSV. With `auto_key` no column is the key:
        every row is identified by its position in the file (1, 2, 3 ...)."""
        path = self.the_csv()
        if not isinstance(columns, list) or not columns:
            raise WorkbenchError("No columns.")
        names = []
        keys = 0
        for c in columns:
            name = str(c.get("name", "")).strip()
            if not name:
                raise WorkbenchError("A column has no name.")
            if c.get("type") not in COLUMN_TYPES:
                raise WorkbenchError(f"Column '{name}': unknown type '{c.get('type')}'.")
            names.append(name)
            keys += bool(c.get("is_key"))
        check_header(names)
        if auto_key:
            keys = 0
        elif keys != 1:
            raise WorkbenchError("Exactly one column must be the key, or generate the keys.")
        with self.lock:
            with self.connect() as conn, Tx(conn):
                conn.execute("DELETE FROM columns")
                conn.executemany("INSERT INTO columns(position, name, type, is_key) VALUES(?,?,?,?)",
                                 [(i, c["name"].strip(), c["type"], 0 if auto_key else int(bool(c.get("is_key")))) for i, c in enumerate(columns)])
                meta_set(conn, "key_mode", "auto" if auto_key else "column")
                conn.execute("DELETE FROM ingest")
            return self.ingest(progress)

    def ingest(self, progress: Progress | None = None) -> dict:
        path = self.the_csv()
        columns = self.declared_columns()
        if not columns:
            raise WorkbenchError("The source is not set up yet.")
        names = [c["name"] for c in columns]
        key_index = next((i for i, c in enumerate(columns) if c["is_key"]), None)  # None: generated keys
        stat = path.stat()
        with self.lock:
            with self._open(path) as handle:
                reader = self._reader(handle)
                header = [h.strip() for h in (next(reader, None) or [])]
                keep = kept_columns(header)
                if [header[i] for i in keep] != names:
                    with self.connect() as conn:
                        conn.execute("DELETE FROM columns")
                        meta_set(conn, "ingest_error", "The columns of the CSV file changed; set the columns up again.")
                    raise WorkbenchError("The columns of the CSV file differ from the declared columns; set them up again.")
                with self.connect() as conn:
                    ddl = ", ".join(f"{q(c['name'])} {SQL_TYPES[c['type']]}" for c in columns)
                    with Tx(conn):
                        conn.execute("DROP TABLE IF EXISTS data")
                        conn.execute(f"CREATE TABLE data (_key TEXT PRIMARY KEY, _row INTEGER, _hash TEXT, {ddl})")
                        conn.execute("DELETE FROM row_issues")
                        total, issues, kept = self._load_rows(conn, reader, columns, key_index, progress, keep, len(header))
                        conn.execute("DELETE FROM ingest")
                        conn.execute("INSERT INTO ingest VALUES(?,?,?,?)", (path.name, stat.st_size, stat.st_mtime_ns, now_iso()))
                        meta_set(conn, "issue_total", issues)
                        meta_set(conn, "ingest_error", None)
        return {"rows": kept, "read": total, "issues": issues}

    def _load_rows(self, conn, reader, columns, key_index, progress, keep, width):
        names = [c["name"] for c in columns]
        placeholders = ",".join("?" * (len(names) + 3))
        insert = f"INSERT INTO data VALUES({placeholders})"
        seen: dict[str, int | None] = {}
        issues = 0
        stored = 0
        total = 0

        def issue(row_number, key, column, code, message):
            nonlocal issues
            issues += 1
            if stored_issues[0] < MAX_STORED_ISSUES:
                stored_issues[0] += 1
                conn.execute("INSERT INTO row_issues VALUES(?,?,?,?,?)", (row_number, key, column, code, message))

        stored_issues = [0]
        for row_number, row in enumerate(reader, start=2):
            if not any(cell.strip() for cell in row):
                continue
            total += 1
            if progress and total % 20000 == 0:
                progress({"phase": "ingest", "rows": total})
            if len(row) != width:
                issue(row_number, None, None, "column_count", f"expected {width} values, found {len(row)}")
                continue
            row = [row[i] for i in keep]
            key = str(row_number - 1) if key_index is None else row[key_index].strip()
            if not key:
                issue(row_number, None, names[key_index], "empty_key", "the key is empty")
                continue
            if key in seen:
                if seen[key] is not None:  # the first occurrence becomes unusable, too
                    conn.execute("DELETE FROM data WHERE _key=?", (key,))
                    stored -= 1
                    issue(seen[key], key, names[key_index], "duplicate_key", f"key '{key}' is not unique")
                    seen[key] = None
                issue(row_number, key, names[key_index], "duplicate_key", f"key '{key}' is not unique")
                continue
            values, bad = [], False
            for c, cell in zip(columns, row):
                if not cell.strip():
                    values.append(None)
                    continue
                try:
                    values.append(convert(c["type"], cell))
                except ValueError as e:
                    issue(row_number, key, c["name"], "type", str(e))
                    bad = True
            if bad:
                continue
            seen[key] = row_number
            digest = sha1_text("\x1f".join(row))
            conn.execute(insert, [key, row_number, digest, *values])
            stored += 1
        return total, issues, stored

    def refresh(self, progress: Progress | None = None) -> bool:
        """Re-ingests when the CSV changed on disk (size or mtime differ from the last ingest)."""
        path = self.csv_file()
        if path is None or not self.declared_columns():
            return False
        st = path.stat()
        with self.connect() as conn:
            last = conn.execute("SELECT size, mtime_ns FROM ingest ORDER BY rowid DESC LIMIT 1").fetchone()
        if last and last["size"] == st.st_size and last["mtime_ns"] == st.st_mtime_ns and self._has_data():
            return False
        try:
            self.ingest(progress)
        except WorkbenchError:
            pass  # the source shows the problem as needs_setup
        return True

    def _has_data(self) -> bool:
        with self.connect() as conn:
            return _has_table(conn, "data")

    def issues(self, limit: int, offset: int) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT row_number, key, column_name AS column, code, message FROM row_issues ORDER BY row_number, column_name LIMIT ? OFFSET ?",
                (limit, offset))]

    def find(self, q: str, limit: int) -> list[dict]:
        """The CSV files whose name contains every word of `q` (the global search)."""
        words = [w.lower() for w in q.split()]
        if not words:
            return []
        return [{"id": p.name, "path": p.name, "name": p.name, "ext": "csv", "size": p.stat().st_size, "status": "ok", "abs_path": str(p)}
                for p in self.csv_files() if all(w in p.name.lower() for w in words)]

    # -- references
    def _columns_for(self, columns) -> list[str]:
        declared = [c["name"] for c in self.declared_columns()]
        if not columns:
            return declared
        return [c for c in columns if c in declared]

    def has_columns(self, columns) -> list[str]:
        declared = {c["name"] for c in self.declared_columns()}
        return [c for c in columns if c not in declared]

    def fingerprints(self, item_ids, columns=None) -> dict:
        cols = self._columns_for(columns)
        found = {}
        if not cols or not self._has_data():
            return found
        select = ", ".join(q(c) for c in cols)
        with self.connect() as conn:
            for ids in chunks(item_ids):
                rows = conn.execute(f"SELECT _key, {select} FROM data WHERE _key IN ({','.join('?' * len(ids))})", ids)
                for r in rows:
                    found[r["_key"]] = (row_fingerprint(cols, [r[c] for c in cols]), r["_key"])
        return found

    def describe(self, item_id: str, columns=None) -> dict | None:
        cols = self._columns_for(columns)
        if not cols or not self._has_data():
            return None
        select = ", ".join(q(c) for c in cols)
        with self.connect() as conn:
            r = conn.execute(f"SELECT _key, {select} FROM data WHERE _key=?", (item_id,)).fetchone()
        if not r:
            return None
        return {"label": r["_key"], "values": {c: r[c] for c in cols}}

    def search(self, q_text: str, limit: int, offset: int, columns=None, **options) -> list[dict]:
        cols = self._columns_for(columns)
        if not cols or not self._has_data():
            return []
        shown = cols[:4]
        sql = f"SELECT _key, {', '.join(q(c) for c in shown)} FROM data"
        args: list = []
        if q_text.strip():
            pattern = f"%{like_escape(q_text.strip())}%"
            clauses = ["_key LIKE ? ESCAPE '\\'"] + [f"CAST({q(c)} AS TEXT) LIKE ? ESCAPE '\\'" for c in cols[:12]]
            sql += " WHERE " + " OR ".join(clauses)
            args = [pattern] * len(clauses)
        sql += " ORDER BY _row LIMIT ? OFFSET ?"
        with self.connect() as conn:
            return [{"item": r["_key"], "label": r["_key"],
                     "detail": ", ".join(f"{c}={r[c]}" for c in shown if r[c] is not None and c != r["_key"])[:120]}
                    for r in conn.execute(sql, [*args, limit, offset])]

    def rename_item(self, rel_path: str, new_name: str) -> str:
        """Renames the CSV file (the only item of a table source with a name)."""
        csv_path = self._csv_named(rel_path)
        new_name = check_file_name(new_name)
        if not new_name.lower().endswith(".csv"):
            raise WorkbenchError("The CSV file name must end in .csv.")
        with self.lock:
            rename_on_disk(csv_path, self.path / new_name)
            with self.connect() as conn:
                conn.execute("UPDATE ingest SET csv_name=?", (new_name,))
        return new_name

    def delete_item(self, rel_path: str, referenced: set[str]) -> None:
        """Deletes the CSV file; the rows are gone, so entries that used them show 'source missing'."""
        csv_path = self._csv_named(rel_path)
        with self.lock:
            others = [p for p in self.csv_files() if p != csv_path]
            csv_path.unlink()
            if others:  # one of several: the source is not (yet) the CSV that is left, so nothing is forgotten
                return
            with self.connect() as conn, Tx(conn):
                conn.execute("DROP TABLE IF EXISTS data")
                for table in ("columns", "ingest", "row_issues"):
                    conn.execute(f"DELETE FROM {table}")
                meta_set(conn, "issue_total", 0)
                meta_set(conn, "ingest_error", None)

    def _csv_named(self, rel_path: str) -> Path:
        name = safe_rel_path(rel_path)
        for p in self.csv_files():
            if p.name == name:
                return p
        raise NotFound("No such file in this source.")


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def row_fingerprint(columns: list[str], values: list) -> str:
    return hashlib.sha1(json.dumps([columns, values], default=str).encode("utf-8")).hexdigest()


def kept_columns(header: list[str]) -> list[int]:
    """Indexes of the named columns; columns with an empty header are ignored."""
    return [i for i, name in enumerate(header) if name]


def check_header(names: list[str]) -> None:
    if len(set(names)) != len(names):
        raise WorkbenchError("The column names are not unique.")
    for n in names:
        if not n:
            raise WorkbenchError("A column has no name.")
        if n in RESERVED_COLUMNS:
            raise WorkbenchError(f"The column name '{n}' is reserved.")


def guess_key(names: list[str], rows: list[list[str]]) -> int | None:
    """The index of a column that can be the key, or None if no column is unique and never empty."""
    def unique(i):
        values = [r[i].strip() for r in rows]
        return bool(values) and all(values) and len(set(values)) == len(values)

    idlike = [i for i, n in enumerate(names) if re.search(r"(^|_|\b)(id|key|pseudonym|uuid)$", n.lower())]
    for i in idlike + list(range(len(names))):
        if unique(i):
            return i
    return None
