#!/usr/bin/env python3
"""File browser backend: lists the entries of a directory of the server.

GET /list?path=<directory>  ->  {"path", "parent", "entries": [{"name", "kind", "size", "modified"}]}
Without `path` the configured start folder (`start_directory`, else the home directory) is listed.
With `show_hidden` off, names starting with a dot are left out. `kind` is "dir" or "file"; symbolic links
count as what they point to (a broken link is a file).
"""
import asyncio
import os
from pathlib import Path

import salus_sdk as salus

app = salus.Plugin()


def entry_info(entry: os.DirEntry) -> dict:
    try:
        is_dir = entry.is_dir()  # follows symbolic links
    except OSError:
        is_dir = False
    try:
        stat = entry.stat()
        size, modified = (None if is_dir else stat.st_size), stat.st_mtime
    except OSError:
        size, modified = None, None  # e.g. a broken link
    return {"name": entry.name, "kind": "dir" if is_dir else "file", "size": size, "modified": modified}


def list_directory(requested: str | None, show_hidden: bool = True) -> dict:
    directory = Path(requested).expanduser() if requested else Path.home()
    if not directory.is_absolute():
        raise salus.HTTPError(400, "The path must be absolute.")
    directory = Path(os.path.normpath(directory))
    try:
        with os.scandir(directory) as scan:
            entries = [entry_info(entry) for entry in scan if show_hidden or not entry.name.startswith(".")]
    except FileNotFoundError:
        raise salus.HTTPError(404, f"{directory} does not exist.") from None
    except NotADirectoryError:
        raise salus.HTTPError(400, f"{directory} is not a directory.") from None
    except PermissionError:
        raise salus.HTTPError(403, f"No permission to read {directory}.") from None
    entries.sort(key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    parent = None if directory == directory.parent else str(directory.parent)
    return {"path": str(directory), "parent": parent, "entries": entries}


@app.route("GET", "/list")
async def list_route(req):
    # Reading a large directory can block, so it runs in a worker thread.
    settings = await app.config.all()
    requested = req.query.get("path") or settings.get("start_directory") or None
    result = await asyncio.to_thread(list_directory, requested, settings.get("show_hidden", True))
    return salus.Response.json(result, headers=[("cache-control", "no-store")])


if __name__ == "__main__":
    app.run()
