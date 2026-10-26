"""The workbench as the plugin backend uses it: catalog, datasets, uploads, background jobs and events."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from .catalog import Catalog
from .dataset import DatasetStore
from .sources import FileSource, TableSource
from .uploads import Uploads
from .util import WorkbenchError, new_id

log = logging.getLogger("workbench")
Notify = Callable[[dict], None]


class Workbench:
    def __init__(self, root: Path, notify: Notify | None = None, background: bool = True):
        self.catalog = Catalog(root)
        self.datasets = DatasetStore(self.catalog)
        self.uploads = Uploads(self.catalog)
        self.notify: Notify = notify or (lambda event: None)
        self.background = background  # tests run jobs inline
        self.state = {"dataset": None, "entry": None}
        self._running: set[tuple] = set()
        self._guard = threading.Lock()

    # -- jobs
    def start_job(self, kind: str, target: str, work: Callable[[Callable[[dict], None]], dict]) -> dict:
        key = (kind, target)
        with self._guard:
            if key in self._running:
                return {"job": None, "running": True}
            self._running.add(key)
        job = new_id()

        def progress(info: dict) -> None:
            self.notify({"type": "job", "job": job, "kind": kind, "target": target, **info})

        def run() -> None:
            result: dict = {}
            try:
                result = work(progress) or {}
                self.notify({"type": "job_done", "job": job, "kind": kind, "target": target, "ok": True, **result})
            except WorkbenchError as e:
                self.notify({"type": "job_done", "job": job, "kind": kind, "target": target, "ok": False, "error": e.message})
            except Exception as e:
                log.exception("job %s %s failed", kind, target)
                self.notify({"type": "job_done", "job": job, "kind": kind, "target": target, "ok": False, "error": str(e)})
            finally:
                with self._guard:
                    self._running.discard(key)

        if self.background:
            threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
        else:
            run()
        return {"job": job, "running": False}

    # -- sources
    def scan_source(self, source_id: str) -> dict:
        source = self.catalog.find_source(source_id)

        def work(progress):
            if isinstance(source, FileSource):
                result = source.scan(progress)
            else:
                if source.declared_columns():
                    result = source.ingest(progress)
                else:
                    result = {}
            self.source_changed(source_id)
            return result

        return self.start_job("scan", source_id, work)

    def scan_all(self) -> dict:
        """Starts a scan of every source (the Refresh button); sources that are being scanned already are skipped."""
        started = 0
        for source_id in self.catalog.source_ids():
            try:
                if not self.scan_source(source_id)["running"]:
                    started += 1
            except WorkbenchError:
                continue  # a broken source must not keep the others from being read
        return {"started": started, "running": self.running_jobs()}

    def running_jobs(self) -> int:
        with self._guard:
            return len(self._running)

    def source_changed(self, source_id: str) -> None:
        """After a scan, an ingest or an upload: refreshes the states of all cells that reference the source."""
        self.notify({"type": "source_changed", "source": source_id})
        for dataset in self.datasets.datasets_referencing(source_id):
            dataset.recompute(source_id=source_id)
            self.notify({"type": "dataset_changed", "dataset": dataset.id})

    def configure_table(self, source_id: str, columns: list, auto_key: bool = False) -> dict:
        source = self.catalog.find_source(source_id)
        if not isinstance(source, TableSource):
            raise WorkbenchError("Only table sources have columns.")
        source.inspect()  # fails early with a readable message when there is no usable CSV

        def work(progress):
            result = source.configure(columns, progress, auto_key)
            self.source_changed(source_id)
            return result

        return self.start_job("ingest", source_id, work)

    def import_csv(self, source_id: str, path: str) -> dict:
        import shutil
        source = self.catalog.find_source(source_id)
        if not isinstance(source, TableSource):
            raise WorkbenchError("Only table sources take a CSV.")
        origin = Path(path).expanduser()
        if not origin.is_file() or origin.suffix.lower() != ".csv":
            raise WorkbenchError("That is not a CSV file on the server.")
        files = source.csv_files()
        if len(files) > 1:
            source.the_csv()  # says that there are several
        shutil.copyfile(origin, source.path / (files[0].name if files else origin.name))
        self.csv_arrived(source_id)
        return source.info()

    def csv_arrived(self, source_id: str) -> None:
        """A CSV was uploaded or imported: re-ingest if the source is set up, otherwise wait for the setup."""
        source = self.catalog.find_source(source_id)
        if isinstance(source, TableSource) and source.declared_columns():
            self.start_job("ingest", source_id, lambda progress: (source.ingest(progress), self.source_changed(source_id))[0])
        self.notify({"type": "source_changed", "source": source_id})

    def finish_upload(self, upload_id: str) -> dict:
        result = self.uploads.commit(upload_id)
        source = self.catalog.find_source(result["source"])
        if isinstance(source, TableSource):
            self.csv_arrived(source.id)
        else:
            self.source_changed(source.id)
        self.notify({"type": "tree_changed"})
        return result

    # -- usage, renaming, deleting
    def usage(self, targets: list[tuple[str, set[str] | None]]) -> dict:
        """Which dataset entries use the given sources (or items of them): {entries, datasets: [{id, name, entries}]}."""
        per_dataset: dict[str, set[int]] = {}
        names: dict[str, str] = {}
        for source_id, item_ids in targets:
            for dataset in self.datasets.datasets_referencing(source_id):
                found = dataset.using_entries(source_id, item_ids)
                if found:
                    per_dataset.setdefault(dataset.id, set()).update(found)
                    names[dataset.id] = dataset.summary()["name"]
        return {"entries": sum(len(v) for v in per_dataset.values()),
                "datasets": [{"id": k, "name": names[k], "entries": len(v)} for k, v in per_dataset.items()]}

    def source_usage(self, source_id: str, path: str | None = None) -> dict:
        source = self.catalog.find_source(source_id)
        return self.usage([(source_id, source.item_ids_under(path) if path else None)])

    def group_usage(self, rel: str) -> dict:
        return self.usage([(sid, None) for sid in self.catalog.group_contents(rel)["sources"]])

    def rename_item(self, source_id: str, path: str, name: str) -> str:
        source = self.catalog.find_source(source_id)
        new_path = source.rename_item(path, name)
        self.source_changed(source_id)  # entries pick up the new file name; nothing else changes
        self.notify({"type": "tree_changed"})
        return new_path

    def delete_item(self, source_id: str, path: str) -> None:
        source = self.catalog.find_source(source_id)
        referenced: set[str] = set()
        for dataset in self.datasets.datasets_referencing(source_id):
            referenced |= dataset.referenced_items(source_id)
        source.delete_item(path, referenced)
        self.source_changed(source_id)
        self.notify({"type": "tree_changed"})

    def refresh_names(self) -> None:
        """After a group or source was renamed: datasets show the new names."""
        self.notify({"type": "tree_changed"})
        for summary in self.datasets.list():
            self.notify({"type": "dataset_changed", "dataset": summary["id"]})

    # -- datasets
    def check_dataset(self, dataset_id: str) -> dict:
        dataset = self.datasets.get(dataset_id)

        def work(progress):
            for source_id in dataset.referenced_sources():
                source = self.catalog.try_source(source_id)
                if source is not None:
                    source.refresh(progress)
            dataset.recompute()
            self.notify({"type": "dataset_changed", "dataset": dataset.id})
            return {}

        return self.start_job("check", dataset_id, work)

    def set_state(self, dataset, entry) -> dict:
        self.state = {"dataset": dataset, "entry": entry}
        return self.state
