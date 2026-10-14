"""Table viewer: tabular files as SQLite tables: CSV/TSV and JSON are loaded into a temporary database, SQLite files are opened read-only."""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from datetime import date, datetime
from pathlib import Path

from errors import ViewerError

SAMPLE_ROWS = 5000
MAX_SEARCH_COLUMNS = 40


# Opening and closing SQLite connections from several threads at once can hang inside SQLite (several threads stuck in
# connect/close). Only one thread at a time opens or closes a connection; the queries themselves still run in parallel.
_open_close = threading.Lock()


def q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def infer(values: list[str]) -> str:
    values = [v.strip() for v in values if v is not None and str(v).strip() != ""]
    if not values:
        return "TEXT"

    def all_ok(parse):
        try:
            for v in values:
                parse(v)
            return True
        except ValueError:
            return False

    if all_ok(int):
        return "INTEGER"
    if all_ok(lambda v: float(v) if v.lower() not in ("nan", "inf", "-inf", "infinity") else (_ for _ in ()).throw(ValueError())):
        return "REAL"
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in values) and all_ok(date.fromisoformat):
        return "DATE"
    if all_ok(lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))):
        return "DATETIME"
    return "TEXT"


def convert(kind: str, value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    try:
        if kind == "INTEGER":
            return int(value)
        if kind == "REAL":
            return float(value)
    except ValueError:
        pass
    return value


def unique_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, name in enumerate(names):
        name = (name or "").strip() or f"column_{i + 1}"
        n = seen.get(name, 0)
        seen[name] = n + 1
        out.append(name if n == 0 else f"{name}_{n + 1}")
    return out


def jsonable(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    return value


class Table:
    def __init__(self, name: str, columns: list[dict], rows: int):
        self.name, self.columns, self.rows = name, columns, rows

    def info(self) -> dict:
        return {"name": self.name, "columns": self.columns, "rows": self.rows}


class Store:
    """One opened file. `load()` runs in a worker thread; `state` is loading | ready | error."""

    def __init__(self, path: Path):
        self.path = path
        self.fingerprint = self._stat()
        self.state = "loading"
        self.error: str | None = None
        self.progress = 0
        self.tables: dict[str, Table] = {}
        self._dir: str | None = None
        self._db_path: str | None = None
        self._readonly_uri = False
        self._lock = threading.Lock()

    def _stat(self):
        st = self.path.stat()
        return (st.st_size, st.st_mtime_ns)

    def stale(self) -> bool:
        try:
            return self._stat() != self.fingerprint
        except OSError:
            return True

    def close(self) -> None:
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)

    def info(self) -> dict:
        return {"file": str(self.path), "name": self.path.name, "state": self.state, "error": self.error,
                "progress": self.progress, "tables": [t.info() for t in self.tables.values()]}

    # -- loading
    def load(self) -> None:
        try:
            suffix = self.path.suffix.lower()
            if suffix in (".sqlite", ".sqlite3", ".db"):
                self._load_sqlite()
            elif suffix in (".json", ".jsonl", ".ndjson"):
                self._load_json(suffix)
            else:
                self._load_csv()
            self.state = "ready"
        except ViewerError as e:
            self.state, self.error = "error", str(e)
        except (OSError, sqlite3.Error, csv.Error, UnicodeError, ValueError) as e:
            self.state, self.error = "error", f"{type(e).__name__}: {e}"

    def _connect_loader(self) -> sqlite3.Connection:
        with _open_close:
            return sqlite3.connect(self._db_path)

    def _new_db(self) -> sqlite3.Connection:
        self._dir = tempfile.mkdtemp(prefix="relviewer-")
        self._db_path = os.path.join(self._dir, "data.sqlite")
        with _open_close:
            conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    def _load_sqlite(self) -> None:
        self._db_path = f"file:{self.path.as_posix()}?mode=ro"
        self._readonly_uri = True
        with self.connection() as conn:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for name in names:
                self.tables[name] = self._describe(conn, name)
        if not self.tables:
            raise ViewerError("This SQLite file has no tables.")

    @staticmethod
    def _describe(conn, name: str) -> Table:
        columns = [{"name": r[1], "type": (r[2] or "").upper() or "ANY"} for r in conn.execute(f"PRAGMA table_info({q(name)})")]
        rows = conn.execute(f"SELECT COUNT(*) FROM {q(name)}").fetchone()[0]
        return Table(name, columns, rows)

    def _load_csv(self) -> None:
        with open(self.path, newline="", encoding="utf-8-sig", errors="replace") as handle:
            sample = handle.read(65536)
            handle.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel_tab if self.path.suffix.lower() == ".tsv" else csv.excel
            reader = csv.reader(handle, dialect)
            header = next(reader, None)
            if not header:
                raise ViewerError("The file is empty.")
            names = unique_names(header)
            head: list[list[str]] = []
            for row in reader:
                head.append(row)
                if len(head) >= SAMPLE_ROWS:
                    break
            kinds = [infer([r[i] for r in head if i < len(r)]) for i in range(len(names))]

            def rows():
                for r in head:
                    yield r
                for r in reader:
                    yield r

            self._insert_table("data", names, kinds, rows())

    def _insert_table(self, table: str, names: list[str], kinds: list[str], rows) -> None:
        conn = self._new_db() if self._db_path is None or self._readonly_uri else self._connect_loader()
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                conn.execute(f"CREATE TABLE {q(table)} ({', '.join(f'{q(n)} {k}' for n, k in zip(names, kinds))})")
            insert = f"INSERT INTO {q(table)} VALUES ({','.join('?' * len(names))})"
            batch, count = [], 0
            for r in rows:
                values = [convert(kinds[i], r[i]) if i < len(r) else None for i in range(len(names))]
                batch.append(values)
                count += 1
                if len(batch) >= 5000:
                    conn.executemany(insert, batch)
                    batch.clear()
                    self.progress = count
            conn.executemany(insert, batch)
            conn.commit()
        finally:
            with _open_close:
                conn.close()
        with self.connection() as c:
            self.tables[table] = self._describe(c, table)

    def _load_json(self, suffix: str) -> None:
        text = self.path.read_text(encoding="utf-8-sig", errors="replace")
        if suffix in (".jsonl", ".ndjson"):
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            data = json.loads(text)
        tables: dict[str, list] = {}
        if isinstance(data, list):
            tables["records"] = data
        elif isinstance(data, dict):
            lists = {k: v for k, v in data.items() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)}
            if lists:
                tables.update(lists)
            else:
                tables["object"] = [data]
        else:
            raise ViewerError("The JSON file holds a single value, not records.")
        for name, records in tables.items():
            if not records:
                continue
            records = [r if isinstance(r, dict) else {"value": r} for r in records]
            names = unique_names(list(dict.fromkeys(k for r in records for k in r)))
            keys = list(dict.fromkeys(k for r in records for k in r))
            flat = [[(json.dumps(r.get(k), ensure_ascii=False) if isinstance(r.get(k), (dict, list)) else r.get(k)) for k in keys] for r in records]
            kinds = []
            for i in range(len(names)):
                values = [row[i] for row in flat]
                if all(isinstance(v, bool) or v is None for v in values):
                    kinds.append("INTEGER")
                elif all(isinstance(v, int) and not isinstance(v, bool) or v is None for v in values):
                    kinds.append("INTEGER")
                elif all(isinstance(v, (int, float)) and not isinstance(v, bool) or v is None for v in values):
                    kinds.append("REAL")
                else:
                    kinds.append("TEXT")
            self._insert_table(name, names, kinds, ([None if v is None else (int(v) if isinstance(v, bool) else v) for v in row] for row in flat))
        if not self.tables:
            raise ViewerError("No records found in this JSON file.")

    # -- querying
    def connection(self) -> sqlite3.Connection:
        with _open_close:
            conn = sqlite3.connect(self._db_path, uri=self._readonly_uri, check_same_thread=False)
        return _Closing(conn)

    def page(self, table: str, limit: int, offset: int, sort: str | None, descending: bool, search: str, filters: dict) -> dict:
        t = self.tables.get(table)
        if t is None:
            raise ViewerError("No such table.", 404)
        names = [c["name"] for c in t.columns]
        where, args = [], []
        if search.strip():
            pattern = f"%{_escape(search.strip())}%"
            clauses = [f"CAST({q(n)} AS TEXT) LIKE ? ESCAPE '\\'" for n in names[:MAX_SEARCH_COLUMNS]]
            where.append("(" + " OR ".join(clauses) + ")")
            args += [pattern] * len(clauses)
        for column, text in filters.items():
            if column not in names:
                raise ViewerError(f"No such column: {column}")
            if str(text).strip():
                where.append(f"CAST({q(column)} AS TEXT) LIKE ? ESCAPE '\\'")
                args.append(f"%{_escape(str(text).strip())}%")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        order = ""
        if sort:
            if sort not in names:
                raise ViewerError(f"No such column: {sort}")
            order = f" ORDER BY {q(sort)} IS NULL, {q(sort)} {'DESC' if descending else 'ASC'}"
        with self.connection() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM {q(table)}{clause}", args).fetchone()[0] if where else t.rows
            rows = conn.execute(f"SELECT {', '.join(q(n) for n in names)} FROM {q(table)}{clause}{order} LIMIT ? OFFSET ?",
                                [*args, limit, offset]).fetchall()
        return {"total": total, "offset": offset, "rows": [[jsonable(v) for v in row] for row in rows]}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class _Closing:
    """A connection that closes at the end of `with`."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        with _open_close:
            self._conn.close()
        return False
