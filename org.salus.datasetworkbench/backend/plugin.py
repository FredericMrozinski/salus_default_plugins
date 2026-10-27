#!/usr/bin/env python3
"""Dataset Workbench backend. One backend serves the browser, dataset-viewer and entry-editor components.

All catalog logic lives in the `workbench` package (no Salus dependency, unit-tested); this file only maps
HTTP routes to it and broadcasts change events on the `events` channel.

Configuration: the workbench folder is the `root` setting of the plugin (configurable.toml), which the user sets in Salus' settings
(`app.config`). Until it is set the data routes answer 412 and the frontends say where to set it.
"""
import asyncio
import faulthandler
import logging
import signal
from functools import wraps

import salus_sdk as salus
from workbench.active import Active
from workbench.settings import RootSettings
from workbench.uploads import CHUNK_SIZE
from workbench.util import WorkbenchError

log = logging.getLogger("datasetworkbench")
faulthandler.register(signal.SIGUSR1, all_threads=True)  # `kill -USR1 <pid>` prints every thread's stack to stderr (debugging a hang)
app = salus.Plugin()


def notify(event: dict) -> None:
    """Tells every attached component that something changed (events are small; components refetch)."""
    try:
        app.send_blocking(salus.Payload.json(event), channel="events", to=salus.ALL)
    except Exception:
        log.debug("event not delivered: %s", event, exc_info=True)


settings = RootSettings(app.config)
wb = Active(settings, notify)  # use it like a Workbench; it is the one for the configured folder


def api(handler):
    """Turns WorkbenchError into an HTTP error with a readable message."""
    @wraps(handler)
    def wrapped(req):
        try:
            return handler(req)
        except WorkbenchError as e:
            raise salus.HTTPError(e.status, e.message) from None
    return wrapped


def body(req) -> dict:
    data = req.body.as_json() if len(req.body) else {}
    if not isinstance(data, dict):
        raise salus.HTTPError(400, "Expected a JSON object.")
    return data


def number(req, name: str, default: int, maximum: int = 1000) -> int:
    try:
        return max(0, min(int(req.query.get(name, default)), maximum))
    except ValueError:
        raise salus.HTTPError(400, f"Invalid {name}.") from None


def route(method: str, path: str):
    def register(handler):
        return app.route(method, path)(api(handler))
    return register


def entry_id(req) -> int:
    try:
        return int(req.params["eid"])
    except ValueError:
        raise salus.HTTPError(404, "No such entry.") from None


def slot_id(req) -> int:
    try:
        return int(req.params["slot"])
    except ValueError:
        raise salus.HTTPError(404, "No such slot.") from None


# --------------------------------------------------------------------------- general

@route("GET", "/config")
def config(req):
    return {"root": str(wb.catalog.root), "chunk_size": CHUNK_SIZE}


# --------------------------------------------------------------------------- the workbench folder setting (needs no folder to exist)

@route("GET", "/settings")
def get_settings(req):
    """{root: the folder set in the settings or null, active: whether it is in use, error: why it cannot be used}"""
    return wb.status()


@route("GET", "/state")
def get_state(req):
    return wb.state


@route("PUT", "/state")
def put_state(req):
    data = body(req)
    wb.set_state(data.get("dataset"), data.get("entry"))
    notify({"type": "state", **wb.state})
    return wb.state


# --------------------------------------------------------------------------- sources

@route("GET", "/tree")
def tree(req):
    return wb.catalog.tree()


@route("GET", "/search")
def search(req):
    """The global search over the files of all sources."""
    return wb.catalog.search(req.query.get("q", ""), number(req, "limit", 200, 1000))


@route("POST", "/groups")
def create_group(req):
    data = body(req)
    result = wb.catalog.create_group(data.get("parent", ""), data.get("name"))
    notify({"type": "tree_changed"})
    return salus.Response.json(result, status=201)


@route("POST", "/sources")
def create_source(req):
    data = body(req)
    source = wb.catalog.create_source(data.get("group", ""), data.get("name"), data.get("kind"))
    if source.kind == "file":
        wb.scan_source(source.id)  # adopts files that already are in the folder
    notify({"type": "tree_changed"})
    # The same shape as a node of /tree, so the frontend can use it straight away.
    node = {"type": "source", "path": str(source.path.relative_to(wb.catalog.sources_dir)), **source.info()}
    return salus.Response.json(node, status=201)


