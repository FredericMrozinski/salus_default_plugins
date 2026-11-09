"""Dataset automation: entries created from a source cohort, and fields derived from other fields by a regex.

Settings (kept in the dataset's `meta` table under "automation", so they live and die with the dataset):

    {"rules": {"<slot id>": {"mode": "source"}                                  # at most one, a file or row slot
               "<slot id>": {"mode": "derive", "from": <slot id>, "read": "<regex>", "value": "<template>"}}}

A slot without a rule is left empty ("leave empty" in the dialog).

* **source**: every item of the slot's source cohorts (files with an allowed extension, rows of a table) that no entry has in that
  slot yet becomes a new entry, with the item in that slot. Runs whenever a source is refreshed (scan, upload, ingest, the
  dataset check) and when the settings are saved.
* **derive**: `read` is searched (re.search, Python syntax, not anchored) in the text of the `from` slot: the relative path for a
  file, the row key for a row, the value for a value slot. The result is the `value` template with `\\1`..`\\99` and `\\g<name>`
  (or `\\g<0>` for the whole match) replaced; `\\\\` is a backslash. Unlike sed, the result is only the template, not the input
  with the match replaced. A value slot gets the text converted to its type; a file or row slot links the item the text names
  (a relative path or, without a folder, a unique file name; a row key) in one of its sources.
  Derivations chain: a slot may derive from a derived slot (cycles are refused). Only empty cells are filled, so a value typed
  by hand is never overwritten; a derivation that fails (no match, empty result, wrong type, no such item) leaves the cell
  empty and flags it "invalid" with the reason until the cell is filled (by hand, or by a later run that succeeds).
"""
from __future__ import annotations

import json
import re

from .util import Tx, WorkbenchError, meta_get, meta_set, now_iso

META_KEY = "automation"
ISSUES_TABLE = "automation_issues"
_TEMPLATE = re.compile(r"\\(?:(\d{1,2})|g<([^>]*)>|(\\)|(.)?)", re.S)


# --------------------------------------------------------------------------- the template language

def expand(template: str, match: re.Match) -> str:
    """`template` with group references replaced (see the module doc). Raises ValueError for an unknown group."""
    def one(m: re.Match) -> str:
        number, name, backslash, other = m.groups()
        if backslash:
            return "\\"
        if number is None and name is None:
            return "\\" + (other or "")  # any other backslash stays as it is
        ref = number if number is not None else name
        key: int | str = int(ref) if ref.isdigit() else ref
        try:
            return match.group(key) or ""
        except IndexError:  # an unknown group number or name
            raise ValueError(f"the read pattern has no group {ref}") from None
    return _TEMPLATE.sub(one, template)


def check_template(template: str, pattern: re.Pattern) -> None:
    for m in _TEMPLATE.finditer(template):
        number, name = m.group(1), m.group(2)
        if number is not None and int(number) > pattern.groups:
            raise ValueError(f"the read pattern has no group {number}")
        if name is not None and not (name.isdigit() and int(name) <= pattern.groups) and name not in pattern.groupindex:
            raise ValueError(f"the read pattern has no group {name or '(empty)'}")


def derive_text(read: str, template: str, text: str) -> str:
    """The derived text; ValueError says why there is none."""
    try:
        pattern = re.compile(read)
    except re.error as e:
        raise ValueError(f"invalid read pattern: {e}") from None
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"'{text}' does not match the read pattern")
    result = expand(template, match).strip()
    if not result:
        raise ValueError(f"the value template gives an empty text for '{text}'")
    return result


# --------------------------------------------------------------------------- settings

def _slot_text(slot: dict) -> str:
    return slot["label"] or slot["name"]


