#!/usr/bin/env python3
"""Backup backend: bundles of folders backed up on demand with restic into the backup root (a Salus setting).

Passwords are sent in request bodies (never in URLs), handed to restic on its standard input and never stored.

  GET  /settings                     the backup root and whether restic is there      PUT /settings {root}
  GET  /bundles                      the bundles in the backup root, with their running or last job
  POST /bundles {name, password, folders, excludes}                                   PUT /bundles/{name} {folders, excludes}
  POST /bundles/{name}/unlock {password}            checks the password
  POST /bundles/{name}/snapshots {password}         POST /bundles/{name}/ls {password, snapshot, path}
  POST /bundles/{name}/backup {password}            POST /bundles/{name}/restore {password, snapshot, items, target, strip, overwrite}
  POST /bundles/{name}/cleanup {password, keep_last}   POST /bundles/{name}/check {password}
  GET  /bundles/{name}/job                          POST /bundles/{name}/job/stop
  GET  /fs?path=                     server folders, for choosing folders      POST /fs/mkdir {parent, name}
"""
import asyncio
import logging
import subprocess
from pathlib import Path

import restic
import salus_sdk as salus
from bundles import Bundles
from errors import BackupError

log = logging.getLogger("backup")
app = salus.Plugin()
bundles = Bundles()
NO_CACHE = [("cache-control", "no-store")]
NOT_SET_UP = 412


def guarded(handler):
    async def wrapped(req):
        try:
            return await handler(req)
        except BackupError as e:
            raise salus.HTTPError(e.status, str(e)) from None
    wrapped.__name__ = handler.__name__
    return wrapped


def body(req) -> dict:
    data = req.body.as_json() if len(req.body) else {}
    if not isinstance(data, dict):
        raise salus.HTTPError(400, "Expected a JSON object.")
    return data


def password_of(data: dict) -> str:
    password = data.get("password")
    if not isinstance(password, str) or not password:
        raise BackupError("Enter the bundle's password.", 401)
    return password


async def root() -> Path:
    value = await app.config.get("backup_root")
    if not value:
        raise BackupError("The backup folder is not set yet.", NOT_SET_UP)
    return Path(value).expanduser()


async def bundle_of(req):
    return bundles.get(await root(), req.params["name"])


def restic_version() -> dict:
    try:
        result = subprocess.run([restic.binary(), "version"], capture_output=True, text=True, timeout=10)
        return {"ok": result.returncode == 0, "version": result.stdout.strip().split(" compiled")[0]}
    except BackupError as e:
        return {"ok": False, "error": str(e)}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": f"restic cannot be started: {e}"}


# --------------------------------------------------------------------------- settings

@app.route("GET", "/settings")
async def get_settings(req):
    value = await app.config.get("backup_root")
    return salus.Response.json({"root": value or None, "root_exists": bool(value) and Path(value).expanduser().is_dir(),
                                "restic": await asyncio.to_thread(restic_version)}, headers=NO_CACHE)


@app.route("PUT", "/settings")
@guarded
async def put_settings(req):
    value = str(body(req).get("root") or "").strip()
    path = Path(value).expanduser()
    if not value or not path.is_absolute():
        raise BackupError("Enter the full path of the backup folder (it starts with / or ~).")
    if path.exists() and not path.is_dir():
        raise BackupError("That path is a file.")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupError(f"The folder cannot be created: {e.strerror or e}") from None
    await app.config.set("backup_root", str(path))
    return {"root": str(path)}


# --------------------------------------------------------------------------- bundles

@app.route("GET", "/bundles")
@guarded
async def list_bundles(req):
    found = await asyncio.to_thread(bundles.list, await root())
    return salus.Response.json({"bundles": [{**b.info(), "job": bundles.status(b.name)} for b in found]}, headers=NO_CACHE)


@app.route("POST", "/bundles")
@guarded
async def create_bundle(req):
    data = body(req)
    bundle = await asyncio.to_thread(bundles.create, await root(), data.get("name"), password_of(data),
                                     data.get("folders"), data.get("excludes"))
    return bundle.info()


