"""The workbench as the plugin backend uses it: catalog, datasets, uploads, background jobs and events."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from . import export
from .catalog import Catalog
from .dataset import DatasetStore
from .sources import FileSource, TableSource
from .uploads import Uploads
from .util import WorkbenchError, new_id

log = logging.getLogger("workbench")
Notify = Callable[[dict], None]
SETTLE_DELAY = 0.5  # seconds a burst of uploads (or deletes) is collected before the datasets are brought up to date once
SETTLE_IDLE_EXIT = 30.0


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
        self._exports: dict[str, dict] = {}  # by dataset: the last export's {state, progress, result, error, cancel}
        self._settle_pending: set[str] = set()  # sources whose datasets are to be brought up to date (source_changed_later)
        self._settle_cond = threading.Condition()
        self._settler: threading.Thread | None = None
        self._settling = False  # the worker is updating datasets right now

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
        return self.start_job("scan", source_id, lambda progress: self._scan(source_id, source, progress))

    def _scan(self, source_id: str, source, progress) -> dict:
        """Reads one source again (files: a scan; a table: its CSV again) and updates what references it."""
        if isinstance(source, FileSource):
            result = source.scan(progress, self.referenced_items(source_id))
        elif source.declared_columns():
            result = source.ingest(progress)
        else:
            result = {}
        if progress:
            progress({"phase": "datasets"})
        self.source_changed(source_id)
        return result

    def scan_all(self) -> dict:
        """The Refresh button: one job ("refresh") that scans the sources one after the other, not all at once (on a
        large or remote filesystem, parallel walks only compete for the disk and for the backend's threads).

        Its progress events carry {phase, index, count, source, files}: the source being read (index of count, from 1),
        and how many files it has seen so far. A source that is being scanned on its own already is skipped.
        Returns {started: sources to scan, running}; a second Refresh while one runs starts nothing."""
        source_ids = self.catalog.source_ids()

        def work(progress):
            done = skipped = 0
            for index, source_id in enumerate(source_ids, 1):
                key = ("scan", source_id)
                with self._guard:
                    if key in self._running:
                        skipped += 1
                        continue
                    self._running.add(key)
                try:
                    source = self.catalog.find_source(source_id)
                    name = source.info().get("name", "")
                    progress({"phase": "scan", "index": index, "count": len(source_ids), "source": name, "files": 0})
                    self._scan(source_id, source,
                               lambda info: progress({**info, "index": index, "count": len(source_ids), "source": name}))
                    done += 1
                except WorkbenchError:
                    continue  # a broken source must not keep the others from being read
                finally:
                    with self._guard:
                        self._running.discard(key)
            return {"scanned": done, "skipped": skipped}

        job = self.start_job("refresh", "all", work)
        return {"started": 0 if job["running"] else len(source_ids), "running": self.running_jobs(), "job": job["job"]}

    def running_jobs(self) -> int:
        with self._guard:
            return len(self._running)

    def source_changed(self, source_id: str) -> None:
        """After a scan or an ingest (in its job): refreshes the states of all cells that reference the source.

        The files' fingerprints come from the source's index, which the scan (or the upload) has just brought up to date:
        looking at every referenced file again would read the disk a second time, which on a slow disk kept the backend
        busy long after a Refresh. Opening a dataset still checks the files themselves (check_dataset)."""
        self.notify({"type": "source_changed", "source": source_id})
        self._update_datasets(source_id)

    def source_changed_later(self, source_id: str) -> None:
        """Like source_changed, for requests (an upload's commit, a rename, a delete): the file lists are told now, the
        datasets are brought up to date in the background. That recomputes every entry that uses the source and runs the
        automation of the datasets that read it, which takes seconds on a large dataset: done inside each upload's commit,
        the commits of a folder upload queued up behind each other (one at a time, under the dataset's lock) until they
        and everything else waiting for the backend's threads ran into Salus' 30 s timeout (504). Changes that arrive
        while the worker waits or works are collected, so a folder of uploads costs one update, not one per file."""
        self.notify({"type": "source_changed", "source": source_id})
        if not self.background:
            self._update_datasets(source_id)
            return
        with self._settle_cond:
            self._settle_pending.add(source_id)
            if self._settler is None:
                self._settler = threading.Thread(target=self._settle_loop, name="settle-datasets", daemon=True)
                self._settler.start()
            self._settle_cond.notify()

    def _settle_loop(self) -> None:
        while True:
            with self._settle_cond:
                if not self._settle_pending:
                    self._settle_cond.wait(SETTLE_IDLE_EXIT)
                    if not self._settle_pending:
                        self._settler = None
                        return
            time.sleep(SETTLE_DELAY)  # let the rest of a burst arrive
            with self._settle_cond:
                batch, self._settle_pending = self._settle_pending, set()
                self._settling = True
            try:
                for source_id in sorted(batch):
                    try:
                        self._update_datasets(source_id)
                    except Exception:
                        log.exception("bringing the datasets up to date after a change of source %s failed", source_id)
            finally:
                with self._settle_cond:
                    self._settling = False

    def settled(self, timeout: float = 30.0) -> bool:
        """Waits until no dataset update is pending or running (tests, benchmarks)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._settle_cond:
                if not self._settle_pending and not self._settling:
                    return True
            time.sleep(0.02)
        return False

    def _update_datasets(self, source_id: str) -> None:
        referencing = self.datasets.datasets_referencing(source_id)
        for dataset in referencing:
            dataset.recompute(source_id=source_id, from_index=True)
        changed = {d.id for d in referencing}
        for dataset in self.datasets.automated(source_id):
            if dataset.run_automation()["changed"]:
                changed.add(dataset.id)
        for dataset_id in sorted(changed):
            self.notify({"type": "dataset_changed", "dataset": dataset_id})

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
            self.source_changed_later(source.id)
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
        self.source_changed_later(source_id)  # entries pick up the new file name; nothing else changes
        self.notify({"type": "tree_changed"})
        return new_path

    def referenced_items(self, source_id: str) -> set[str]:
        """The items of a source that dataset entries use."""
        referenced: set[str] = set()
        for dataset in self.datasets.datasets_referencing(source_id):
            referenced |= dataset.referenced_items(source_id)
        return referenced

    def delete_item(self, source_id: str, path: str) -> None:
        source = self.catalog.find_source(source_id)
        source.delete_item(path, self.referenced_items(source_id))
        self.source_changed_later(source_id)
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
            for source_id in sorted(set(dataset.referenced_sources()) | dataset.automation_sources()):
                source = self.catalog.try_source(source_id)
                if source is not None:
                    source.refresh(progress)
            dataset.recompute()
            result = dataset.run_automation()
            self.notify({"type": "dataset_changed", "dataset": dataset.id})
            return result

        return self.start_job("check", dataset_id, work)

    def set_automation(self, dataset_id: str, raw, overwrite: bool = False) -> dict:
        """Saves the automation settings and applies them in the background (a job "automation"). `overwrite`: derived
        fields that have a different value already get the derived one ("Overwrite existing"); otherwise only empty
        fields are filled ("Fill in missing")."""
        dataset = self.datasets.get(dataset_id)
        config = dataset.set_automation(raw)
        self.notify({"type": "dataset_changed", "dataset": dataset.id, "schema": True})
        self.run_automation(dataset_id, overwrite)
        return config

    def run_automation(self, dataset_id: str, overwrite: bool = False) -> dict:
        dataset = self.datasets.get(dataset_id)

        def work(progress):
            result = dataset.run_automation(overwrite)
            self.notify({"type": "dataset_changed", "dataset": dataset.id})
            return result

        return self.start_job("automation", dataset_id, work)

    # -- export (export.py)
    def export_dataset(self, dataset_id: str, options, overwrite: bool = False) -> dict:
        """Starts an export (job "export"); one at a time per dataset. Its progress events carry {phase, done, total, bytes,
        total_bytes}; `export_status` says the same for a wizard that is opened while it runs."""
        dataset = self.datasets.get(dataset_id)
        options = export.normalize_options(options, dataset, dataset.slots())
        destination = export.check_destination(dataset, options)  # say what is wrong now, not in a failed job
        if destination["error"]:
            raise WorkbenchError(destination["error"])
        if destination["replace"] and not overwrite:
            raise WorkbenchError(f"{destination['path']} exists already.", 409)
        cancel = threading.Event()
        status = {"state": "running", "progress": {"phase": "collect"}, "result": None, "error": None, "cancel": cancel}

        def work(progress):
            def report(info: dict) -> None:
                status["progress"] = info
                progress(info)
            try:
                status["result"] = export.run(dataset, options, overwrite, report, cancel)
                status["state"] = "done"
                return status["result"]
            except export.Cancelled:
                status["state"] = "cancelled"
                raise
            except Exception as e:
                status["state"], status["error"] = "failed", getattr(e, "message", None) or str(e)
                raise

        job = self.start_job("export", dataset_id, work)
        if job["running"]:
            raise WorkbenchError("An export of this dataset is running already.", 409)
        with self._guard:
            self._exports[dataset_id] = status
        return {"job": job["job"], "path": destination["path"]}

    def export_status(self, dataset_id: str) -> dict:
        status = self._exports.get(dataset_id)
        if status is None:
            return {"state": "idle"}
        return {k: v for k, v in status.items() if k != "cancel"}

    def cancel_export(self, dataset_id: str) -> None:
        status = self._exports.get(dataset_id)
        if status is not None and status["state"] == "running":
            status["cancel"].set()

    def set_state(self, dataset, entry) -> dict:
        self.state = {"dataset": dataset, "entry": entry}
        return self.state
