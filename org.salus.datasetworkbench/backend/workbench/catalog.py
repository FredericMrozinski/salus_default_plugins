"""The workbench root: folders and source cohorts under `sources/`, datasets under `datasets/`."""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from .sources import SOURCE_DB, FileSource, Source, TableSource, hidden, open_source
from .util import NotFound, WorkbenchError, check_name, meta_set, rename_on_disk, resolve_inside, safe_rel_path


class Catalog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.sources_dir = self.root / "sources"
        self.datasets_dir = self.root / "datasets"
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.datasets_dir.mkdir(parents=True, exist_ok=True)
        self._paths: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._last_walk = 0.0

    # -- tree
    def _walk(self, directory: Path, rel: str, found: dict) -> list[dict]:
        nodes = []
        try:
            children = sorted((c for c in directory.iterdir() if c.is_dir() and not hidden(c.name)), key=lambda c: c.name.lower())
        except OSError:
            return nodes
        for child in children:
            child_rel = f"{rel}/{child.name}" if rel else child.name
            if (child / SOURCE_DB).exists():
                try:
                    source = open_source(child)
                    info = source.info()
                except Exception as e:  # a broken source must not hide the others
                    nodes.append({"type": "source", "id": None, "name": child.name, "path": child_rel, "kind": "file",
                                  "state": "broken", "error": str(e)})
                    continue
                found[info["id"]] = child
                nodes.append({"type": "source", "path": child_rel, **info})
            else:
                nodes.append({"type": "group", "name": child.name, "path": child_rel,
                              "children": self._walk(child, child_rel, found)})
        return nodes

    def tree(self) -> list[dict]:
        found: dict[str, Path] = {}
        nodes = self._walk(self.sources_dir, "", found)
        with self._lock:
            self._paths = found
            self._last_walk = time.monotonic()
        return nodes

    def search(self, q: str, limit: int = 200) -> dict:
        """The global search: files (and the CSV files of table sources) of all sources whose path contains every word of `q`."""
        results: list[dict] = []
        more = False
        found: dict[str, Path] = {}
        self._walk(self.sources_dir, "", found)
        for source_id, path in sorted(found.items(), key=lambda kv: str(kv[1]).lower()):
            try:
                source = open_source(path)
                hits = source.find(q, limit + 1 - len(results))
                name = source.name
            except Exception:
                continue  # a broken source must not hide the others
            rel = str(path.relative_to(self.sources_dir))
            results += [{**hit, "source": source_id, "source_name": name, "source_path": rel, "kind": source.kind} for hit in hits]
            if len(results) > limit:
                more = True
                results = results[:limit]
                break
        return {"results": results, "more": more}

    def source_ids(self) -> list[str]:
        self.tree()
        return list(self._paths)

    def find_source(self, source_id: str) -> Source:
        source = self.try_source(source_id, min_interval=0.0)
        if source is None:
            raise NotFound("No such source.")
        return source

    def try_source(self, source_id: str, refresh: bool = True, min_interval: float = 1.0) -> Source | None:
        """The source with this id, or None. A miss re-walks the tree (at most once per `min_interval` seconds,
        so many lookups of a deleted source stay cheap)."""
        path = self._paths.get(source_id)
        if path is not None and (path / SOURCE_DB).exists():
            try:
                source = open_source(path)
                if source.id == source_id:
                    return source
            except Exception:
                pass
        if refresh and time.monotonic() - self._last_walk >= min_interval:
            self.tree()
            path = self._paths.get(source_id)
            if path is not None:
                return open_source(path)
        return None

    # -- groups and sources
    def create_group(self, parent: str, name: str) -> dict:
        name = check_name(name, "folder name")
        parent_path = self._group_path(parent)
        path = parent_path / name
        if path.exists():
            raise WorkbenchError(f"'{name}' already exists here.", 409)
        path.mkdir()
        return {"path": safe_rel_path(f"{parent}/{name}")}

    def _group_path(self, rel: str) -> Path:
        path = resolve_inside(self.sources_dir, rel or "")
        if path != self.sources_dir:
            if not path.is_dir():
                raise NotFound("No such folder.")
            if (path / SOURCE_DB).exists():
                raise WorkbenchError("A source cohort cannot contain folders or source cohorts.")
        return path

    def create_source(self, group: str, name: str, kind: str) -> Source:
        if kind not in ("file", "table"):
            raise WorkbenchError("The type must be 'file' or 'table'.")
        name = check_name(name, "source cohort name")
        path = self._group_path(group) / name
        if path.exists() and ((path / SOURCE_DB).exists()):
            raise WorkbenchError(f"'{name}' already is a source.", 409)
        if path.exists() and not path.is_dir():
            raise WorkbenchError(f"'{name}' already exists here.", 409)
        # An existing folder without source.sqlite becomes a source (its files are adopted).
        # Folders containing sources cannot be adopted.
        if path.exists() and any((p / SOURCE_DB).exists() for p in path.rglob("*") if p.is_dir()):
            raise WorkbenchError("That folder contains source cohorts; it is a folder of its own.")
        source = Source.create(path, name, kind)
        self.tree()
        return source

    def remove_source(self, source_id: str, delete_files: bool = False) -> None:
        """Forgets a source (its metadata goes away, the files stay) or, with `delete_files`, deletes its folder from disk."""
        source = self.find_source(source_id)
        with source.lock:
            if delete_files:
                self._delete_folder(source.path)
            else:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    (source.path / (SOURCE_DB + suffix)).unlink(missing_ok=True)
        self._paths.pop(source_id, None)

    def _delete_folder(self, path: Path) -> None:
        real = path.resolve()
        if real == self.sources_dir.resolve() or self.sources_dir.resolve() not in real.parents:
            raise WorkbenchError("Refusing to delete a folder outside the sources.", 500)
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path)

    def rename_group(self, rel: str, new_name: str) -> str:
        """Renames a group folder. Sources inside keep their ids, so nothing that references them breaks."""
        new_name = check_name(new_name, "folder name")
        path = self._group_path(rel)
        if path == self.sources_dir:
            raise WorkbenchError("The top level cannot be renamed.")
        rename_on_disk(path, path.parent / new_name)
        self.tree()
        parent = safe_rel_path(rel).rsplit("/", 1)[0] + "/" if "/" in safe_rel_path(rel) else ""
        return parent + new_name

    def rename_source(self, source_id: str, new_name: str) -> Source:
        """Renames the source's folder and its name. The id stays, so datasets keep pointing at it."""
        new_name = check_name(new_name, "source cohort name")
        source = self.find_source(source_id)
        with source.lock:
            rename_on_disk(source.path, source.path.parent / new_name)
        renamed = open_source(source.path.parent / new_name)
        with renamed.connect() as conn:
            meta_set(conn, "name", new_name)
        self.tree()
        return renamed

    def move(self, rel: str, to: str) -> str:
        """Moves a source or a group (with everything in it) into the group `to` ("" is the top level).
        Source ids live in the sources' own metadata, so references from datasets keep working."""
        rel = safe_rel_path(rel)
        to = safe_rel_path(to or "", allow_empty=True)
        src = resolve_inside(self.sources_dir, rel, allow_empty=False)
        if not src.is_dir() or src.is_symlink():
            raise NotFound("No such source cohort or folder.")
        target = self._group_path(to)
        # A source's folder holds its files; nothing may be moved into it or into one of its subfolders.
        for folder in [target, *target.parents]:
            if folder.resolve() == self.sources_dir.resolve():
                break
            if (folder / SOURCE_DB).exists():
                raise WorkbenchError("A source cohort cannot contain folders or source cohorts.")
        real_src, real_target = src.resolve(), target.resolve()
        if real_target == real_src or real_src in real_target.parents:
            raise WorkbenchError("A folder cannot be moved into itself.")
        if src.parent.resolve() == real_target:
            return rel
        dest = target / src.name
        if dest.exists():
            raise WorkbenchError(f"'{src.name}' already exists in {to or 'the top level'}.", 409)
        try:
            os.rename(src, dest)
        except OSError as e:
            raise WorkbenchError(f"Could not move: {e.strerror or e}", 500) from None
        self.tree()
        return f"{to}/{src.name}" if to else src.name

    def group_contents(self, rel: str) -> dict:
        path = self._group_path(rel)
        if path == self.sources_dir:
            raise WorkbenchError("The top level cannot be deleted.")
        found: dict[str, Path] = {}
        self._walk(path, rel, found)
        return {"sources": list(found), "empty": not any(path.iterdir())}

    def delete_group(self, rel: str, force: bool = False) -> list[str]:
        """Deletes a group folder. A group with anything in it needs `force`. Returns the ids of the sources that went away."""
        info = self.group_contents(rel)
        if not info["empty"] and not force:
            raise WorkbenchError("The folder is not empty.", 409)
        self._delete_folder(self._group_path(rel))
        for source_id in info["sources"]:
            self._paths.pop(source_id, None)
        return info["sources"]