@route("GET", "/sources/{id}")
def source_info(req):
    return wb.catalog.find_source(req.params["id"]).info()


@route("DELETE", "/sources/{id}")
def remove_source(req):
    """Forgets the source; with ?files=1 its folder is deleted from disk, too."""
    source_id = req.params["id"]
    wb.catalog.remove_source(source_id, delete_files=req.query.get("files") == "1")
    wb.source_changed(source_id)  # references to it become "source missing"
    notify({"type": "tree_changed"})
    return None


@route("GET", "/groups/contents")
def group_contents(req):
    path = req.query.get("path", "")
    return {**wb.catalog.group_contents(path), "usage": wb.group_usage(path)}


@route("POST", "/move")
def move(req):
    """Moves a source or group into another group ("to": "" is the top level)."""
    data = body(req)
    new_path = wb.catalog.move(data.get("path", ""), data.get("to", ""))
    wb.refresh_names()
    return {"path": new_path}


@route("POST", "/groups/rename")
def rename_group(req):
    data = body(req)
    new_path = wb.catalog.rename_group(data.get("path", ""), data.get("name"))
    wb.refresh_names()
    return {"path": new_path}


@route("POST", "/sources/{id}/rename")
def rename_source(req):
    source = wb.catalog.rename_source(req.params["id"], body(req).get("name"))
    wb.refresh_names()
    return source.info()


@route("GET", "/sources/{id}/usage")
def source_usage(req):
    return wb.source_usage(req.params["id"], req.query.get("path") or None)


@route("POST", "/sources/{id}/items/rename")
def rename_item(req):
    data = body(req)
    return {"path": wb.rename_item(req.params["id"], data.get("path", ""), data.get("name"))}


@route("DELETE", "/sources/{id}/items")
def delete_item(req):
    wb.delete_item(req.params["id"], req.query.get("path", ""))
    return None


@route("DELETE", "/groups")
def delete_group(req):
    """Deletes a group folder from disk. A group with content needs ?force=1."""
    removed = wb.catalog.delete_group(req.query.get("path", ""), force=req.query.get("force") == "1")
    for source_id in removed:
        wb.source_changed(source_id)
    notify({"type": "tree_changed"})
    return None


@route("POST", "/sources/{id}/scan")
def scan(req):
    return wb.scan_source(req.params["id"])


@route("POST", "/scan")
def scan_all(req):
    return wb.scan_all()


@route("GET", "/jobs")
def jobs(req):
    return {"running": wb.running_jobs()}


@route("GET", "/sources/{id}/files")
def files(req):
    source = wb.catalog.find_source(req.params["id"])
    if source.kind != "file":
        raise salus.HTTPError(400, "Only file sources have files.")
    return source.list_dir(req.query.get("dir", ""), req.query.get("q", ""), number(req, "limit", 200), number(req, "offset", 0, 10**9))


@route("GET", "/sources/{id}/suggest-columns")
def suggest_columns(req):
    source = wb.catalog.find_source(req.params["id"])
    if source.kind != "table":
        raise salus.HTTPError(400, "Only table sources have columns.")
    return source.inspect()


@route("PUT", "/sources/{id}/columns")
def set_columns(req):
    data = body(req)
    return wb.configure_table(req.params["id"], data.get("columns"), bool(data.get("auto_key")))


@route("POST", "/sources/{id}/import-csv")
def import_csv(req):
    return wb.import_csv(req.params["id"], body(req).get("path", ""))


@route("GET", "/sources/{id}/issues")
def issues(req):
    source = wb.catalog.find_source(req.params["id"])
    if source.kind != "table":
        raise salus.HTTPError(400, "Only table sources have ingest issues.")
    return source.issues(number(req, "limit", 200), number(req, "offset", 0, 10**9))


# --------------------------------------------------------------------------- uploads

@route("POST", "/upload")
def upload_start(req):
    data = body(req)
    return wb.uploads.start(data.get("source"), data.get("path", ""), data.get("size"), bool(data.get("overwrite")))


@route("POST", "/upload/check")
def upload_check(req):
    """Which of the paths exist already: {"existing": [...]}."""
    data = body(req)
    return {"existing": wb.uploads.check(data.get("source"), data.get("paths", []))}


@route("GET", "/upload/{id}")
def upload_status(req):
    return wb.uploads.status(req.params["id"])


