"""Datasets: a schema of slots, entries made of one cell per slot, and the state model (design 7.3)."""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path

from .catalog import Catalog
from .sources import FileSource, TableSource
from .util import (NotFound, Tx, WorkbenchError, check_identifier, check_name, chunks, connect, like_escape, lock_for,
                   meta_get, meta_set, new_id, now_iso, slugify)

DATASET_DB = "dataset.sqlite"
VALUE_TYPES = ("text", "integer", "float", "boolean", "date", "datetime", "choice")
KINDS = ("value", "file", "row")
CELL_STATES = ("ok", "incomplete", "invalid", "source_missing", "source_changed")


# --------------------------------------------------------------------------- values

def coerce_value(value_type: str, raw):
    """Normalizes a value for a slot type; raises ValueError with a readable message."""
    if isinstance(raw, str):
        raw = raw.strip()
    if value_type in ("text", "choice"):
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise ValueError("expected text")
        return str(raw)
    if value_type == "integer":
        if isinstance(raw, bool):
            raise ValueError("expected an integer")
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        if isinstance(raw, int):
            return raw
        try:
            return int(str(raw))
        except ValueError:
            raise ValueError(f"'{raw}' is not an integer") from None
    if value_type == "float":
        if isinstance(raw, bool):
            raise ValueError("expected a number")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"'{raw}' is not a number") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"'{raw}' is not a finite number")
        return value
    if value_type == "boolean":
        if isinstance(raw, bool):
            return raw
        text = str(raw).lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        raise ValueError(f"'{raw}' is not a boolean")
    if value_type == "date":
        try:
            return date.fromisoformat(str(raw)).isoformat()
        except ValueError:
            raise ValueError(f"'{raw}' is not a date (YYYY-MM-DD)") from None
    if value_type == "datetime":
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).isoformat(sep="T")
        except ValueError:
            raise ValueError(f"'{raw}' is not a date and time (ISO 8601)") from None
    raise ValueError(f"unknown type {value_type}")


def value_problem(slot: dict, stored) -> str | None:
    """Why `stored` does not fit the value slot, or None. Called with the stored value of a non-empty cell."""
    kind, c = slot["value_type"], slot["constraints"]
    try:
        value = coerce_value(kind, stored)
    except ValueError as e:
        return str(e)
    if kind in ("integer", "float"):
        if c.get("min") is not None and value < c["min"]:
            return f"must be at least {c['min']}"
        if c.get("max") is not None and value > c["max"]:
            return f"must be at most {c['max']}"
    elif kind == "text":
        if c.get("max_length") is not None and len(value) > c["max_length"]:
            return f"longer than {c['max_length']} characters"
        if c.get("regex") and not re.fullmatch(c["regex"], value):
            return "does not match the required pattern"
    elif kind == "choice":
        if value not in (c.get("choices") or []):
            return f"'{value}' is not one of the allowed choices"
    return None


def display_value(stored) -> str:
    if isinstance(stored, bool):
        return "yes" if stored else "no"
    return str(stored)


# --------------------------------------------------------------------------- schema