def normalize(raw, slots: list[dict]) -> dict:
    """Validates settings sent by the dialog. Raises WorkbenchError with a readable message."""
    if not isinstance(raw, dict) or not isinstance(raw.get("rules", {}), dict):
        raise WorkbenchError("Invalid automation settings.")
    by_id = {str(s["id"]): s for s in slots}
    rules: dict[str, dict] = {}
    for key, rule in (raw.get("rules") or {}).items():
        key = str(key)
        if key not in by_id:
            raise WorkbenchError("The automation names a field that is not in the schema (reload the dialog).")
        slot = by_id[key]
        if not isinstance(rule, dict):
            raise WorkbenchError(f"{_slot_text(slot)}: invalid rule.")
        mode = rule.get("mode") or "empty"
        if mode == "empty":
            continue
        if mode == "source":
            if slot["kind"] == "value":
                raise WorkbenchError(f"{_slot_text(slot)}: only a file or row field can be created from a source cohort.")
            rules[key] = {"mode": "source"}
        elif mode == "derive":
            origin = str(rule.get("from") or "")
            if origin not in by_id:
                raise WorkbenchError(f"{_slot_text(slot)}: choose the field to derive from.")
            if origin == key:
                raise WorkbenchError(f"{_slot_text(slot)}: a field cannot derive from itself.")
            read, template = str(rule.get("read") or ""), str(rule.get("value") or "")
            if not read:
                raise WorkbenchError(f"{_slot_text(slot)}: enter a read pattern.")
            if not template:
                raise WorkbenchError(f"{_slot_text(slot)}: enter a value template.")
            try:
                pattern = re.compile(read)
            except re.error as e:
                raise WorkbenchError(f"{_slot_text(slot)}: invalid read pattern: {e}") from None
            try:
                check_template(template, pattern)
            except ValueError as e:
                raise WorkbenchError(f"{_slot_text(slot)}: {e}") from None
            rules[key] = {"mode": "derive", "from": int(origin), "read": read, "value": template}
        else:
            raise WorkbenchError(f"{_slot_text(slot)}: unknown mode '{mode}'.")
    sources = [k for k, r in rules.items() if r["mode"] == "source"]
    if len(sources) > 1:
        raise WorkbenchError("Only one field can be created automatically from a source cohort.")
    order(rules, by_id)  # raises on a cycle
    return {"rules": rules}


def order(rules: dict, by_id: dict) -> list[str]:
    """The derive rules so that every slot comes after the slot it derives from. Raises WorkbenchError on a cycle."""
    derive = {k: str(r["from"]) for k, r in rules.items() if r["mode"] == "derive"}
    done: list[str] = []
    state: dict[str, int] = {}

    def visit(key: str, path: list[str]) -> None:
        if state.get(key) == 2:
            return
        if state.get(key) == 1:
            names = " → ".join(_slot_text(by_id[k]) if k in by_id else k for k in [*path, key])
            raise WorkbenchError(f"The derivations form a cycle: {names}.")
        state[key] = 1
        if key in derive:
            visit(derive[key], [*path, key])
            done.append(key)
        state[key] = 2

    for key in derive:
        visit(key, [])
    return done


def load(conn, slots: list[dict]) -> dict:
    """The stored settings, without rules whose field was deleted or no longer fits (schema edits)."""
    try:
        raw = json.loads(meta_get(conn, META_KEY) or "{}")
    except ValueError:
        raw = {}
    by_id = {str(s["id"]): s for s in slots}
    rules = {}
    for key, rule in (raw.get("rules") or {}).items():
        slot = by_id.get(str(key))
        if slot is None or not isinstance(rule, dict):
            continue
        if rule.get("mode") == "source" and slot["kind"] == "value":
            continue
        if rule.get("mode") == "derive" and str(rule.get("from")) not in by_id:
            continue
        rules[str(key)] = rule
    try:
        order(rules, by_id)
    except WorkbenchError:
        rules = {k: r for k, r in rules.items() if r.get("mode") != "derive"}
    return {"rules": rules}


def save(conn, config: dict) -> None:
    meta_set(conn, META_KEY, json.dumps(config))


def ensure_issues_table(conn) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {ISSUES_TABLE} (
        entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
        slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
        message TEXT NOT NULL, PRIMARY KEY (entry_id, slot_id))""")


def issues(conn) -> dict[tuple[int, int], str]:
    """{(entry id, slot id): why the derivation failed} (empty for datasets that never ran an automation)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ISSUES_TABLE,)).fetchone():
        return {}
    return {(r[0], r[1]): r[2] for r in conn.execute(f"SELECT entry_id, slot_id, message FROM {ISSUES_TABLE}")}


def clear_issue(conn, entry_id: int, slot_id: int) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ISSUES_TABLE,)).fetchone():
        conn.execute(f"DELETE FROM {ISSUES_TABLE} WHERE entry_id=? AND slot_id=?", (entry_id, slot_id))


# --------------------------------------------------------------------------- running

def _cell_text(slot: dict, cell: dict) -> str | None:
    """The text a derivation reads from a cell, or None when the cell is empty."""
    if slot["kind"] == "value":
        stored = None if cell["value"] is None else json.loads(cell["value"])
        if stored is None or stored == "":
            return None
        return ("true" if stored else "false") if isinstance(stored, bool) else str(stored)
    return cell["ref_label"] if cell["ref_item_id"] is not None else None


