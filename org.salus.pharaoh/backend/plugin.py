#!/usr/bin/env python3
"""Pharaoh backend: repairs and compresses whole slide images (pyramid levels, compression).

  GET  /state                  the list, the plan and the run
  GET  /fs?path=               server folders and slide files, for choosing what to add
  POST /items {path}           adds a file, or all slides below a folder
  POST /items/remove {ids}     POST /items/clear
  PUT  /plan {transform?, output?}
  GET  /preview                what a run would do with each file
  POST /run, /run/abort, /run/reset
"""
import logging
from pathlib import Path

import salus_sdk as salus
import wsi
from errors import PharaohError
from pharaoh import Pharaoh

log = logging.getLogger("pharaoh")
app = salus.Plugin()
pharaoh = Pharaoh()
NO_CACHE = [("cache-control", "no-store")]


def guarded(handler):
    def wrapped(req):
        try:
            return handler(req)
        except PharaohError as e:
            raise salus.HTTPError(e.status, str(e)) from None
    wrapped.__name__ = handler.__name__
    return wrapped


def body(req) -> dict:
    data = req.body.as_json() if len(req.body) else {}
    if not isinstance(data, dict):
        raise salus.HTTPError(400, "Expected a JSON object.")
    return data


@app.route("GET", "/state")
def state(req):
    return salus.Response.json(pharaoh.state(), headers=NO_CACHE)


@app.route("GET", "/fs")
@guarded
def browse(req):
    path = Path(req.query.get("path") or "~").expanduser()
    if not path.is_dir():
        # A file or something that is gone: show the folder it was in.
        path = path.parent if path.parent.is_dir() else Path.home()
    folders, files = [], []
    try:
        for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    folders.append({"name": entry.name})
                elif entry.is_file() and wsi.is_slide_name(entry.name):
                    files.append({"name": entry.name, "size": entry.stat().st_size})
            except OSError:
                continue
    except OSError as e:
        raise PharaohError(f"That folder cannot be read: {e.strerror or e}") from None
    parent = str(path.parent) if path.parent != path else None
    return salus.Response.json({"path": str(path), "parent": parent, "folders": folders, "files": files}, headers=NO_CACHE)


@app.route("POST", "/items")
@guarded
def add(req):
    return pharaoh.add(str(body(req).get("path", "")))


@app.route("POST", "/items/remove")
@guarded
def remove(req):
    pharaoh.remove([str(i) for i in body(req).get("ids", [])])
    return {}


@app.route("POST", "/items/clear")
@guarded
def clear(req):
    pharaoh.clear()
    return {}


@app.route("PUT", "/plan")
@guarded
def plan(req):
    data = body(req)
    pharaoh.set_plan(data.get("transform"), data.get("output"))
    return {"transform": pharaoh.transform, "output": pharaoh.output}


@app.route("GET", "/preview")
def preview(req):
    return salus.Response.json({"rows": pharaoh.preview()}, headers=NO_CACHE)


@app.route("POST", "/run")
@guarded
def run(req):
    pharaoh.start()
    return {}


@app.route("POST", "/run/abort")
def abort(req):
    pharaoh.abort()
    return {}


@app.route("POST", "/run/reset")
@guarded
def reset(req):
    pharaoh.reset()
    return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()
