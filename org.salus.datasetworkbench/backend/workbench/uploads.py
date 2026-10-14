"""Chunked uploads into sources (see design section 6.5)."""
from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .sources import FileSource, Source, TableSource, hidden, is_internal
from .util import PART_SUFFIX, RESERVED_SOURCE_FILES, WorkbenchError, NotFound, new_id, safe_rel_path

CHUNK_SIZE = 8 * 1024 * 1024
DISK_MARGIN = 64 * 1024 * 1024
STALE_PART_SECONDS = 24 * 3600


@dataclass
class Upload:
    id: str
    source_id: str
    rel_path: str
    size: int
    part: Path
    final: Path
    overwrite: bool
    received: int = 0
    hasher: "hashlib._Hash" = field(default_factory=hashlib.sha256)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Uploads:
    def __init__(self, catalog):
        self.catalog = catalog
        self._uploads: dict[str, Upload] = {}

    # -- start
    def start(self, source_id: str, path: str, size: int, overwrite: bool = False) -> dict:
        source = self.catalog.find_source(source_id)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise WorkbenchError("Invalid size.")
        if str(path).startswith(("/", "\\")):
            raise WorkbenchError("The path must be relative to the source.")
        rel = safe_rel_path(path)
        for part in rel.split("/"):
            if hidden(part) or part in RESERVED_SOURCE_FILES or part.endswith(PART_SUFFIX):
                raise WorkbenchError(f"The name '{part}' is not allowed.")
        if isinstance(source, TableSource):
            rel = self._table_destination(source, rel, overwrite)
        final = source.path / rel
        if final.exists() and not overwrite:
            raise WorkbenchError(f"'{rel}' already exists in this source.", 409)
        if final.is_dir():
            raise WorkbenchError(f"'{rel}' is a folder.", 409)
        final.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(final.parent).free < size + DISK_MARGIN:
            raise WorkbenchError("Not enough free disk space.", 507)
        upload_id = new_id()
        part = final.with_name(f"{final.name}.upload-{upload_id}{PART_SUFFIX}")
        part.write_bytes(b"")
        self._uploads[upload_id] = Upload(upload_id, source_id, rel, size, part, final, overwrite)
        return {"upload_id": upload_id, "chunk_size": CHUNK_SIZE, "path": rel}

    @staticmethod
    def _table_destination(source: TableSource, rel: str, overwrite: bool) -> str:
        if "/" in rel:
            raise WorkbenchError("A table source keeps its CSV in its top folder.")
        if not rel.lower().endswith(".csv"):
            raise WorkbenchError("A table source takes a .csv file.")
        if len(source.csv_files()) > 1:
            source.the_csv()  # says that there are several CSV files
        existing = source.csv_file()
        if existing is None:
            return rel
        if not overwrite:
            raise WorkbenchError("This source already has a CSV file; choose to replace it.", 409)
        return existing.name  # the CSV is replaced in place, whatever the new file is called

    def _get(self, upload_id: str) -> Upload:
        upload = self._uploads.get(upload_id)
        if upload is None:
            raise NotFound("No such upload (the backend may have restarted); start it again.")
        return upload

    def check(self, source_id: str, paths: list) -> list[str]:
        """Which of the destination paths already exist (so that uploading them would overwrite something)."""
        source = self.catalog.find_source(source_id)
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise WorkbenchError("Invalid paths.")
        if isinstance(source, TableSource):
            if len(source.csv_files()) > 1:
                source.the_csv()  # says that there are several CSV files
            existing = source.csv_file()  # a table source keeps one CSV; a new one replaces it whatever it is called
            return [existing.name] if existing is not None and paths else []
        found = []
        for path in paths:
            rel = safe_rel_path(path)
            if (source.path / rel).exists():
                found.append(rel)
        return found

    # -- chunks
    def status(self, upload_id: str) -> dict:
        upload = self._get(upload_id)
        return {"received": upload.received, "size": upload.size}

    def append(self, upload_id: str, offset: int, data: bytes) -> dict:
        upload = self._get(upload_id)
        with upload.lock:
            if offset != upload.received:
                return {"received": upload.received, "resync": True}
            if upload.received + len(data) > upload.size:
                raise WorkbenchError("More data than announced.")
            with open(upload.part, "ab") as handle:
                handle.write(data)
            upload.hasher.update(data)
            upload.received += len(data)
            return {"received": upload.received}

    # -- finish
    def commit(self, upload_id: str) -> dict:
        upload = self._get(upload_id)
        with upload.lock:
            if upload.received != upload.size:
                raise WorkbenchError(f"Incomplete: {upload.received} of {upload.size} bytes received.")
            source = self.catalog.find_source(upload.source_id)
            with source.lock:
                if upload.final.exists() and not upload.overwrite:
                    raise WorkbenchError(f"'{upload.rel_path}' appeared in the meantime.", 409)
                os.replace(upload.part, upload.final)
            digest = upload.hasher.hexdigest()
            self._uploads.pop(upload_id, None)
        result = {"path": upload.rel_path, "size": upload.size, "hash": digest, "source": upload.source_id}
        if isinstance(source, FileSource):
            result["item"] = source.register(upload.rel_path, digest)
        return result

    def abort(self, upload_id: str) -> None:
        upload = self._uploads.pop(upload_id, None)
        if upload:
            upload.part.unlink(missing_ok=True)

    # -- housekeeping
    def clean_stale_parts(self) -> int:
        removed = 0
        cutoff = time.time() - STALE_PART_SECONDS
        active = {u.part for u in self._uploads.values()}
        for dirpath, _dirs, files in os.walk(self.catalog.sources_dir):
            for name in files:
                if name.endswith(PART_SUFFIX) and ".upload-" in name:
                    path = Path(dirpath) / name
                    try:
                        if path not in active and path.stat().st_mtime < cutoff:
                            path.unlink()
                            removed += 1
                    except OSError:
                        pass
        return removed