def _find_item(catalog, slot: dict, text: str):
    """(source id, item id) that `text` names in one of the slot's sources; ValueError says why there is none."""
    found = []
    for source_id in slot["sources"]:
        source = catalog.try_source(source_id)
        if source is not None:
            found += [(source_id, item) for item in source.lookup(text, slot["columns"] or None)]
    if not found:
        return None
    if len(found) > 1:
        raise ValueError(f"'{text}' names {len(found)} items; use the relative path")
    return found[0]


def _converted(slot: dict, text: str):
    from .dataset import coerce_value  # dataset imports this module
    return coerce_value(slot["value_type"], text)


def run(dataset) -> dict:
    """Applies the settings to the dataset: new entries from the source cohort, then the derivations. Returns counts."""
    result = {"created": 0, "derived": 0, "flagged": 0}
    with dataset.lock:
        with dataset.connect() as conn:
            slots = dataset.slots(conn)
            config = load(conn, slots)
        rules = config["rules"]
        if not rules:
            return result
        by_id = {str(s["id"]): s for s in slots}
        catalog = dataset.catalog

        # 1. one entry per source item that no entry has yet
        source_key = next((k for k, r in rules.items() if r["mode"] == "source"), None)
        new_items = []
        if source_key is not None:
            slot = by_id[source_key]
            with dataset.connect() as conn:
                present = {(r[0], r[1]) for r in conn.execute(
                    "SELECT ref_source_id, ref_item_id FROM cells WHERE slot_id=? AND ref_item_id IS NOT NULL", (slot["id"],))}
            for source_id in slot["sources"]:
                source = catalog.try_source(source_id)
                if source is None:
                    continue
                missing = [i for i in source.all_items(slot["extensions"] or None, slot["columns"] or None)
                           if (source_id, i) not in present]
                if missing:
                    fps = source.fingerprints(missing, slot["columns"] or None)
                    new_items += [(source_id, i, *fps[i]) for i in missing if i in fps]

        # 2. derivations, in an order where every field comes after the one it reads
        derive = [(by_id[k], rules[k]) for k in order(rules, by_id)]
        with dataset.connect() as conn, Tx(conn):
            ensure_issues_table(conn)
            now = now_iso()
            for source_id, item, fingerprint, label in new_items:
                entry_id = conn.execute("INSERT INTO entries(created_at, updated_at) VALUES(?,?)", (now, now)).lastrowid
                for s in slots:
                    default = s["constraints"].get("default")
                    conn.execute("INSERT INTO cells(entry_id, slot_id, value) VALUES(?,?,?)",
                                 (entry_id, s["id"], None if default is None or s["kind"] != "value" else json.dumps(default)))
                conn.execute("UPDATE cells SET ref_source_id=?, ref_item_id=?, ref_fingerprint=?, ref_label=? WHERE entry_id=? AND slot_id=?",
                             (source_id, item, fingerprint, label, entry_id, int(source_key)))
                result["created"] += 1
            if derive:
                cells: dict[int, dict[int, dict]] = {}
                wanted = {s["id"] for s, _ in derive} | {r["from"] for _, r in derive}
                for c in conn.execute(f"SELECT * FROM cells WHERE slot_id IN ({','.join('?' * len(wanted))})", sorted(wanted)):
                    cells.setdefault(c["entry_id"], {})[c["slot_id"]] = dict(c)
                for entry_id, row in cells.items():
                    for slot, rule in derive:
                        cell, origin = row.get(slot["id"]), row.get(rule["from"])
                        if cell is None or origin is None or _cell_text(slot, cell) is not None:
                            if cell is not None:
                                conn.execute(f"DELETE FROM {ISSUES_TABLE} WHERE entry_id=? AND slot_id=?", (entry_id, slot["id"]))
                            continue
                        text = _cell_text(by_id[str(rule["from"])], origin)
                        problem = None
                        if text is None:
                            conn.execute(f"DELETE FROM {ISSUES_TABLE} WHERE entry_id=? AND slot_id=?", (entry_id, slot["id"]))
                            continue
                        try:
                            derived = derive_text(rule["read"], rule["value"], text)
                            if slot["kind"] == "value":
                                value = json.dumps(_converted(slot, derived))
                                conn.execute("UPDATE cells SET value=? WHERE entry_id=? AND slot_id=?", (value, entry_id, slot["id"]))
                                cell["value"] = value
                            else:
                                hit = _find_item(catalog, slot, derived)
                                if hit is None:
                                    raise ValueError(f"no item '{derived}' in the field's sources")
                                source = catalog.try_source(hit[0])
                                fingerprint, label = source.fingerprints([hit[1]], slot["columns"] or None)[hit[1]]
                                conn.execute("UPDATE cells SET ref_source_id=?, ref_item_id=?, ref_fingerprint=?, ref_label=?"
                                             " WHERE entry_id=? AND slot_id=?", (hit[0], hit[1], fingerprint, label, entry_id, slot["id"]))
                                cell.update(ref_source_id=hit[0], ref_item_id=hit[1], ref_label=label)
                            result["derived"] += 1
                        except (ValueError, KeyError) as e:
                            problem = str(e).strip("'\"") if isinstance(e, KeyError) else str(e)
                        if problem:
                            conn.execute(f"INSERT INTO {ISSUES_TABLE}(entry_id, slot_id, message) VALUES(?,?,?)"
                                         " ON CONFLICT(entry_id, slot_id) DO UPDATE SET message=excluded.message",
                                         (entry_id, slot["id"], problem))
                            result["flagged"] += 1
                        else:
                            conn.execute(f"DELETE FROM {ISSUES_TABLE} WHERE entry_id=? AND slot_id=?", (entry_id, slot["id"]))
        dataset.recompute()
    return result


