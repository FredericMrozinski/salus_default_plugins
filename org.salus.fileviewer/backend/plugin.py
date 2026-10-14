#!/usr/bin/env python3
"""File viewer backend: one backend for the image, table and PDF viewers.

  slide  GET /slide/info?file=, GET /slide/tiles/{slide}/{level}/{tile}   (slides.py: whole slide images, OpenSlide)
  image  GET /image/info?file=, GET /image/tiles/{id}/{level}/{tile}      (pictures.py: png, jpeg, ... rendered by Pillow)
  table  GET /table/open?file=, GET /table/rows?file=&table=...           (tables.py)
         /table/edit/...                                                  (table_edit.py: editing CSV and TSV files)
  pdf    GET /pdf/info?file=, GET /pdf/page/{doc}/{page}?scale=           (pdfs.py; renders pages, never sends the file)

Every request names the file, so one backend serves any number of open viewers. Add a viewer by adding a module,
its routes here and a component in the manifest.
"""
import asyncio
import json
import logging
import threading
from pathlib import Path

import pdfs
import salus_sdk as salus
from errors import ViewerError
from pictures import Pictures
from slides import Slides
from table_edit import Edits
from tables import Store

log = logging.getLogger("fileviewer")
app = salus.Plugin()

# Frontend ids restart with the server, so URLs that contain one must not be cached; content-addressed ones may be.
NO_CACHE = [("cache-control", "no-store")]
TILE_CACHE = [("cache-control", "private, max-age=86400, immutable")]


def fail(error: ViewerError):
    return salus.HTTPError(error.status, str(error))


def file_arg(req) -> str:
    file = req.query.get("file")
    if not file:
        raise salus.HTTPError(400, "Missing file.")
    return file


# --------------------------------------------------------------------------- slide (whole slide images)

slides = Slides()


@app.route("GET", "/slide/info")
async def slide_info(req):
    try:
        slide = await asyncio.to_thread(slides.open, file_arg(req))
    except ViewerError as e:
        raise fail(e) from None
    asyncio.ensure_future(slides.prewarm(slide))
    return salus.Response.json({**slide.info(), "cache": slides.cache.stats()}, headers=NO_CACHE)


@app.route("GET", "/slide/tiles/{slide}/{level}/{tile}")
async def slide_tile(req):
    try:
        data = await slides.tile(req.params["slide"], req.params["level"], req.params["tile"])
    except ViewerError as e:
        raise fail(e) from None
    return salus.Response.bytes(data, content_type="image/jpeg", headers=TILE_CACHE)


# --------------------------------------------------------------------------- image (png, jpeg, ...)

pictures = Pictures()


@app.route("GET", "/image/info")
async def image_info(req):
    try:
        picture = await asyncio.to_thread(pictures.open, file_arg(req))
    except ViewerError as e:
        raise fail(e) from None
    return salus.Response.json(picture.info(), headers=NO_CACHE)


@app.route("GET", "/image/tiles/{id}/{level}/{tile}")
async def image_tile(req):
    try:
        data, kind = await pictures.tile(req.params["id"], req.params["level"], req.params["tile"])
    except ViewerError as e:
        raise fail(e) from None
    # Not cached by the browser: the tiles of a private image should not be left lying around.
    return salus.Response.bytes(data, content_type=f"image/{kind}", headers=NO_CACHE)


# --------------------------------------------------------------------------- table

stores: dict[str, Store] = {}
stores_lock = threading.Lock()


def open_store(file: str) -> Store:
    path = Path(file).expanduser()
    if not path.is_file():
        raise salus.HTTPError(404, "That file does not exist on the server.")
    with stores_lock:
        store = stores.get(str(path))
        if store is not None and store.state != "loading" and store.stale():
            store.close()
            store = None
        if store is None:
            store = stores[str(path)] = Store(path)
            threading.Thread(target=store.load, daemon=True, name=f"load-{path.name}").start()
        return store


@app.route("GET", "/table/open")
def table_open(req):
    return open_store(file_arg(req)).info()


@app.route("GET", "/table/rows")
def table_rows(req):
    store = open_store(file_arg(req))
    if store.state != "ready":
        raise salus.HTTPError(409, store.error or "Still loading.")
    try:
        filters = json.loads(req.query.get("filters") or "{}")
        if not isinstance(filters, dict):
            raise ValueError
        return store.page(
            req.query.get("table", ""),
            max(1, min(int(req.query.get("limit", 100)), 1000)),
            max(0, int(req.query.get("offset", 0))),
            req.query.get("sort") or None,
            req.query.get("desc") == "1",
            req.query.get("q", ""),
            filters,
        )
    except ViewerError as e:
        raise fail(e) from None
    except ValueError:
        raise salus.HTTPError(400, "Invalid parameters.") from None


# --- editing CSV/TSV (table_edit.py): a private working copy, saved explicitly; nothing is locked

edits = Edits()


def edit_session(req):
    try:
        return edits.get(req.params["id"])
    except ViewerError as e:
        raise fail(e) from None