@app.route("PUT", "/bundles/{name}")
@guarded
async def update_bundle(req):
    data = body(req)
    bundle = await bundle_of(req)
    bundle.save_definition(data.get("folders"), data.get("excludes"))
    return bundle.info()


@app.route("POST", "/bundles/{name}/unlock")
@guarded
async def unlock(req):
    await asyncio.to_thread(bundles.unlock, await bundle_of(req), password_of(body(req)))
    return {}


@app.route("POST", "/bundles/{name}/snapshots")
@guarded
async def snapshots(req):
    result = await asyncio.to_thread(bundles.snapshots, await bundle_of(req), password_of(body(req)))
    return salus.Response.json({"snapshots": result}, headers=NO_CACHE)


@app.route("POST", "/bundles/{name}/ls")
@guarded
async def ls(req):
    data = body(req)
    entries = await asyncio.to_thread(bundles.list_dir, await bundle_of(req), password_of(data), data.get("snapshot"), data.get("path", "/"))
    return salus.Response.json({"entries": entries}, headers=NO_CACHE)


@app.route("POST", "/bundles/{name}/backup")
@guarded
async def backup(req):
    return bundles.backup(await bundle_of(req), password_of(body(req))).status()


@app.route("POST", "/bundles/{name}/restore")
@guarded
async def restore(req):
    data = body(req)
    target = data.get("target")
    return bundles.restore(await bundle_of(req), password_of(data), data.get("snapshot"), data.get("items") or [],
                           None if target in (None, "") else target, bool(data.get("strip")), bool(data.get("overwrite"))).status()


@app.route("POST", "/bundles/{name}/cleanup")
@guarded
async def cleanup(req):
    data = body(req)
    try:
        keep = int(data.get("keep_last"))
    except (TypeError, ValueError):
        raise BackupError("Enter how many snapshots to keep.") from None
    return bundles.forget(await bundle_of(req), password_of(data), keep).status()


@app.route("POST", "/bundles/{name}/check")
@guarded
async def check(req):
    return bundles.check(await bundle_of(req), password_of(body(req))).status()


@app.route("GET", "/bundles/{name}/job")
async def job(req):
    return salus.Response.json({"job": bundles.status(req.params["name"])}, headers=NO_CACHE)


@app.route("POST", "/bundles/{name}/job/stop")
async def stop(req):
    current = bundles.jobs.get(req.params["name"])
    if current and current.state == "running":
        current.stop()
    return {}


# --------------------------------------------------------------------------- choosing folders

@app.route("GET", "/fs")
@guarded
async def browse(req):
    path = Path(req.query.get("path") or "~").expanduser()
    if not path.is_dir():
        path = path.parent if path.parent.is_dir() else Path.home()

    def listing():
        folders = []
        try:
            for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if entry.is_dir() and not entry.name.startswith("."):
                        folders.append({"name": entry.name})
                except OSError:
                    continue
        except OSError as e:
            raise BackupError(f"That folder cannot be read: {e.strerror or e}") from None
        return folders

    return salus.Response.json({"path": str(path), "folders": await asyncio.to_thread(listing), "files": []}, headers=NO_CACHE)


@app.route("POST", "/fs/mkdir")
@guarded
async def make_folder(req):
    """Creates a folder inside an existing one (for choosing a new backup folder or restore target)."""
    data = body(req)
    parent = Path(str(data.get("parent") or "")).expanduser()
    name = str(data.get("name") or "").strip()
    if not parent.is_absolute() or not parent.is_dir():
        raise BackupError("Choose an existing folder to create the new folder in.")
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise BackupError("Enter a name without slashes.")
    target = parent / name
    if target.exists():
        raise BackupError("There is something with that name there already.")
    try:
        target.mkdir()
    except OSError as e:
        raise BackupError(f"The folder cannot be created: {e.strerror or e}") from None
    return {"path": str(target)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()