@route("PUT", "/upload/{id}/{offset}")
def upload_chunk(req):
    try:
        offset = int(req.params["offset"])
    except ValueError:
        raise salus.HTTPError(400, "Invalid offset.") from None
    return wb.uploads.append(req.params["id"], offset, req.body.as_bytes())


@route("POST", "/upload/{id}/commit")
def upload_commit(req):
    return wb.finish_upload(req.params["id"])


@route("DELETE", "/upload/{id}")
def upload_abort(req):
    wb.uploads.abort(req.params["id"])
    return None


# --------------------------------------------------------------------------- datasets

@route("GET", "/datasets")
def list_datasets(req):
    return wb.datasets.list()


@route("POST", "/datasets")
def create_dataset(req):
    dataset = wb.datasets.create(body(req).get("name"))
    notify({"type": "tree_changed"})
    return salus.Response.json(dataset.detail(), status=201)


@route("GET", "/datasets/{id}")
def get_dataset(req):
    return wb.datasets.get(req.params["id"]).detail()


@route("PATCH", "/datasets/{id}")
def rename_dataset(req):
    dataset = wb.datasets.get(req.params["id"])
    dataset.rename(body(req).get("name"))
    notify({"type": "dataset_changed", "dataset": dataset.id})
    return dataset.detail()


@route("DELETE", "/datasets/{id}")
def delete_dataset(req):
    wb.datasets.delete(req.params["id"])
    notify({"type": "tree_changed"})
    return None


@route("PUT", "/datasets/{id}/schema")
def set_schema(req):
    dataset = wb.datasets.get(req.params["id"])
    dataset.set_schema(body(req).get("slots"))
    notify({"type": "dataset_changed", "dataset": dataset.id, "schema": True})
    return dataset.detail()


@route("GET", "/datasets/{id}/stats")
def stats(req):
    return wb.datasets.get(req.params["id"]).stats()


@route("POST", "/datasets/{id}/check")
def check(req):
    return wb.check_dataset(req.params["id"])


@route("GET", "/datasets/{id}/entries")
def entries(req):
    dataset = wb.datasets.get(req.params["id"])
    return dataset.list_entries(number(req, "limit", 50, 500), number(req, "offset", 0, 10**9),
                                req.query.get("state") or None, req.query.get("q", ""))


@route("POST", "/datasets/{id}/entries")
def create_entry(req):
    dataset = wb.datasets.get(req.params["id"])
    entry = dataset.create_entry()
    notify({"type": "dataset_changed", "dataset": dataset.id, "entry": entry["id"]})
    return salus.Response.json(entry, status=201)


@route("GET", "/datasets/{id}/entries/{eid}")
def get_entry(req):
    return wb.datasets.get(req.params["id"]).get_entry(entry_id(req))


@route("DELETE", "/datasets/{id}/entries/{eid}")
def delete_entry(req):
    dataset = wb.datasets.get(req.params["id"])
    dataset.delete_entry(entry_id(req))
    notify({"type": "dataset_changed", "dataset": dataset.id, "entry": entry_id(req), "deleted": True})
    return None


@route("PUT", "/datasets/{id}/entries/{eid}/cells/{slot}")
def set_cell(req):
    dataset = wb.datasets.get(req.params["id"])
    entry = dataset.set_cell(entry_id(req), slot_id(req), body(req))
    notify({"type": "dataset_changed", "dataset": dataset.id, "entry": entry["id"]})
    return entry


@route("POST", "/datasets/{id}/entries/{eid}/cells/{slot}/mark-valid")
def mark_valid(req):
    dataset = wb.datasets.get(req.params["id"])
    entry = dataset.mark_valid(entry_id(req), slot_id(req))
    notify({"type": "dataset_changed", "dataset": dataset.id, "entry": entry["id"]})
    return entry


@route("GET", "/datasets/{id}/slots/{slot}/candidates")
def candidates(req):
    dataset = wb.datasets.get(req.params["id"])
    return dataset.candidates(slot_id(req), req.query.get("q", ""), number(req, "limit", 30, 200), number(req, "offset", 0, 10**6))


async def main(plugin):
    # Reading the setting asks Salus, which needs the running connection: so not while this module loads. A request that comes in
    # before this is done finds the setting itself (`wb.status()`).
    await asyncio.to_thread(wb.load)
    log.info("Workbench folder: %s", wb.root or "not set up yet")


if __name__ == "__main__":
    app.run(main)