def json_body(req) -> dict:
    data = req.body.as_json() if len(req.body) else {}
    if not isinstance(data, dict):
        raise salus.HTTPError(400, "Expected a JSON object.")
    return data


def editing(handler):
    """Runs an editing route; ViewerError becomes an HTTP error with its message."""
    def wrapped(req):
        try:
            return handler(req)
        except ViewerError as e:
            raise fail(e) from None
        except (TypeError, ValueError, KeyError):
            raise salus.HTTPError(400, "Invalid request.") from None
    wrapped.__name__ = handler.__name__
    return wrapped


@app.route("POST", "/table/edit/start")
@editing
def edit_start(req):
    return edits.start(file_arg_body(req), req.frontend_id).info()


def file_arg_body(req) -> str:
    file = json_body(req).get("file")
    if not file:
        raise salus.HTTPError(400, "Missing file.")
    return file


@app.route("GET", "/table/edit/{id}")
@editing
def edit_info(req):
    return edit_session(req).info()


@app.route("GET", "/table/edit/{id}/rows")
@editing
def edit_rows(req):
    return edit_session(req).page(max(1, min(int(req.query.get("limit", 100)), 1000)), max(0, int(req.query.get("offset", 0))))


@app.route("POST", "/table/edit/{id}/cells")
@editing
def edit_cells(req):
    session = edit_session(req)
    session.set_cells(json_body(req).get("edits"))
    return {"modified": session.modified}


@app.route("POST", "/table/edit/{id}/rows")
@editing
def edit_insert_rows(req):
    session = edit_session(req)
    data = json_body(req)
    ids = session.insert_rows(data.get("where", "end"), data.get("count", 1))
    return {"ids": ids, "rows": session.rows, "modified": session.modified}


@app.route("POST", "/table/edit/{id}/rows/delete")
@editing
def edit_delete_rows(req):
    session = edit_session(req)
    removed = session.delete_rows(json_body(req).get("ids"))
    return {"removed": removed, "rows": session.rows, "modified": session.modified}


@app.route("POST", "/table/edit/{id}/columns")
@editing
def edit_add_column(req):
    session = edit_session(req)
    data = json_body(req)
    column = session.add_column(data.get("name", ""), data.get("after"))
    return {"column": column, "columns": session.columns, "modified": session.modified}


@app.route("POST", "/table/edit/{id}/columns/rename")
@editing
def edit_rename_column(req):
    session = edit_session(req)
    data = json_body(req)
    session.rename_column(data.get("col"), data.get("name"))
    return {"columns": session.columns, "modified": session.modified}


@app.route("POST", "/table/edit/{id}/columns/delete")
@editing
def edit_delete_column(req):
    session = edit_session(req)
    session.delete_column(json_body(req).get("col"))
    return {"columns": session.columns, "modified": session.modified}


@app.route("GET", "/table/edit/{id}/path-info")
@editing
def edit_path_info(req):
    return edit_session(req).path_info(req.query.get("path", ""))


@app.route("POST", "/table/edit/{id}/save")
@editing
def edit_save(req):
    """{} saves into the file; {"overwrite": true} even if it changed; {"path": ...} writes a new file there.
    If the file changed since it was loaded the answer is 409 with {"conflict": "changed" | "deleted"} and nothing is written."""
    data = json_body(req)
    result = edit_session(req).save(data.get("path") or None, bool(data.get("overwrite")))
    if "conflict" in result:
        return salus.Response.json(result, status=409)
    return result


@app.route("POST", "/table/edit/{id}/discard")
@editing
def edit_discard(req):
    session = edit_session(req)
    session.discard()
    return session.info()


@app.route("DELETE", "/table/edit/{id}")
@editing
def edit_close(req):
    edits.close(req.params["id"])
    return None


@app.on_frontend_detached
def viewer_closed(frontend_id):
    edits.close_owned(frontend_id)  # the working copies of a closed viewer go away; unsaved edits are lost


# --------------------------------------------------------------------------- pdf

pdf_docs = pdfs.Pdfs()


@app.route("GET", "/pdf/info")
async def pdf_info(req):
    try:
        doc = await asyncio.to_thread(pdf_docs.open, file_arg(req))
    except ViewerError as e:
        raise fail(e) from None
    return salus.Response.json(doc.info(), headers=NO_CACHE)


@app.route("GET", "/pdf/page/{doc}/{page}")
async def pdf_page(req):
    try:
        page = int(req.params["page"])
        scale = float(req.query.get("scale", "1"))
        data = await asyncio.to_thread(pdf_docs.render, req.params["doc"], page, scale)
    except ValueError:
        raise salus.HTTPError(400, "Invalid page or scale.") from None
    except ViewerError as e:
        raise fail(e) from None
    # No caching in the browser: the pages of a private document should not be left lying around.
    return salus.Response.bytes(data, content_type="image/png", headers=NO_CACHE)


if __name__ == "__main__":
    app.run()