def normalize_slot(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise WorkbenchError("Invalid slot.")
    name = check_identifier(raw.get("name"), "slot name")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise WorkbenchError(f"Slot '{name}': kind must be value, file or row.")
    slot = {"id": raw.get("id"), "name": name, "label": str(raw.get("label") or name)[:100],
            "description": str(raw.get("description") or "")[:500], "required": bool(raw.get("required")),
            "unique": bool(raw.get("unique")), "kind": kind, "value_type": None, "constraints": {},
            "sources": [], "extensions": [], "columns": []}
    if kind == "value":
        vt = raw.get("value_type")
        if vt not in VALUE_TYPES:
            raise WorkbenchError(f"Slot '{name}': unknown value type '{vt}'.")
        slot["value_type"] = vt
        slot["constraints"] = _constraints(name, vt, raw.get("constraints") or {})
    else:
        sources = raw.get("sources") or []
        if not isinstance(sources, list) or not sources or not all(isinstance(s, str) for s in sources):
            raise WorkbenchError(f"Slot '{name}': choose at least one source.")
        slot["sources"] = list(dict.fromkeys(sources))
        if kind == "file":
            slot["extensions"] = [str(e).lower().lstrip(".").strip() for e in raw.get("extensions") or [] if str(e).strip()]
        else:
            slot["columns"] = [str(c) for c in raw.get("columns") or []]
    return slot


def _constraints(name: str, vt: str, raw: dict) -> dict:
    out: dict = {}
    if raw.get("default") not in (None, ""):
        try:
            out["default"] = coerce_value(vt, raw["default"])
        except ValueError as e:
            raise WorkbenchError(f"Slot '{name}': default value: {e}") from None
    if vt in ("integer", "float"):
        for key in ("min", "max"):
            if raw.get(key) not in (None, ""):
                try:
                    out[key] = coerce_value(vt, raw[key])
                except ValueError as e:
                    raise WorkbenchError(f"Slot '{name}': {key}: {e}") from None
        if raw.get("unit"):
            out["unit"] = str(raw["unit"])[:20]
    elif vt == "text":
        if raw.get("max_length") not in (None, ""):
            try:
                out["max_length"] = int(raw["max_length"])
            except (TypeError, ValueError):
                raise WorkbenchError(f"Slot '{name}': max length must be a number.") from None
        if raw.get("regex"):
            try:
                re.compile(raw["regex"])
            except re.error as e:
                raise WorkbenchError(f"Slot '{name}': invalid pattern: {e}") from None
            out["regex"] = raw["regex"]
    elif vt == "choice":
        choices = [str(c).strip() for c in raw.get("choices") or [] if str(c).strip()]
        if not choices:
            raise WorkbenchError(f"Slot '{name}': a choice needs at least one allowed value.")
        out["choices"] = list(dict.fromkeys(choices))
    return out


# --------------------------------------------------------------------------- evaluation

class Resolver:
    """Looks sources up once per computation."""

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._cache: dict = {}

    def source(self, source_id: str):
        if source_id not in self._cache:
            self._cache[source_id] = self.catalog.try_source(source_id)
        return self._cache[source_id]


def _stored(cell) -> object:
    return None if cell["value"] is None else json.loads(cell["value"])


def _is_empty(slot: dict, cell) -> bool:
    if slot["kind"] == "value":
        stored = _stored(cell)
        return stored is None or stored == ""
    return cell["ref_item_id"] is None


def evaluate(slot: dict, cell, resolver: Resolver, fps: dict):
    """Returns (state, detail, label or None) for one cell."""
    if _is_empty(slot, cell):
        return ("incomplete", "required", None) if slot["required"] else ("ok", None, None)
    if slot["kind"] == "value":
        problem = value_problem(slot, _stored(cell))
        return ("invalid", problem, None) if problem else ("ok", None, None)

    source_id = cell["ref_source_id"]
    if source_id not in slot["sources"]:
        return "invalid", "this source is not allowed in this slot", None
    source = resolver.source(source_id)
    if source is None:
        return "source_missing", "the source was removed", None
    if slot["kind"] == "file":
        if not isinstance(source, FileSource):
            return "invalid", "a file slot needs a file source", None
        label = cell["ref_label"] or ""
        ext = label.rsplit(".", 1)[-1].lower() if "." in label else ""
    else:
        if not isinstance(source, TableSource):
            return "invalid", "a row slot needs a table source", None
        missing = source.has_columns(slot["columns"]) if slot["columns"] else []
        if missing:
            return "invalid", f"the source has no column {', '.join(missing)}", None
        ext = None
    key = (source_id, tuple(slot["columns"]) if slot["kind"] == "row" else ())
    current = fps.get(key, {}).get(cell["ref_item_id"])
    if current is None:
        return "source_missing", "the item was deleted", None
    fingerprint, label = current
    if ext is not None and slot["extensions"]:
        real_ext = label.rsplit(".", 1)[-1].lower() if "." in label else ""
        if real_ext not in slot["extensions"]:
            return "invalid", f"file type .{real_ext} is not allowed here", label
    if fingerprint != cell["ref_fingerprint"]:
        return "source_changed", "the source was edited", label
    return "ok", None, label


# --------------------------------------------------------------------------- dataset

class Dataset:
    def __init__(self, catalog: Catalog, path: Path):
        self.catalog = catalog
        self.path = Path(path)
        self.db_path = self.path / DATASET_DB

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def lock(self):
        return lock_for(self.db_path)

    def connect(self):
        return connect(self.db_path)

    # -- creation
    @staticmethod
    def create(catalog: Catalog, name: str) -> "Dataset":
        name = check_name(name, "dataset name")
        base = slugify(name)
        folder, n = base, 2
        while (catalog.datasets_dir / folder).exists():
            folder, n = f"{base}-{n}", n + 1
        path = catalog.datasets_dir / folder
        path.mkdir()
        with connect(path / DATASET_DB, create=True) as conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("""CREATE TABLE slots (
                id INTEGER PRIMARY KEY, position INTEGER NOT NULL, name TEXT UNIQUE NOT NULL, label TEXT, description TEXT,
                required INTEGER NOT NULL, "unique" INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL,
                value_type TEXT, constraints TEXT, sources TEXT, extensions TEXT, columns TEXT)""")
            conn.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, created_at TEXT, updated_at TEXT,"
                         " state TEXT NOT NULL DEFAULT 'incomplete')")
            conn.execute("""CREATE TABLE cells (
                entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
                value TEXT, ref_source_id TEXT, ref_item_id TEXT, ref_fingerprint TEXT, ref_label TEXT,
                state TEXT NOT NULL DEFAULT 'incomplete', state_detail TEXT,
                PRIMARY KEY (entry_id, slot_id))""")
            conn.execute("CREATE INDEX cells_ref ON cells(ref_source_id, ref_item_id)")
            conn.execute("CREATE INDEX cells_slot ON cells(slot_id)")
            conn.execute("CREATE INDEX entries_state ON entries(state)")
            for k, v in {"dataset_id": new_id(), "name": name, "created_at": now_iso(), "schema_version": "1"}.items():
                meta_set(conn, k, v)
        return Dataset(catalog, path)

    # -- reading
    def slots(self, conn=None) -> list[dict]:
        own = conn is None
        conn = conn or self.connect()
        try:
            return [{"id": r["id"], "position": r["position"], "name": r["name"], "label": r["label"],
                     "description": r["description"] or "", "required": bool(r["required"]), "unique": bool(r["unique"]),
                     "kind": r["kind"], "value_type": r["value_type"], "constraints": json.loads(r["constraints"] or "{}"),
                     "sources": json.loads(r["sources"] or "[]"), "extensions": json.loads(r["extensions"] or "[]"),
                     "columns": json.loads(r["columns"] or "[]")}
                    for r in conn.execute("SELECT * FROM slots ORDER BY position")]
        finally:
            if own:
                conn.close()

    def summary(self) -> dict:
        with self.connect() as conn:
            counts = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM entries GROUP BY state")}
            return {"id": self.id, "name": meta_get(conn, "name", self.id), "entries": sum(counts.values()),
                    "states": {s: counts.get(s, 0) for s in ("valid", "incomplete", "corrupted")},
                    "slots": conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0]}

    def detail(self) -> dict:
        data = self.summary()
        with self.connect() as conn:
            slots = self.slots(conn)
        known = set(self.catalog.source_ids())
        for slot in slots:
            slot["dangling_sources"] = [s for s in slot["sources"] if s not in known]
            slot["source_names"] = {s: self._source_name(s) for s in slot["sources"]}
        data["schema"] = slots
        return data

    def _source_name(self, source_id: str) -> str | None:
        source = self.catalog.try_source(source_id, refresh=False)
        return source.name if source else None

    def rename(self, name: str) -> None:
        name = check_name(name, "dataset name")
        with self.lock, self.connect() as conn:
            meta_set(conn, "name", name)

    # -- schema editing
    def set_schema(self, raw_slots: list) -> list[dict]:
        if not isinstance(raw_slots, list):
            raise WorkbenchError("Invalid schema.")
        slots = [normalize_slot(s) for s in raw_slots]
        names = [s["name"] for s in slots]
        if len(set(names)) != len(names):
            raise WorkbenchError("Slot names must be unique.")
        with self.lock:
            with self.connect() as conn, Tx(conn):
                existing = {s["id"]: s for s in self.slots(conn)}
                keep = {s["id"] for s in slots if s["id"] in existing}
                for slot_id in set(existing) - keep:
                    conn.execute("DELETE FROM slots WHERE id=?", (slot_id,))
                # names are unique: park the existing ones so renames and swaps cannot collide
                conn.execute("UPDATE slots SET name='~' || id")
                for position, slot in enumerate(slots):
                    fields = (position, slot["name"], slot["label"], slot["description"], int(slot["required"]),
                              int(slot["unique"]), slot["kind"], slot["value_type"], json.dumps(slot["constraints"]),
                              json.dumps(slot["sources"]), json.dumps(slot["extensions"]), json.dumps(slot["columns"]))
                    if slot["id"] in existing:
                        if existing[slot["id"]]["kind"] != slot["kind"]:
                            conn.execute("UPDATE cells SET value=NULL, ref_source_id=NULL, ref_item_id=NULL,"
                                         " ref_fingerprint=NULL, ref_label=NULL WHERE slot_id=?", (slot["id"],))
                        conn.execute('UPDATE slots SET position=?, name=?, label=?, description=?, required=?, "unique"=?, kind=?,'
                                     " value_type=?, constraints=?, sources=?, extensions=?, columns=? WHERE id=?",
                                     (*fields, slot["id"]))
                    else:
                        cur = conn.execute('INSERT INTO slots(position, name, label, description, required, "unique", kind,'
                                           " value_type, constraints, sources, extensions, columns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                           fields)
                        default = slot["constraints"].get("default")
                        conn.execute("INSERT INTO cells(entry_id, slot_id, value) SELECT id, ?, ? FROM entries",
                                     (cur.lastrowid, None if default is None else json.dumps(default)))
            self.recompute()
        with self.connect() as conn:
            return self.slots(conn)

    # -- entries
    def create_entry(self) -> dict:
        with self.lock:
            with self.connect() as conn, Tx(conn):
                now = now_iso()
                entry_id = conn.execute("INSERT INTO entries(created_at, updated_at) VALUES(?,?)", (now, now)).lastrowid
                for slot in self.slots(conn):
                    default = slot["constraints"].get("default")
                    conn.execute("INSERT INTO cells(entry_id, slot_id, value) VALUES(?,?,?)",
                                 (entry_id, slot["id"], None if default is None else json.dumps(default)))
            self.recompute(entry_ids=[entry_id])
        return self.get_entry(entry_id)

    def delete_entry(self, entry_id: int) -> None:
        with self.lock, self.connect() as conn:
            if conn.execute("DELETE FROM entries WHERE id=?", (entry_id,)).rowcount == 0:
                raise NotFound("No such entry.")

    def get_entry(self, entry_id: int) -> dict:
        with self.connect() as conn:
            entry = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if not entry:
                raise NotFound("No such entry.")
            slots = self.slots(conn)
            cells = {c["slot_id"]: c for c in conn.execute("SELECT * FROM cells WHERE entry_id=?", (entry_id,))}
        resolver = Resolver(self.catalog)
        out = []
        for slot in slots:
            cell = cells.get(slot["id"])
            item = {"slot": slot["id"], "name": slot["name"], "state": cell["state"] if cell else "incomplete",
                    "detail": cell["state_detail"] if cell else None, "value": None, "ref": None}
            if cell is not None:
                if slot["kind"] == "value":
                    item["value"] = _stored(cell)
                elif cell["ref_item_id"] is not None:
                    ref = {"source": cell["ref_source_id"], "item": cell["ref_item_id"], "label": cell["ref_label"],
                           "source_name": None, "path": None, "values": None}
                    source = resolver.source(cell["ref_source_id"])
                    if source is not None:
                        ref["source_name"] = source.name
                        described = source.describe(cell["ref_item_id"], slot["columns"] or None)
                        if described:
                            ref.update({k: described.get(k) for k in ("path", "values")})
                            ref["label"] = described["label"]
                    item["ref"] = ref
            out.append(item)
        return {"id": entry["id"], "state": entry["state"], "created_at": entry["created_at"],
                "updated_at": entry["updated_at"], "cells": out}

    def list_entries(self, limit: int = 50, offset: int = 0, state: str | None = None, q: str = "") -> dict:
        where, args = [], []
        if state:
            where.append("e.state=?")
            args.append(state)
        if q.strip():
            where.append("EXISTS (SELECT 1 FROM cells c WHERE c.entry_id=e.id AND (c.value LIKE ? ESCAPE '\\' OR c.ref_label LIKE ? ESCAPE '\\'))")
            args += [f"%{like_escape(q.strip())}%"] * 2
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM entries e{clause}", args).fetchone()[0]
            rows = conn.execute(f"SELECT e.id, e.state FROM entries e{clause} ORDER BY e.id LIMIT ? OFFSET ?",
                                [*args, limit, offset]).fetchall()
            ids = [r["id"] for r in rows]
            cells: dict[int, dict] = {i: {} for i in ids}
            if ids:
                slot_kind = {s["id"]: s["kind"] for s in self.slots(conn)}
                for c in conn.execute(f"SELECT * FROM cells WHERE entry_id IN ({','.join('?' * len(ids))})", ids):
                    if slot_kind.get(c["slot_id"]) == "value":
                        stored = _stored(c)
                        text = None if stored in (None, "") else display_value(stored)
                    else:
                        text = c["ref_label"]
                    cells[c["entry_id"]][str(c["slot_id"])] = {"text": text, "state": c["state"]}
        return {"total": total, "entries": [{"id": r["id"], "state": r["state"], "cells": cells[r["id"]]} for r in rows]}

    def stats(self) -> dict:
        with self.connect() as conn:
            slots = self.slots(conn)
            by_state = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM entries GROUP BY state")}
            per_slot = {s["id"]: {"slot": s["id"], "name": s["name"], "label": s["label"], "required": s["required"],
                                  "filled": 0, "states": {k: 0 for k in CELL_STATES}} for s in slots}
            for r in conn.execute("SELECT slot_id, state, COUNT(*) AS n,"
                                  " SUM(value IS NOT NULL AND value NOT IN ('\"\"','null') OR ref_item_id IS NOT NULL) AS filled"
                                  " FROM cells GROUP BY slot_id, state"):
                entry = per_slot.get(r["slot_id"])
                if entry:
                    entry["states"][r["state"]] = r["n"]
                    entry["filled"] += r["filled"] or 0
        total = sum(by_state.values())
        items = {k: sum(s["states"][k] for s in per_slot.values()) for k in CELL_STATES}
        return {"entries": total, "states": {s: by_state.get(s, 0) for s in ("valid", "incomplete", "corrupted")},
                "items": items, "slots": list(per_slot.values())}

    # -- editing cells
    def _slot(self, conn, slot_id: int) -> dict:
        for slot in self.slots(conn):
            if slot["id"] == slot_id:
                return slot
        raise NotFound("No such slot.")

    def set_cell(self, entry_id: int, slot_id: int, payload: dict) -> dict:
        with self.lock:
            with self.connect() as conn, Tx(conn):
                slot = self._slot(conn, slot_id)
                cell = conn.execute("SELECT * FROM cells WHERE entry_id=? AND slot_id=?", (entry_id, slot_id)).fetchone()
                if cell is None:
                    raise NotFound("No such entry.")
                touched = self._identity_matches(conn, slot_id, cell)
                if slot["kind"] == "value":
                    raw = payload.get("value")
                    if raw is None or (isinstance(raw, str) and not raw.strip()):
                        conn.execute("UPDATE cells SET value=NULL WHERE entry_id=? AND slot_id=?", (entry_id, slot_id))
                    else:
                        try:
                            value = coerce_value(slot["value_type"], raw)
                        except ValueError as e:
                            raise WorkbenchError(f"{slot['label']}: {e}") from None
                        conn.execute("UPDATE cells SET value=? WHERE entry_id=? AND slot_id=?",
                                     (json.dumps(value), entry_id, slot_id))
                else:
                    if not payload.get("item"):
                        conn.execute("UPDATE cells SET ref_source_id=NULL, ref_item_id=NULL, ref_fingerprint=NULL, ref_label=NULL"
                                     " WHERE entry_id=? AND slot_id=?", (entry_id, slot_id))
                    else:
                        source_id, item = payload.get("source"), str(payload["item"])
                        if source_id not in slot["sources"]:
                            raise WorkbenchError("This source is not allowed in this slot.")
                        source = self.catalog.find_source(source_id)
                        found = source.fingerprints([item], slot["columns"] or None)
                        if item not in found:
                            raise NotFound("No such item in that source.")
                        fingerprint, label = found[item]
                        conn.execute("UPDATE cells SET ref_source_id=?, ref_item_id=?, ref_fingerprint=?, ref_label=?"
                                     " WHERE entry_id=? AND slot_id=?", (source_id, item, fingerprint, label, entry_id, slot_id))
                conn.execute("UPDATE entries SET updated_at=? WHERE id=?", (now_iso(), entry_id))
                new_cell = conn.execute("SELECT * FROM cells WHERE entry_id=? AND slot_id=?", (entry_id, slot_id)).fetchone()
                touched |= self._identity_matches(conn, slot_id, new_cell)
            if slot["unique"]:
                self.recompute(entry_ids=sorted(touched | {entry_id}), slot_ids=[slot_id])
            self.recompute(entry_ids=[entry_id])
        return self.get_entry(entry_id)

    @staticmethod
    def _identity_matches(conn, slot_id: int, cell) -> set[int]:
        """Entries whose cell in the slot has the same value or reference as `cell` (they share uniqueness)."""
        if cell["value"] is not None:
            rows = conn.execute("SELECT entry_id FROM cells WHERE slot_id=? AND value=?", (slot_id, cell["value"]))
        elif cell["ref_item_id"] is not None:
            rows = conn.execute("SELECT entry_id FROM cells WHERE slot_id=? AND ref_source_id=? AND ref_item_id=?",
                                (slot_id, cell["ref_source_id"], cell["ref_item_id"]))
        else:
            return set()
        return {r["entry_id"] for r in rows}

    def mark_valid(self, entry_id: int, slot_id: int) -> dict:
        """D32: accept the changed source item, or clear a reference whose item is gone."""
        with self.lock:
            with self.connect() as conn:
                slot = self._slot(conn, slot_id)
                cell = conn.execute("SELECT * FROM cells WHERE entry_id=? AND slot_id=?", (entry_id, slot_id)).fetchone()
            if cell is None or slot["kind"] == "value":
                raise WorkbenchError("Nothing to mark as valid here.")
            if cell["state"] == "source_changed":
                source = self.catalog.find_source(cell["ref_source_id"])
                found = source.fingerprints([cell["ref_item_id"]], slot["columns"] or None)
                if cell["ref_item_id"] in found:
                    fingerprint, label = found[cell["ref_item_id"]]
                    with self.connect() as conn:
                        conn.execute("UPDATE cells SET ref_fingerprint=?, ref_label=? WHERE entry_id=? AND slot_id=?",
                                     (fingerprint, label, entry_id, slot_id))
            elif cell["state"] == "source_missing":
                with self.connect() as conn:
                    conn.execute("UPDATE cells SET ref_source_id=NULL, ref_item_id=NULL, ref_fingerprint=NULL, ref_label=NULL"
                                 " WHERE entry_id=? AND slot_id=?", (entry_id, slot_id))
            else:
                raise WorkbenchError("This item has no source problem.")
            self.recompute(entry_ids=[entry_id])
        return self.get_entry(entry_id)

    def candidates(self, slot_id: int, q: str, limit: int = 30, offset: int = 0) -> list[dict]:
        with self.connect() as conn:
            slot = self._slot(conn, slot_id)
        if slot["kind"] == "value":
            raise WorkbenchError("Only file and row slots reference sources.")
        out: list[dict] = []
        for source_id in slot["sources"]:
            source = self.catalog.try_source(source_id)
            if source is None:
                continue
            hits = source.search(q, limit, offset if len(slot["sources"]) == 1 else 0,
                                 extensions=slot["extensions"] or None, columns=slot["columns"] or None)
            out += [{"source": source_id, "source_name": source.name, **h} for h in hits]
        return out[:limit]

    # -- state computation
    def recompute(self, entry_ids=None, slot_ids=None, source_id=None) -> int:
        """Re-evaluates cells (all, or by entry, slot or referenced source). Returns the number of changed cells."""
        with self.lock:
            with self.connect() as conn:
                slots = {s["id"]: s for s in self.slots(conn)}
                cells = self._load_cells(conn, entry_ids, slot_ids, source_id)
                dup = self._duplicates(conn, slots)
                resolver = Resolver(self.catalog)
                groups: dict[tuple, set] = {}
                for c in cells:
                    slot = slots.get(c["slot_id"])
                    if slot and slot["kind"] != "value" and c["ref_item_id"] is not None and c["ref_source_id"] in slot["sources"]:
                        groups.setdefault((c["ref_source_id"], tuple(slot["columns"]) if slot["kind"] == "row" else ()), set()).add(c["ref_item_id"])
                fps: dict = {}
                for (sid, cols), ids in groups.items():
                    source = resolver.source(sid)
                    if source is not None:
                        fps[(sid, cols)] = source.fingerprints(ids, list(cols) or None)
                updates, affected = [], set()
                for c in cells:
                    slot = slots.get(c["slot_id"])
                    if slot is None:
                        continue
                    state, detail, label = evaluate(slot, c, resolver, fps)
                    if state == "ok" and slot["unique"] and c["slot_id"] in dup and self._identity(slot, c) in dup[c["slot_id"]]:
                        state, detail = "invalid", "duplicate: another entry has the same value"
                    affected.add(c["entry_id"])
                    new_label = label if label is not None else c["ref_label"]
                    if (state, detail, new_label) != (c["state"], c["state_detail"], c["ref_label"]):
                        updates.append((state, detail, new_label, c["entry_id"], c["slot_id"]))
                with Tx(conn):
                    conn.executemany("UPDATE cells SET state=?, state_detail=?, ref_label=? WHERE entry_id=? AND slot_id=?", updates)
                    self._update_entry_states(conn, None if entry_ids is None and slot_ids is None and source_id is None
                                              else sorted(affected))
        return len(updates)

    @staticmethod
    def _identity(slot: dict, cell):
        return cell["value"] if slot["kind"] == "value" else (cell["ref_source_id"], cell["ref_item_id"])

    @staticmethod
    def _duplicates(conn, slots: dict) -> dict:
        out = {}
        for slot_id, slot in slots.items():
            if not slot["unique"]:
                continue
            if slot["kind"] == "value":
                rows = conn.execute("SELECT value FROM cells WHERE slot_id=? AND value IS NOT NULL AND value NOT IN ('\"\"','null')"
                                    " GROUP BY value HAVING COUNT(*)>1", (slot_id,))
                out[slot_id] = {r["value"] for r in rows}
            else:
                rows = conn.execute("SELECT ref_source_id, ref_item_id FROM cells WHERE slot_id=? AND ref_item_id IS NOT NULL"
                                    " GROUP BY ref_source_id, ref_item_id HAVING COUNT(*)>1", (slot_id,))
                out[slot_id] = {(r[0], r[1]) for r in rows}
        return out

    @staticmethod
    def _load_cells(conn, entry_ids, slot_ids, source_id) -> list:
        where, args = [], []
        if slot_ids is not None:
            where.append(f"slot_id IN ({','.join('?' * len(slot_ids))})")
            args += list(slot_ids)
        if source_id is not None:
            where.append("ref_source_id=?")
            args.append(source_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        if entry_ids is None:
            return conn.execute(f"SELECT * FROM cells{clause}", args).fetchall()
        rows = []
        for ids in chunks(entry_ids, 400):
            extra = f"entry_id IN ({','.join('?' * len(ids))})"
            sql = f"SELECT * FROM cells{clause + ' AND ' if clause else ' WHERE '}{extra}"
            rows += conn.execute(sql, [*args, *ids]).fetchall()
        return rows

    @staticmethod
    def _update_entry_states(conn, entry_ids) -> None:
        sql = ("UPDATE entries SET state = CASE"
               " WHEN EXISTS (SELECT 1 FROM cells c WHERE c.entry_id=entries.id AND c.state IN ('source_missing','source_changed')) THEN 'corrupted'"
               " WHEN EXISTS (SELECT 1 FROM cells c WHERE c.entry_id=entries.id AND c.state IN ('incomplete','invalid')) THEN 'incomplete'"
               " ELSE 'valid' END")
        if entry_ids is None:
            conn.execute(sql)
        else:
            for ids in chunks(entry_ids, 400):
                conn.execute(f"{sql} WHERE id IN ({','.join('?' * len(ids))})", ids)

    def references(self, source_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM cells WHERE ref_source_id=? LIMIT 1", (source_id,)).fetchone() is not None

    def using_entries(self, source_id: str, item_ids: set[str] | None = None) -> set[int]:
        """Ids of the entries that reference the source (or only the given items of it)."""
        with self.connect() as conn:
            if item_ids is None:
                return {r[0] for r in conn.execute("SELECT DISTINCT entry_id FROM cells WHERE ref_source_id=?", (source_id,))}
            out: set[int] = set()
            for batch in chunks(item_ids):
                rows = conn.execute(f"SELECT DISTINCT entry_id FROM cells WHERE ref_source_id=? AND ref_item_id IN ({','.join('?' * len(batch))})",
                                    [source_id, *batch])
                out |= {r[0] for r in rows}
            return out

    def referenced_items(self, source_id: str) -> set[str]:
        with self.connect() as conn:
            return {r[0] for r in conn.execute("SELECT DISTINCT ref_item_id FROM cells WHERE ref_source_id=? AND ref_item_id IS NOT NULL", (source_id,))}

    def referenced_sources(self) -> list[str]:
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT DISTINCT ref_source_id FROM cells WHERE ref_source_id IS NOT NULL")]


class DatasetStore:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog

    def list(self) -> list[dict]:
        out = []
        for path in sorted(self.catalog.datasets_dir.iterdir(), key=lambda p: p.name):
            if path.is_dir() and (path / DATASET_DB).exists():
                try:
                    out.append(Dataset(self.catalog, path).summary())
                except Exception as e:
                    out.append({"id": path.name, "name": path.name, "error": str(e), "entries": 0,
                                "states": {"valid": 0, "incomplete": 0, "corrupted": 0}, "slots": 0})
        return sorted(out, key=lambda d: d["name"].lower())

    def get(self, dataset_id: str) -> Dataset:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", dataset_id or ""):
            raise NotFound("No such dataset.")
        path = self.catalog.datasets_dir / dataset_id
        if not (path / DATASET_DB).exists():
            raise NotFound("No such dataset.")
        return Dataset(self.catalog, path)

    def create(self, name: str) -> Dataset:
        return Dataset.create(self.catalog, name)

    def delete(self, dataset_id: str) -> None:
        dataset = self.get(dataset_id)
        with dataset.lock:
            for suffix in ("", "-wal", "-shm", "-journal"):
                (dataset.path / (DATASET_DB + suffix)).unlink(missing_ok=True)
            try:
                dataset.path.rmdir()
            except OSError:
                pass

    def datasets_referencing(self, source_id: str) -> list[Dataset]:
        out = []
        for summary in self.list():
            if "error" in summary:
                continue
            dataset = self.get(summary["id"])
            if dataset.references(source_id):
                out.append(dataset)
        return out
