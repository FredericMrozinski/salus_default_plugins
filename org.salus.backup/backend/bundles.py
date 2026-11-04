"""Backup bundles: folders that are backed up together into one restic repository with its own password.

A bundle is a folder in the backup root:

    <backup root>/<bundle>/bundle.json   name, the folders and the exclude patterns
    <backup root>/<bundle>/repo/         the restic repository (encrypted, compressed, deduplicated snapshots)

Everything about a bundle is inside its folder, so a bundle can be moved to another machine by copying the folder into
that machine's backup root. A plain restic repository put into the backup root shows up as a bundle as well.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path

import restic
from errors import BackupError

BUNDLE_FILE = "bundle.json"
REPO_DIR = "repo"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
MIN_PASSWORD = 8


def is_repository(path: Path) -> bool:
    return (path / "config").is_file() and (path / "keys").is_dir()


def check_name(name) -> str:
    name = str(name or "").strip()
    if not NAME.match(name) or name.endswith("."):
        raise BackupError("Use 1–64 letters, digits, spaces, dots, dashes or underscores for the name (starting with a letter or digit).")
    return name


def clean_folders(folders) -> list[str]:
    if not isinstance(folders, list) or not folders:
        raise BackupError("Add at least one folder to the bundle.")
    out = []
    for folder in folders:
        path = Path(str(folder).strip()).expanduser()
        if not path.is_absolute():
            raise BackupError(f"'{folder}' is not a full path.")
        if str(path) not in out:
            out.append(str(path))
    return out


def clean_excludes(excludes) -> list[str]:
    if excludes is None:
        return []
    if isinstance(excludes, str):
        excludes = excludes.splitlines()
    return [e.strip() for e in excludes if str(e).strip()]


class Bundle:
    def __init__(self, folder: Path):
        self.folder = folder
        self.name = folder.name
        bare = is_repository(folder)  # a restic repository put into the root as it is
        self.repo = folder if bare else folder / REPO_DIR
        self.definition: dict = {"folders": [], "excludes": []}
        file = folder / BUNDLE_FILE
        if not bare and file.is_file():
            try:
                self.definition.update(json.loads(file.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        self.bare = bare

    @property
    def folders(self) -> list[str]:
        return list(self.definition.get("folders") or [])

    @property
    def excludes(self) -> list[str]:
        return list(self.definition.get("excludes") or [])

    def info(self) -> dict:
        missing = [f for f in self.folders if not Path(f).exists()]
        return {"name": self.name, "path": str(self.folder), "folders": self.folders, "excludes": self.excludes,
                "missing_folders": missing, "ok": is_repository(self.repo), "bare": self.bare}

    def save_definition(self, folders, excludes) -> None:
        if self.bare:
            raise BackupError("This bundle is a plain restic repository; its folder list cannot be stored next to it.")
        self.definition.update(folders=clean_folders(folders), excludes=clean_excludes(excludes))
        tmp = self.folder / f".{BUNDLE_FILE}.tmp"
        tmp.write_text(json.dumps(self.definition, indent=1), encoding="utf-8")
        tmp.replace(self.folder / BUNDLE_FILE)


class Bundles:
    def __init__(self):
        self.jobs: dict[str, Job] = {}  # bundle name -> its running or last job
        self.lock = threading.Lock()

    # -- finding, creating, changing
    def list(self, root: Path) -> list[Bundle]:
        if not root.is_dir():
            return []
        out = []
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith(".") and (is_repository(child) or is_repository(child / REPO_DIR) or (child / BUNDLE_FILE).is_file()):
                out.append(Bundle(child))
        return out

    def get(self, root: Path, name: str) -> Bundle:
        folder = root / check_name(name)
        if not folder.is_dir():
            raise BackupError("There is no such bundle.", 404)
        return Bundle(folder)

    def create(self, root: Path, name, password: str, folders, excludes) -> Bundle:
        name = check_name(name)
        folders = clean_folders(folders)
        excludes = clean_excludes(excludes)
        if len(password or "") < MIN_PASSWORD:
            raise BackupError(f"Use a password of at least {MIN_PASSWORD} characters.")
        folder = root / name
        if folder.exists():
            raise BackupError("There is a bundle (or another folder) with that name already.")
        root.mkdir(parents=True, exist_ok=True)
        folder.mkdir()
        try:
            restic.run(folder / REPO_DIR, ["init"], password)
            bundle = Bundle(folder)
            bundle.definition["created"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            bundle.save_definition(folders, excludes)
            return bundle
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    # -- reading (password needed)
    def unlock(self, bundle: Bundle, password: str) -> None:
        """Checks the password (cheap: only the repository's key is read)."""
        restic.run(bundle.repo, ["cat", "config"], password)

    def snapshots(self, bundle: Bundle, password: str) -> list[dict]:
        out = restic.run(bundle.repo, ["snapshots"], password)
        snapshots = out[0] if out and isinstance(out[0], list) else []
        result = []
        for s in snapshots:
            summary = s.get("summary") or {}
            result.append({"id": s.get("id"), "short_id": s.get("short_id") or str(s.get("id", ""))[:8], "time": s.get("time"),
                           "hostname": s.get("hostname"), "paths": s.get("paths") or [], "tags": s.get("tags") or [],
                           "files": summary.get("total_files_processed"), "bytes": summary.get("total_bytes_processed"),
                           "added": summary.get("data_added_packed", summary.get("data_added"))})
        result.sort(key=lambda s: s["time"] or "", reverse=True)
        return result

    def list_dir(self, bundle: Bundle, password: str, snapshot: str, path: str) -> list[dict]:
        """The entries directly inside a folder of a snapshot ("/" is the top)."""
        snapshot = check_snapshot(snapshot)
        path = "/" + str(path or "/").strip("/")
        out = restic.run(bundle.repo, ["ls", snapshot, path], password)
        entries = []
        for node in out:
            if not isinstance(node, dict) or node.get("struct_type") not in (None, "node") or "path" not in node:
                continue
            if node["path"].rstrip("/") == path.rstrip("/"):  # the folder itself
                continue
            parent = node["path"].rsplit("/", 1)[0] or "/"
            if parent != path:  # only the direct children
                continue
            entries.append({"name": node.get("name"), "path": node["path"], "type": node.get("type"),
                            "size": node.get("size"), "mtime": node.get("mtime")})
        entries.sort(key=lambda e: (e["type"] != "dir", (e["name"] or "").lower()))
        return entries

    # -- jobs (one at a time per bundle)
    def start(self, bundle: Bundle, job: Job) -> Job:
        with self.lock:
            current = self.jobs.get(bundle.name)
            if current and current.state == "running":
                raise BackupError("Something is running for this bundle already. Wait until it has finished.", 409)
            self.jobs[bundle.name] = job
        threading.Thread(target=job.run, name=f"{job.kind}-{bundle.name}", daemon=True).start()
        return job

    def backup(self, bundle: Bundle, password: str) -> Job:
        if not bundle.folders:
            raise BackupError("The bundle has no folders. Add some first.")
        missing = [f for f in bundle.folders if not Path(f).exists()]
        if missing:
            raise BackupError(f"These folders do not exist: {', '.join(missing)}. Remove them from the bundle or bring them back.")
        args = ["backup", "--tag", "salus"]
        for pattern in bundle.excludes:
            args += ["--exclude", pattern]
        return self.start(bundle, Job("backup", bundle, args + ["--", *bundle.folders], password))

    def restore(self, bundle: Bundle, password: str, snapshot: str, items: list[str], target: str, strip: bool,
                overwrite: bool) -> Job:
        snapshot = check_snapshot(snapshot)
        items = [("/" + str(i).strip("/")) for i in items or []] or ["/"]
        original = target is None
        if not original:
            target_path = Path(str(target).strip()).expanduser()
            if not target_path.is_absolute():
                raise BackupError("Enter the full path of the folder to restore into.")
            target = str(target_path)
        policy = ["--overwrite", "if-changed" if overwrite else "never"]
        runs = []
        if original:
            runs.append([snapshot, "--target", "/", *include_args(items), *policy])
        elif not strip or items == ["/"]:
            runs.append([snapshot, "--target", target, *include_args(items), *policy])
        else:
            # Without the original folders above them: <snapshot>:<parent> restores the parent's content into target.
            by_parent: dict[str, list[str]] = {}
            for item in items:
                parent, _, name = item.rpartition("/")
                by_parent.setdefault(parent or "/", []).append("/" + name)
            for parent, names in by_parent.items():
                runs.append([f"{snapshot}:{parent}", "--target", target, *include_args(names), *policy])
        return self.start(bundle, Job("restore", bundle, None, password, runs=[["restore", *r] for r in runs],
                                      detail={"target": target or "original locations", "items": items}))

    def forget(self, bundle: Bundle, password: str, keep_last: int) -> Job:
        if not isinstance(keep_last, int) or keep_last < 1:
            raise BackupError("Keep at least one snapshot.")
        return self.start(bundle, Job("cleanup", bundle, ["forget", "--keep-last", str(keep_last), "--prune"], password))

    def check(self, bundle: Bundle, password: str) -> Job:
        return self.start(bundle, Job("check", bundle, ["check"], password))

    def status(self, name: str) -> dict | None:
        job = self.jobs.get(name)
        return job.status() if job else None


def check_snapshot(snapshot) -> str:
    snapshot = str(snapshot or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}|latest", snapshot):
        raise BackupError("Invalid snapshot.")
    return snapshot


def include_args(items: list[str]) -> list[str]:
    if items == ["/"]:
        return []
    out = []
    for item in items:
        out += ["--include", item]
    return out


class Job:
    """A restic run (or several, for a restore) in a thread, with its progress."""

    def __init__(self, kind: str, bundle: Bundle, args: list[str] | None, password: str, runs: list[list[str]] | None = None,
                 detail: dict | None = None):
        self.kind = kind
        self.bundle = bundle
        self.runs = runs if runs is not None else [args]
        self.password = password
        self.detail = detail or {}
        self.state = "running"  # running | done | failed | stopped
        self.progress: dict = {}
        self.summary: dict = {}
        self.errors: list[str] = []
        self.message = ""
        self.started = time.time()
        self.finished: float | None = None
        self.process: restic.Process | None = None
        self.stop_requested = False

    def on_line(self, line: dict) -> None:
        kind = line.get("message_type")
        if kind == "status":
            self.progress = {k: line.get(k) for k in ("percent_done", "total_files", "files_done", "files_restored", "files_skipped",
                                                    "total_bytes", "bytes_done", "bytes_restored", "seconds_remaining", "seconds_elapsed")}
            current = line.get("current_files") or []
            self.progress["current"] = current[0] if current else None
        elif kind == "summary":
            self.summary = {k: v for k, v in line.items() if k != "message_type"}
        elif kind == "error" and len(self.errors) < 100:
            error = line.get("error") or {}
            self.errors.append(f"{line.get('item', '')}: {error.get('message', error) if isinstance(error, dict) else error}".strip(": "))

    def run(self) -> None:
        try:
            for i, args in enumerate(self.runs):
                if self.stop_requested:
                    break
                self.process = restic.Process(self.bundle.repo, args, self.password, self.on_line)
                code, _, stderr = self.process.run()
                # restic decides: a run that finished before the stop arrived counts as done.
                if code == restic.EXIT_INTERRUPTED or (self.stop_requested and code not in (0, restic.EXIT_INCOMPLETE)):
                    self.state, self.message = "stopped", "Stopped. Nothing was saved from this run." if self.kind == "backup" else "Stopped."
                    return
                if code == restic.EXIT_INCOMPLETE and self.kind == "backup":
                    self.message = "Saved, but some files could not be read (see below)."
                elif code != 0:
                    self.state, self.message = "failed", restic.explain(code, stderr)
                    return
            if self.state == "running":
                self.state = "done"
        except Exception as e:
            self.state, self.message = "failed", str(e)
        finally:
            self.password = ""  # not kept longer than needed
            self.finished = time.time()
            if self.state == "running":
                self.state = "done"

    def stop(self) -> None:
        self.stop_requested = True
        if self.process:
            self.process.stop()

    def status(self) -> dict:
        return {"kind": self.kind, "bundle": self.bundle.name, "state": self.state, "progress": self.progress,
                "summary": self.summary, "errors": self.errors, "message": self.message, "detail": self.detail,
                "started": self.started, "finished": self.finished}
