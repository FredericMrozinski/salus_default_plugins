"""The Pharaoh session: the list of slides, the plan for them and the run that carries it out.

State lives in the backend, so closing the panel does not stop a run and a reopened panel shows where things are.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import wsi
from errors import PharaohError

log = logging.getLogger("pharaoh")

COMPRESSIONS = ("jpeg", "webp", "deflate", "lzw", "zstd", "none")
LOSSY = ("jpeg", "webp")
TILE_SIZES = (128, 256, 512, 1024)
MODES = ("overwrite", "beside", "folder")

DEFAULT_TRANSFORM = {"pyramid": True, "compression": "jpeg", "quality": 80, "tile_size": 256, "bigtiff": True, "skip_pyramidal": False}
DEFAULT_OUTPUT = {"mode": "beside", "prefix": "", "suffix": "_pharaoh", "folder": "", "replace_existing": False, "keep_if_larger": True}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def clean_name_part(value, label: str) -> str:
    value = (value or "").strip() if isinstance(value, str) else ""
    if any(c in value for c in "/\\\0"):
        raise PharaohError(f"The {label} cannot contain slashes.")
    return value


def validate_transform(data: dict) -> dict:
    out = {**DEFAULT_TRANSFORM}
    if "compression" in data:
        if data["compression"] not in COMPRESSIONS:
            raise PharaohError("Unknown compression.")
        out["compression"] = data["compression"]
    if "quality" in data:
        try:
            quality = int(data["quality"])
        except (TypeError, ValueError):
            raise PharaohError("The quality must be a number from 1 to 100.") from None
        if not 1 <= quality <= 100:
            raise PharaohError("The quality must be a number from 1 to 100.")
        out["quality"] = quality
    if "tile_size" in data:
        if data["tile_size"] not in TILE_SIZES:
            raise PharaohError("Unsupported tile size.")
        out["tile_size"] = data["tile_size"]
    for flag in ("pyramid", "bigtiff", "skip_pyramidal"):
        if flag in data:
            out[flag] = bool(data[flag])
    return out


def validate_output(data: dict) -> dict:
    out = {**DEFAULT_OUTPUT}
    if "mode" in data:
        if data["mode"] not in MODES:
            raise PharaohError("Unknown way of saving.")
        out["mode"] = data["mode"]
    out["prefix"] = clean_name_part(data.get("prefix", out["prefix"]), "prefix")
    out["suffix"] = clean_name_part(data.get("suffix", out["suffix"]), "suffix")
    out["folder"] = str(data.get("folder", out["folder"]) or "").strip()
    for flag in ("replace_existing", "keep_if_larger"):
        if flag in data:
            out[flag] = bool(data[flag])
    if out["mode"] == "beside" and not (out["prefix"] or out["suffix"]):
        raise PharaohError("Give a prefix or a suffix, otherwise the new files would replace the originals.")
    if out["mode"] == "folder":
        if not out["folder"]:
            raise PharaohError("Enter the folder the new files go to.")
        folder = Path(out["folder"]).expanduser()
        if not folder.is_absolute():
            raise PharaohError("Enter the full path of the folder (it starts with / or ~).")
        if folder.exists() and not folder.is_dir():
            raise PharaohError("That path is a file, not a folder.")
        out["folder"] = str(folder)
    return out


class Item:
    def __init__(self, path: Path, root: Path | None):
        self.id = new_id()
        self.path = path
        self.root = root  # the folder it was found in (None: added as a file)
        self.rel = path.relative_to(root) if root else Path(path.name)
        try:
            self.size = path.stat().st_size
        except OSError:
            self.size = 0
        self.info: dict | None = None
        self.error: str | None = None
        # The run: pending | running | done | skipped | failed | aborted
        self.status = "pending"
        self.percent = 0.0
        self.message = ""
        self.out_path: str | None = None
        self.out_size: int | None = None

    def reset_run(self):
        self.status, self.percent, self.message, self.out_path, self.out_size = "pending", 0.0, "", None, None

    def to_json(self) -> dict:
        return {"id": self.id, "path": str(self.path), "rel": str(self.rel), "name": self.path.name, "size": self.size,
                "info": self.info, "error": self.error, "inspecting": self.info is None and self.error is None,
                "status": self.status, "percent": round(self.percent, 1), "message": self.message,
                "out_path": self.out_path, "out_size": self.out_size}


class Pharaoh:
    def __init__(self):
        self.items: dict[str, Item] = {}
        self.transform = dict(DEFAULT_TRANSFORM)
        self.output = dict(DEFAULT_OUTPUT)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="inspect")
        self.run_state = "idle"  # idle | running | finished | aborted
        self.started: float | None = None
        self.finished: float | None = None
        self.current: Item | None = None
        self._abort = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the list
    def _require_idle(self):
        if self.run_state == "running":
            raise PharaohError("A run is in progress. Stop it first.", 409)

    def add(self, path: str) -> dict:
        self._require_idle()
        target = Path(path).expanduser()
        if not target.is_absolute():
            raise PharaohError("Enter the full path (it starts with / or ~).")
        if target.is_dir():
            files = wsi.find_slides(target)
            root: Path | None = target
            if not files:
                raise PharaohError("There are no slide files (tif, svs, ndpi, ...) in that folder.")
        elif target.is_file():
            files, root = [target], None
        else:
            raise PharaohError("That path does not exist on the server.", 404)
        known = {item.path for item in self.items.values()}
        added = 0
        with self.lock:
            for file in files:
                if file in known:
                    continue
                item = Item(file, root)
                self.items[item.id] = item
                self.pool.submit(self._inspect, item)
                added += 1
            if self.run_state in ("finished", "aborted"):
                self._clear_run()
        return {"added": added, "already_listed": len(files) - added}

    def _inspect(self, item: Item) -> None:
        try:
            item.info = wsi.inspect(item.path)
        except ValueError as e:
            item.error = str(e)
        except OSError as e:
            item.error = str(e.strerror or e)

    def remove(self, ids: list[str]) -> None:
        self._require_idle()
        with self.lock:
            for item_id in ids:
                self.items.pop(item_id, None)
            if self.run_state in ("finished", "aborted"):
                self._clear_run()

    def clear(self) -> None:
        self._require_idle()
        with self.lock:
            self.items.clear()
            self._clear_run()

    # -- the plan
    def set_plan(self, transform: dict | None, output: dict | None) -> None:
        self._require_idle()
        new_transform = validate_transform(transform) if transform is not None else self.transform
        new_output = validate_output(output) if output is not None else self.output
        with self.lock:
            self.transform, self.output = new_transform, new_output
            if self.run_state in ("finished", "aborted"):
                self._clear_run()

    def target_for(self, item: Item, output: dict | None = None) -> Path:
        output = output or self.output
        if output["mode"] == "overwrite":
            return item.path
        if output["mode"] == "beside":
            return item.path.with_name(output["prefix"] + item.path.stem + output["suffix"] + item.path.suffix)
        return Path(output["folder"]) / item.rel

    def preview(self) -> list[dict]:
        """What a run would do with each file, without doing it."""
        rows, seen = [], {}
        with self.lock:
            items = list(self.items.values())
        skip_pyramidal = self.transform["skip_pyramidal"]
        for item in items:
            row = {"id": item.id, "name": item.path.name, "source": str(item.path), "target": None, "action": "write", "note": ""}
            if item.error:
                row.update(action="skip", note="Cannot be read: " + item.error)
            elif item.info is None:
                row.update(action="skip", note="Still being inspected")
            elif skip_pyramidal and item.info["levels"] > 1:
                row.update(action="skip", note=f"Already has {item.info['levels']} levels")
            else:
                target = self.target_for(item)
                row["target"] = str(target)
                if target in seen:
                    row.update(action="skip", note="Another file would be written to the same place")
                elif self.output["mode"] != "overwrite" and target.exists() and not self.output["replace_existing"]:
                    row.update(action="skip", note="The target already exists")
                elif self.output["mode"] != "overwrite" and target.exists():
                    row["note"] = "Replaces an existing file"
                seen[target] = item.id
            rows.append(row)
        return rows

    # -- the run
    def start(self) -> None:
        with self.lock:
            self._require_idle()
            if wsi.pyvips is None:
                raise PharaohError(f"libvips is not available ({wsi.IMPORT_ERROR}). Run backend/setup.sh.", 503)
            if not self.items:
                raise PharaohError("There are no files in the list.")
            if any(item.info is None and item.error is None for item in self.items.values()):
                raise PharaohError("The files are still being inspected. Try again in a moment.", 409)
            plan = {row["id"]: row for row in self.preview()}
            for item in self.items.values():
                item.reset_run()
                row = plan[item.id]
                if row["action"] == "skip":
                    item.status, item.message = "skipped", row["note"]
                else:
                    item.out_path = row["target"]
            self.run_state, self.started, self.finished, self.current = "running", time.time(), None, None
            self._abort.clear()
            self._thread = threading.Thread(target=self._run, name="pharaoh-run", daemon=True)
            self._thread.start()

    def abort(self) -> None:
        self._abort.set()

    def reset(self) -> None:
        """Back to planning after a run."""
        with self.lock:
            if self.run_state == "running":
                raise PharaohError("A run is in progress. Stop it first.", 409)
            self._clear_run()

    def _clear_run(self) -> None:
        self.run_state, self.started, self.finished, self.current = "idle", None, None, None
        for item in self.items.values():
            item.reset_run()

    def _run(self) -> None:
        try:
            for item in list(self.items.values()):
                if item.status != "pending":
                    continue
                if self._abort.is_set():
                    break
                self.current = item
                self._convert(item)
            for item in self.items.values():
                if item.status == "pending":
                    item.status, item.message = "aborted", "Not started"
        except Exception:
            log.exception("The run failed")
        finally:
            self.current = None
            self.finished = time.time()
            self.run_state = "aborted" if self._abort.is_set() else "finished"

    def _convert(self, item: Item) -> None:
        import pyvips
        target = Path(item.out_path)
        part = target.with_name(target.name + wsi.PART_SUFFIX)
        item.status, item.percent = "running", 0.0
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            options = self.transform
            image = pyvips.Image.new_from_file(str(item.path), access="sequential")
            if options["compression"] in LOSSY:
                if image.bands in (2, 4):
                    image = image.flatten(background=[255, 255, 255])
                if image.format == "ushort":
                    image = (image >> 8).cast("uchar")
            image.set_progress(True)

            def on_progress(img, progress):
                item.percent = progress.percent
                if self._abort.is_set():
                    img.set_kill(True)

            image.signal_connect("eval", on_progress)
            args = dict(compression=options["compression"], tile=True, tile_width=options["tile_size"],
                        tile_height=options["tile_size"], pyramid=options["pyramid"], bigtiff=options["bigtiff"])
            if options["compression"] in LOSSY:
                args["Q"] = options["quality"]
            if options["compression"] in ("deflate", "lzw", "zstd"):
                args["predictor"] = "horizontal"
            image.tiffsave(str(part), **args)
            item.out_size = part.stat().st_size
            if self.output["mode"] == "overwrite" and self.output["keep_if_larger"] and item.out_size >= item.size:
                part.unlink()
                item.status, item.out_path, item.out_size = "skipped", None, None
                item.message = "The result was not smaller, so the original was kept"
            else:
                os.replace(part, target)
                item.status, item.percent = "done", 100.0
        except Exception as e:
            part.unlink(missing_ok=True)
            if self._abort.is_set():
                item.status, item.message, item.out_path = "aborted", "Stopped", None
            else:
                log.warning("%s failed: %s", item.path, e)
                item.status, item.out_path = "failed", None
                item.message = str(e).strip().splitlines()[0] if str(e).strip() else "Failed"

    # -- the whole state
    def state(self) -> dict:
        with self.lock:
            items = [item.to_json() for item in self.items.values()]
            return {"items": items, "transform": self.transform, "output": self.output,
                    "run": {"state": self.run_state, "started": self.started, "finished": self.finished,
                            "current": self.current.id if self.current else None},
                    "engine": {"ok": wsi.pyvips is not None, "error": wsi.IMPORT_ERROR}}
