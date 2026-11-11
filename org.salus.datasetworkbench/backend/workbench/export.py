"""Exporting a dataset: its entries as one JSON file, optionally with copies of the source files they use.

The options (what the export wizard asks, `normalize_options`):

    include      {"incomplete": bool, "corrupted": bool}: valid entries are always exported
    keys         {slot id: key}: the key of each field in the exported entries (default: the field's name)
    copy_sources True: the files (and the CSVs of table cohorts) the exported entries use are copied into the export,
                 under sources/<the cohort's folder in the workbench>/, and the JSON names them by that relative path
    paths        without copies, how a file is named: "absolute" (its full path on this server) or "relative" (its
                 path inside its cohort, after the cohort's prefix in `prefixes`, if one is set)
    prefixes     {source id: prefix}
    format       "folder" (dataset.json and sources/ in a folder), "zip" (the same in a zip file) or "json" (only the
                 JSON file; not with copies)
    destination  an absolute path on this server

Everything is written next to the destination under a temporary name first and then renamed into place, so an
interrupted or failed export leaves the destination as it was. Files and folders are created the ordinary way (never
private temporary files, and copies do not take over the original's mode), so the process' umask decides who can
read them: Salus sets it for shared group folders.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Callable

from .sources import FileSource, TableSource, q
from .util import WorkbenchError, chunks, new_id, now_iso

EXPORT_FORMAT = "salus.datasetworkbench.export"
EXPORT_VERSION = 1
JSON_NAME = "dataset.json"
FORMATS = ("folder", "zip", "json")
EXTENSIONS = {"folder": "", "zip": ".zip", "json": ".json"}
CHUNK = 8 * 1024 * 1024
Progress = Callable[[dict], None]


class Cancelled(WorkbenchError):
    def __init__(self):
        super().__init__("The export was cancelled.", 409)


# --------------------------------------------------------------------------- options

def default_destination(dataset, fmt: str) -> str:
    return str(Path(os.path.abspath(dataset.catalog.root)) / "exports" / dataset.id) + EXTENSIONS.get(fmt, "")


def normalize_options(raw, dataset, slots: list[dict]) -> dict:
    if not isinstance(raw, dict):
        raise WorkbenchError("Invalid export options.")
    include = raw.get("include") or {}
    copy = bool(raw.get("copy_sources"))
    fmt = raw.get("format") or "folder"
    if fmt not in FORMATS:
        raise WorkbenchError("The format is folder, zip or json.")
    if copy and fmt == "json":
        raise WorkbenchError("Copies of the source files need a folder or a zip file, not a single JSON file.")
    paths = raw.get("paths") or "absolute"
    if paths not in ("absolute", "relative"):
        raise WorkbenchError("Paths are absolute or relative.")

    given = raw.get("keys") or {}
    if not isinstance(given, dict):
        raise WorkbenchError("Invalid keys.")
    keys: dict[str, str] = {}
    for slot in slots:
        key = given.get(str(slot["id"]))
        key = slot["name"] if key is None else str(key).strip()
        if not key:
            raise WorkbenchError(f"The field '{slot['name']}' needs a key.")
        if len(key) > 200:
            raise WorkbenchError(f"The key of '{slot['name']}' is too long (at most 200 characters).")
        keys[str(slot["id"])] = key
    seen: dict[str, str] = {}
    for slot in slots:
        key = keys[str(slot["id"])]
        if key in seen:
            raise WorkbenchError(f"'{seen[key]}' and '{slot['name']}' would both be exported as '{key}': keys must differ.")
        seen[key] = slot["name"]

    prefixes = raw.get("prefixes") or {}
    if not isinstance(prefixes, dict):
        raise WorkbenchError("Invalid prefixes.")
    return {
        "include": {"incomplete": bool(include.get("incomplete")), "corrupted": bool(include.get("corrupted"))},
        "keys": keys,
        "copy_sources": copy,
        "paths": paths,
        "prefixes": {str(k): str(v).strip() for k, v in prefixes.items() if str(v).strip()},
        "format": fmt,
        "destination": str(raw.get("destination") or "").strip() or default_destination(dataset, fmt),
    }


def _join(prefix: str, rel: str) -> str:
    if not prefix:
        return rel
    return prefix + rel if prefix.endswith(("/", "\\")) else f"{prefix}/{rel}"


# --------------------------------------------------------------------------- destination

def check_destination(dataset, options: dict) -> dict:
    """{path, exists, replace: None | "file" | "folder", error}: where the export goes and what is there already."""
    text = os.path.expanduser(options["destination"])
    out = {"path": text, "exists": False, "replace": None, "error": None}
    if not os.path.isabs(text):
        out["error"] = "Give an absolute path (starting with /)."
        return out
    path = Path(os.path.normpath(text))
    out["path"] = str(path)
    if path.parent == path:
        out["error"] = "Choose a folder or file below /."
        return out
    real = path.resolve()
    catalog = dataset.catalog
    for inner, what in ((catalog.sources_dir, "the source cohorts"), (catalog.datasets_dir, "the datasets")):
        inner = inner.resolve()
        if real == inner or inner in real.parents or real in inner.parents:
            out["error"] = f"The export cannot go into the workbench folder of {what}."
            return out
    if not path.exists():
        if path.parent.exists() and not path.parent.is_dir():
            out["error"] = f"{path.parent} is a file, not a folder."
        return out
    if options["format"] == "folder":
        if not path.is_dir():
            out["error"] = "A file with this name exists. Choose another folder name."
        elif not any(path.iterdir()):
            out["exists"] = True  # an empty folder: nothing is lost
        elif (path / JSON_NAME).is_file():
            out.update(exists=True, replace="folder")
        else:
            out["error"] = "This folder is not empty and holds no earlier export. Choose a new folder."
    else:
        if path.is_dir():
            out["error"] = "A folder with this name exists. Choose a file name."
        else:
            out.update(exists=True, replace="file")
    return out


# --------------------------------------------------------------------------- what is exported

class Plan:
    """The exported JSON document and the files to copy into the export."""

    def __init__(self):
        self.document: dict = {}
        self.copies: list[tuple[Path, str, int]] = []  # (file, path in the export, size)
        self.counts = {"valid": 0, "incomplete": 0, "corrupted": 0}
        self.missing = 0  # referenced files or rows that are gone (exported as null)
        self.sources: list[dict] = []

    @property
    def bytes(self) -> int:
        return sum(size for _, _, size in self.copies)


def _file_items(source: FileSource, item_ids) -> dict:
    """{item id: (relative path, size)} of the items that exist."""
    found = {}
    with source.connect() as conn:
        for ids in chunks(list(item_ids)):
            for r in conn.execute(f"SELECT id, rel_path, size FROM items WHERE status!='missing' AND id IN ({','.join('?' * len(ids))})", ids):
                found[r["id"]] = (r["rel_path"], r["size"] or 0)
    return found


def _rows(source: TableSource, keys, columns) -> dict:
    """{row key: {column: value}} of the rows that exist."""
    cols = source._columns_for(columns)
    if not cols or not source._has_data():
        return {}
    found = {}
    with source.connect() as conn:
        for ids in chunks(list(keys)):
            for r in conn.execute(f"SELECT _key, {', '.join(q(c) for c in cols)} FROM data WHERE _key IN ({','.join('?' * len(ids))})", ids):
                found[r["_key"]] = {c: r[c] for c in cols}
    return found


def plan(dataset, options: dict) -> Plan:
    out = Plan()
    catalog = dataset.catalog
    states = ["valid"] + [s for s in ("incomplete", "corrupted") if options["include"][s]]
    marks = ",".join("?" * len(states))
    with dataset.connect() as conn:
        slots = dataset.slots(conn)
        out.counts.update({r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM entries GROUP BY state")})
        conn.execute("BEGIN")  # one snapshot: entries and cells as of the same moment
        try:
            entry_ids = [r[0] for r in conn.execute(f"SELECT id FROM entries WHERE state IN ({marks}) ORDER BY id", states)]
            cells: dict[int, dict] = {i: {} for i in entry_ids}
            for ids in chunks(entry_ids, 400):
                for c in conn.execute(f"SELECT * FROM cells WHERE entry_id IN ({','.join('?' * len(ids))})", ids):
                    cells[c["entry_id"]][c["slot_id"]] = c
        finally:
            conn.execute("COMMIT")

    # The referenced items, looked up once per source.
    wanted: dict[tuple[str, tuple], set] = {}
    for by_slot in cells.values():
        for slot in slots:
            c = by_slot.get(slot["id"])
            if slot["kind"] != "value" and c is not None and c["ref_item_id"] is not None:
                cols = tuple(slot["columns"]) if slot["kind"] == "row" else ()
                wanted.setdefault((c["ref_source_id"], cols), set()).add(c["ref_item_id"])
    sources: dict[str, object] = {}
    found: dict[tuple[str, tuple], dict] = {}
    for (source_id, cols), ids in wanted.items():
        source = sources.setdefault(source_id, catalog.try_source(source_id))
        if isinstance(source, FileSource) and not cols:
            found[(source_id, cols)] = _file_items(source, ids)
        elif isinstance(source, TableSource):
            found[(source_id, cols)] = _rows(source, ids, list(cols) or None)
        else:
            found[(source_id, cols)] = {}

    # The cohorts every field may use, so the JSON says where each one is (also when no exported entry uses it).
    for slot in slots:
        for source_id in slot["sources"]:
            if source_id not in sources:
                sources[source_id] = catalog.try_source(source_id)

    def cohort_dir(source) -> str:
        try:
            return Path(source.path).resolve().relative_to(catalog.sources_dir.resolve()).as_posix()
        except ValueError:
            return Path(source.path).name

    def file_path(source_id: str, source, rel: str) -> str:
        if options["copy_sources"]:
            return f"sources/{cohort_dir(source)}/{rel}"
        if options["paths"] == "absolute":
            return os.path.abspath(source.path / rel)
        return _join(options["prefixes"].get(source_id, ""), rel)

    copied: set[tuple[str, str]] = set()
    entries = []
    for entry_id in entry_ids:
        values = {}
        for slot in slots:
            key = options["keys"][str(slot["id"])]
            c = cells[entry_id].get(slot["id"])
            value = None
            if c is None:
                pass
            elif slot["kind"] == "value":
                value = None if c["value"] is None else json.loads(c["value"])
                if value == "":
                    value = None
            elif c["ref_item_id"] is not None:
                source = sources.get(c["ref_source_id"])
                cols = tuple(slot["columns"]) if slot["kind"] == "row" else ()
                hit = found.get((c["ref_source_id"], cols), {}).get(c["ref_item_id"])
                if source is None or hit is None:
                    out.missing += 1
                elif slot["kind"] == "file":
                    rel, size = hit
                    value = file_path(c["ref_source_id"], source, rel)
                    if options["copy_sources"] and (c["ref_source_id"], rel) not in copied:
                        copied.add((c["ref_source_id"], rel))
                        out.copies.append((Path(source.path) / rel, value, size))
                else:
                    value = {"key": c["ref_item_id"], "values": hit}
            values[key] = value
        entries.append(values)

    cohorts = []
    for source_id, source in sources.items():
        if source is None:
            continue
        info = {"name": source.name, "kind": source.kind}
        if options["copy_sources"]:
            info["location"] = f"sources/{cohort_dir(source)}"
            if isinstance(source, TableSource):
                csv_file = source.csv_file()
                if csv_file is not None:
                    info["csv"] = f"sources/{cohort_dir(source)}/{csv_file.name}"
                    out.copies.append((csv_file, info["csv"], csv_file.stat().st_size))
        elif options["paths"] == "absolute":
            info["location"] = os.path.abspath(source.path)
        else:
            info["location"] = options["prefixes"].get(source_id) or None
        cohorts.append({"id": source_id, "folder": cohort_dir(source), **info})
    out.sources = cohorts

    name_of = {sid: (s.name if s is not None else None) for sid, s in sources.items()}
    out.document = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "dataset": {"id": dataset.id, "name": dataset.summary()["name"]},
        "exported_at": now_iso(),
        "states": states,
        "paths": "copied" if options["copy_sources"] else options["paths"],
        "fields": [{"key": options["keys"][str(s["id"])], "name": s["name"], "label": s["label"], "kind": s["kind"],
                    "type": s["value_type"], "required": s["required"],
                    "sources": [name_of.get(sid) for sid in s["sources"] if name_of.get(sid)],
                    **({"columns": s["columns"]} if s["kind"] == "row" and s["columns"] else {})} for s in slots],
        "sources": [{"name": c["name"], "kind": c["kind"], "location": c["location"], **({"csv": c["csv"]} if "csv" in c else {})}
                    for c in cohorts],
        "entries": entries,
    }
    return out


def preview(dataset, raw) -> dict:
    """What the wizard shows: the entry counts, the fields and their keys, the cohorts, how much would be copied, where the
    export would go, and the first exported entry. Invalid options give `error` instead of raising."""
    slots = dataset.slots()
    out: dict = {"fields": [{"slot": s["id"], "name": s["name"], "label": s["label"], "kind": s["kind"]} for s in slots],
                 "error": None}
    try:
        options = normalize_options(raw, dataset, slots)
    except WorkbenchError as e:
        out["error"] = e.message
        try:  # say the rest anyway, as if the keys were the field names (what is wrong is usually a key)
            options = normalize_options({**raw, "keys": {}}, dataset, slots)
        except (WorkbenchError, TypeError):
            return out
    result = plan(dataset, options)
    exported = sum(result.counts[s] for s in result.document["states"])
    sample = result.document["entries"][:1]
    out.update({
        "counts": result.counts,
        "entries": exported,
        "keys": options["keys"],
        "sources": result.sources,
        "files": len(result.copies),
        "bytes": result.bytes,
        "missing": result.missing,
        "destination": check_destination(dataset, options),
        "default_destination": default_destination(dataset, options["format"]),
        "free": _free_space(options["destination"]),
        "sample": sample[0] if sample else None,
    })
    return out


def _free_space(destination: str) -> int | None:
    path = Path(os.path.expanduser(destination))
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


# --------------------------------------------------------------------------- writing

def run(dataset, raw, overwrite: bool, progress: Progress | None = None, cancel: threading.Event | None = None) -> dict:
    """Writes the export. Raises WorkbenchError if the destination is taken (and `overwrite` is not set), cannot be used,
    or has not enough space; `Cancelled` if `cancel` was set. Returns {path, entries, files, bytes, missing}."""
    progress = progress or (lambda info: None)
    cancel = cancel or threading.Event()
    options = normalize_options(raw, dataset, dataset.slots())
    dest = check_destination(dataset, options)
    if dest["error"]:
        raise WorkbenchError(dest["error"])
    if dest["replace"] and not overwrite:
        raise WorkbenchError(f"{dest['path']} exists already.", 409)
    progress({"phase": "collect"})
    result = plan(dataset, options)
    target = Path(dest["path"])
    total = result.bytes
    free = _free_space(str(target))
    if free is not None and total + 16 * 1024 * 1024 > free:
        raise WorkbenchError(f"Not enough space: the export needs {_size(total)}, {_size(free)} are free there.", 507)

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.part-{new_id()}"
    text = json.dumps(result.document, indent=2, ensure_ascii=False)
    state = {"bytes": 0, "files": 0}

    def report():
        progress({"phase": "copy", "done": state["files"], "total": len(result.copies), "bytes": state["bytes"], "total_bytes": total})

    def pump(src, dst):
        while True:
            if cancel.is_set():
                raise Cancelled()
            block = src.read(CHUNK)
            if not block:
                return
            dst.write(block)
            state["bytes"] += len(block)
            report()

    try:
        if options["format"] == "json":
            with open(temp, "x", encoding="utf-8") as handle:
                handle.write(text)
        elif options["format"] == "zip":
            with zipfile.ZipFile(temp, "x", allowZip64=True) as archive:
                archive.writestr(JSON_NAME, text, compress_type=zipfile.ZIP_DEFLATED)
                for path, arcname, _ in result.copies:
                    info = zipfile.ZipInfo.from_file(path, arcname)
                    info.compress_type = zipfile.ZIP_STORED  # slides and images are compressed already: deflating only costs time
                    with open(path, "rb") as src, archive.open(info, "w", force_zip64=True) as dst:
                        pump(src, dst)
                    state["files"] += 1
                    report()
        else:
            temp.mkdir()
            (temp / JSON_NAME).write_text(text, encoding="utf-8")
            for path, rel, _ in result.copies:
                dst_path = temp / rel
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "rb") as src, open(dst_path, "xb") as dst:
                    pump(src, dst)
                st = os.stat(path)
                os.utime(dst_path, ns=(st.st_atime_ns, st.st_mtime_ns))  # the time, not the mode: the umask decides that
                state["files"] += 1
                report()
        if cancel.is_set():
            raise Cancelled()
        progress({"phase": "finish"})
        _put_in_place(temp, target, options["format"] == "folder")
    except BaseException:
        _remove(temp)
        raise
    return {"path": str(target), "entries": len(result.document["entries"]), "files": len(result.copies),
            "bytes": total, "missing": result.missing}


def _put_in_place(temp: Path, target: Path, folder: bool) -> None:
    if not folder:
        os.replace(temp, target)
        return
    if not target.exists():
        os.rename(temp, target)
        return
    old = target.parent / f".{target.name}.old-{new_id()}"
    os.rename(target, old)
    try:
        os.rename(temp, target)
    except OSError:
        os.rename(old, target)
        raise
    _remove(old)


def _remove(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"