def preview(dataset, raw: dict, sample: str | None) -> dict:
    """What the settings in the dialog would give for one sample (not saved). Problems are reported per field, not raised.

    {samples: [texts of the first items of the source field], sample, error (settings as a whole), results: {slot id: {text, error}}}"""
    with dataset.connect() as conn:
        slots = dataset.slots(conn)
    by_id = {str(s["id"]): s for s in slots}
    out: dict = {"samples": [], "sample": sample, "error": None, "results": {}}
    rules = {str(k): r for k, r in ((raw or {}).get("rules") or {}).items() if isinstance(r, dict) and str(k) in by_id}
    source_key = next((k for k, r in rules.items() if r.get("mode") == "source"), None)
    if source_key is not None and by_id[source_key]["kind"] != "value":
        slot = by_id[source_key]
        for source_id in slot["sources"]:
            source = dataset.catalog.try_source(source_id)
            if source is None:
                continue
            ids = source.all_items(slot["extensions"] or None, slot["columns"] or None)[:20 - len(out["samples"])]
            fps = source.fingerprints(ids, slot["columns"] or None) if ids else {}
            out["samples"] += [fps[i][1] for i in ids if i in fps]
            if len(out["samples"]) >= 20:
                break
    if sample is None:
        sample = out["samples"][0] if out["samples"] else ""
        out["sample"] = sample
    try:
        normalize({"rules": rules}, slots)
    except WorkbenchError as e:
        out["error"] = e.message
    derive = {k: r for k, r in rules.items() if r.get("mode") == "derive"}
    texts: dict[str, str | None] = {}
    if source_key is not None:
        texts[source_key] = sample
    pending = list(derive)
    for _ in range(len(pending) + 1):  # resolves chains; whatever is left after that is a cycle or reads a field without a sample
        rest = []
        for key in pending:
            origin = str(derive[key].get("from") or "")
            if origin in derive and origin not in texts:
                rest.append(key)
                continue
            slot = by_id[key]
            if origin not in texts:
                texts[key] = None
                out["results"][key] = {"text": None, "error": None if origin in by_id else "choose a field to derive from",
                                       "note": "no sample for that field" if origin in by_id else None}
                continue
            if texts[origin] is None:
                texts[key] = None
                out["results"][key] = {"text": None, "error": None, "note": "the field it reads is empty"}
                continue
            try:
                text = derive_text(str(derive[key].get("read") or ""), str(derive[key].get("value") or ""), texts[origin])
                note = None
                if slot["kind"] == "value":
                    shown = _converted(slot, text)
                    text = ("true" if shown else "false") if isinstance(shown, bool) else str(shown)
                else:
                    hit = _find_item(dataset.catalog, slot, text)
                    note = "links an existing item" if hit else "no such item (yet): the field would be flagged"
                texts[key] = text
                out["results"][key] = {"text": text, "error": None, "note": note}
            except ValueError as e:
                texts[key] = None
                out["results"][key] = {"text": None, "error": str(e), "note": None}
        if not rest or len(rest) == len(pending):
            for key in rest:
                out["results"][key] = {"text": None, "error": "part of a cycle", "note": None}
            break
        pending = rest
    return out
