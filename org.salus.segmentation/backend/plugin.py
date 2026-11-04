#!/usr/bin/env python3
"""Segmentation backend: annotation projects, the images in them, classes, and the polygons drawn on the images.

The four components (browser, viewer, tools, classes) share one state, kept here: the open project, the image, the
selected class and tool. Every change is broadcast on the "events" channel, and each component redraws what concerns it.

  GET  /state                                  projects, the open project and the shared state
  PUT  /state {image?, class_id?, tool?, mode?, brush?, opacity?, hidden?, sensitivity?}   sensitivity: Border growth's colours
  POST /projects {name, root, extensions, save} | /projects/open {save} | /projects/{id}/use | /projects/{id}/forget
  GET  /fs?path=                               server folders, for choosing the root and save folders
  GET  /images?rescan=1                        the images of the open project, with their shape counts and statuses
  POST /images/remove {images}  GET /images/removed  POST /images/restore {images}   (the files stay)
  PUT  /images/status {image | images, status} PUT /statuses {statuses: [{id?, name, color}]}
  POST /classes {name, color}                  PATCH /classes/{id} {name?, color?}
  GET  /slide/info?image=  GET /slide/tiles/{slide}/{level}/{tile}
  GET  /annotations?image=                     GeoJSON          GET /annotations/summary?image= (shapes per class)
  POST /annotations/polygon {image, class_id, points, simplify}
  POST /annotations/paint {image, points, radius, class_id}  /annotations/erase {image, points, radius}
  POST /annotations/undo {image}  /annotations/redo {image}  /annotations/delete {image, id}  /annotations/rename {image, id, name}
  POST /focus {image, bbox, id?}               the viewer zooms to a region (a shape picked in the class editor)
  POST /smartbrush/dab {image, stroke, x, y, scale, mode, inner, method}  /smartbrush/outline {image, stroke, points?, scale?, inner?, method?}
  POST /smartbrush/commit {image, stroke, class_id}  /smartbrush/cancel {stroke}
  POST /import/scan, /import/apply             QuPath GeoJSON (see qupath.py)
  GET  /export/settings  POST /export/start {settings, images, overwrite}  GET /export/status  POST /export/cancel
"""
import asyncio
import functools
import logging
import pkgutil
import threading
from pathlib import Path

import salus_sdk as salus
from annotations import Annotations, count_shapes, parse_points
from errors import ViewerError
import qupath
from export import ExportJob, load_shapes
import models
from project import Project, Registry
from slides import Slides

log = logging.getLogger("segmentation")
app = salus.Plugin()
registry = Registry(Path.home() / ".salus" / "segmentation-projects.json")
slides = Slides()
NO_CACHE = [("cache-control", "no-store")]
TILE_CACHE = [("cache-control", "private, max-age=86400, immutable")]
TOOLS = ("pan", "brush", "eraser", "polygon")


def load_model_modes() -> list[dict]:
    """The modes of the segmentation models in models/ (see modelbrush.py); none if there are none, or no PyTorch."""
    if not any(pkgutil.iter_modules(models.__path__)):
        return []  # nothing to load: PyTorch is not imported at startup
    try:
        import modelbrush
    except ImportError as e:
        log.warning("segmentation models left out: %s", e)
        return []
    modelbrush.load_models()
    return [{"id": s.id, "name": s.name, "about": s.about, "model": True, "output": s.output} for s in modelbrush.SEGMENTERS.values()]


# How brush, eraser and polygon work: "exact" draws what is drawn; the others are smart modes, where they follow the
# image: "border" grows the stroke from its border through the colours of the brush's inner circle (borderbrush.py, on the
# stroke machinery of smartbrush.py), then one mode per model in models/.
MODE_LIST = [
    {"id": "exact", "name": "Exact", "about": "Brush, eraser and polygon draw exactly what you draw"},
    {"id": "border", "name": "Border Growth", "about": "Brush, eraser and polygon follow the image: each dab grows the painted area from its border into the colors of the inner circle, stopping at edges"},
    *load_model_modes(),
]
MODES = tuple(m["id"] for m in MODE_LIST)

lock = threading.RLock()
project: Project | None = None
annotation_cache: dict[str, Annotations] = {}
shape_counts: dict[str, int] = {}
# brush: the tool width in screen pixels (brush and eraser diameter; twice the smart tools' inner radius)
# hidden: ids of the classes whose shapes the viewer does not show (and the eraser leaves alone)
state = {"image": None, "class_id": None, "tool": "pan", "mode": "exact", "brush": 32, "opacity": 0.35, "hidden": [],
         "sensitivity": "normal"}  # sensitivity: Border growth's colours, "normal" or "strict" (see borderbrush)


def broadcast(event: dict) -> None:
    try:
        app.send_blocking(salus.Payload.json(event), channel="events", to=salus.ALL)
    except Exception as e:  # nobody listening, or the connection is going away
        log.debug("broadcast failed: %s", e)


def guarded(handler):
    def wrapped(req):
        try:
            return handler(req)
        except ViewerError as e:
            raise salus.HTTPError(e.status, str(e)) from None
    wrapped.__name__ = handler.__name__
    return wrapped


def body(req) -> dict:
    data = req.body.as_json() if len(req.body) else {}
    if not isinstance(data, dict):
        raise salus.HTTPError(400, "Expected a JSON object.")
    return data


def need_project() -> Project:
    if project is None:
        raise ViewerError("No project is open.", 409)
    return project


def annotations_of(image: str) -> Annotations:
    p = need_project()
    with lock:
        store = annotation_cache.get(image)
        if store is None:
            store = annotation_cache[image] = Annotations(p, image)
        return store


def full_state() -> dict:
    with lock:
        known = []
        for entry in registry.load():
            known.append({**entry, "missing": not (Path(entry["save"]) / "project.json").is_file()})
        return {"projects": known, "project": project.info() if project else None, "modes": MODE_LIST, **state}


def use_project(p: Project) -> None:
    global project
    with lock:
        project = p
        annotation_cache.clear()
        shape_counts.clear()
        registry.remember(p)
        state["image"] = None
        state["hidden"] = []
        classes = p.data["classes"]
        state["class_id"] = classes[0]["id"] if classes else None
    broadcast({"type": "state", "state": full_state()})


def reveal(class_id: int) -> None:
    """Drawing with a hidden class shows it again, so that what is drawn can be seen."""
    with lock:
        if class_id not in state["hidden"]:
            return
        state["hidden"] = [i for i in state["hidden"] if i != class_id]
    broadcast({"type": "state", "state": full_state()})


def hidden_classes() -> set[int]:
    with lock:
        return set(state["hidden"])


def changed_annotations(image: str, store: Annotations) -> dict:
    shape_counts[image] = len(store.shapes)
    broadcast({"type": "annotations", "image": image, **store.summary()})
    return store.summary()


# --------------------------------------------------------------------------- state and projects

@app.route("GET", "/state")
def get_state(req):
    return salus.Response.json(full_state(), headers=NO_CACHE)


@app.route("PUT", "/state")
@guarded
def put_state(req):
    data = body(req)
    with lock:
        if "image" in data:
            image = data["image"]
            if image is not None:
                need_project().image_path(image)
            state["image"] = image
        if "class_id" in data:
            if data["class_id"] is not None:
                need_project().cls(int(data["class_id"]))
            state["class_id"] = data["class_id"]
        if "tool" in data:
            if data["tool"] not in TOOLS:
                raise ViewerError("Unknown tool.")
            state["tool"] = data["tool"]
        if "mode" in data:
            if data["mode"] not in MODES:
                raise ViewerError("Unknown mode.")
            state["mode"] = data["mode"]
        if "brush" in data:
            state["brush"] = max(4, min(200, int(data["brush"])))
        if "sensitivity" in data:
            if data["sensitivity"] not in ("normal", "strict"):
                raise ViewerError("Unknown sensitivity.")
            state["sensitivity"] = data["sensitivity"]
        if "opacity" in data:
            state["opacity"] = max(0.0, min(1.0, float(data["opacity"])))
        if "hidden" in data:
            if not isinstance(data["hidden"], list):
                raise ViewerError("Give the hidden classes as a list.")
            hidden = []
            for class_id in data["hidden"]:
                need_project().cls(int(class_id))
                if int(class_id) not in hidden:
                    hidden.append(int(class_id))
            state["hidden"] = hidden
    result = full_state()
    broadcast({"type": "state", "state": result})
    return result


@app.route("POST", "/projects")
@guarded
def create_project(req):
    data = body(req)
    use_project(Project.create(data.get("name"), data.get("root"), data.get("extensions", ".svs"), data.get("save")))
    return full_state()


@app.route("POST", "/projects/open")
@guarded
def open_project(req):
    use_project(Project.open(body(req).get("save")))
    return full_state()


@app.route("POST", "/projects/{id}/use")
@guarded
def use(req):
    entry = next((e for e in registry.load() if e.get("id") == req.params["id"]), None)
    if entry is None:
        raise ViewerError("Unknown project.", 404)
    use_project(Project.open(entry["save"]))
    return full_state()


@app.route("POST", "/projects/{id}/forget")
@guarded
def forget(req):
    """Removes a project from the list. Its folder and files stay as they are."""
    global project
    registry.forget(req.params["id"])
    with lock:
        if project and project.id == req.params["id"]:
            project = None
            annotation_cache.clear()
            state["image"] = None
    broadcast({"type": "state", "state": full_state()})
    return full_state()


@app.route("GET", "/fs")
@guarded
def browse(req):
    path = Path(req.query.get("path") or "~").expanduser()
    if not path.is_dir():
        path = path.parent if path.parent.is_dir() else Path.home()
    folders, files = [], []
    try:
        for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    folders.append({"name": entry.name})
                elif entry.is_file():
                    files.append({"name": entry.name})
            except OSError:
                continue
    except OSError as e:
        raise ViewerError(f"That folder cannot be read: {e.strerror or e}") from None
    return salus.Response.json({"path": str(path), "folders": folders, "files": files[:500]}, headers=NO_CACHE)


# --------------------------------------------------------------------------- images and classes

@app.route("GET", "/images")
@guarded
def images(req):
    p = need_project()
    paths = p.images(rescan=req.query.get("rescan") == "1")
    out = []
    for rel in paths:
        if rel not in shape_counts:
            shape_counts[rel] = count_shapes(p.annotation_file(rel))
        out.append({"path": rel, "shapes": shape_counts[rel], "status": p.status_of(rel)})
    return salus.Response.json({"images": out}, headers=NO_CACHE)


@app.route("PUT", "/images/status")
@guarded
def set_image_status(req):
    """The status of one image ({image, status}) or of several ({images: [...], status})."""
    data = body(req)
    p = need_project()
    images = data["images"] if "images" in data else [data.get("image")]
    if not isinstance(images, list) or not images:
        raise ViewerError("Choose at least one image.")
    for image in images:
        p.image_path(image)
    status = p.set_status(images, data.get("status"))
    broadcast({"type": "status", "images": images, "status": status})
    return {"images": images, "status": status}


@app.route("POST", "/images/remove")
@guarded
def remove_images(req):
    """Takes images out of the project; their files and annotations stay. The open image, if removed, is closed."""
    p = need_project()
    removed = p.remove_images(body(req).get("images"))
    with lock:
        closed = state["image"] in removed
        if closed:
            state["image"] = None
    broadcast({"type": "images"})
    if closed:
        broadcast({"type": "state", "state": full_state()})
    return {"removed": removed}


@app.route("GET", "/images/removed")
@guarded
def removed_images(req):
    return salus.Response.json({"images": need_project().removed_images()}, headers=NO_CACHE)


@app.route("POST", "/images/restore")
@guarded
def restore_images(req):
    restored = need_project().restore_images(body(req).get("images"))
    broadcast({"type": "images"})
    return {"restored": restored}


@app.route("PUT", "/statuses")
@guarded
def set_statuses(req):
    """The whole list of statuses, edited: [{id?, name, color}] in order (the first is the default)."""
    statuses = need_project().set_statuses(body(req).get("statuses"))
    broadcast({"type": "state", "state": full_state()})
    return {"statuses": statuses}


@app.route("POST", "/classes")
@guarded
def add_class(req):
    data = body(req)
    new = need_project().add_class(data.get("name"), data.get("color"))
    with lock:
        if state["class_id"] is None:
            state["class_id"] = new["id"]
    broadcast({"type": "state", "state": full_state()})
    return new


@app.route("PATCH", "/classes/{id}")
@guarded
def update_class(req):
    p = need_project()
    try:
        class_id = int(req.params["id"])
    except ValueError:
        raise ViewerError("No such class.", 404) from None
    updated = p.update_class(class_id, body(req))
    # The class names and colors are written into the annotation files, so rewrite the ones of this project.
    with lock:
        for store in annotation_cache.values():
            store.save()
    threading.Thread(target=rewrite_annotation_files, args=(p,), daemon=True).start()
    broadcast({"type": "state", "state": full_state()})
    return updated


def rewrite_annotation_files(p: Project) -> None:
    folder = p.save / "annotations"
    for file in folder.rglob("*.geojson") if folder.is_dir() else []:
        rel = file.relative_to(folder).as_posix()[: -len(".geojson")]
        if rel in annotation_cache:
            continue
        try:
            Annotations(p, rel).save()
        except Exception as e:
            log.warning("could not update %s: %s", file, e)


# --------------------------------------------------------------------------- the image itself

@app.route("GET", "/slide/info")
async def slide_info(req):
    try:
        path = need_project().image_path(req.query.get("image", ""))
        # A viewer shows one image at a time: let go of the one it showed before (closed if no other viewer has it).
        await asyncio.to_thread(slides.release, req.frontend_id)
        slide = await asyncio.to_thread(slides.open, str(path), req.frontend_id)
    except ViewerError as e:
        raise salus.HTTPError(e.status, str(e)) from None
    asyncio.ensure_future(slides.prewarm(slide))  # the overview levels, so the first view appears at once
    # The tiles show the scanned area only (OpenSlide's bounds); annotations use the coordinates of the whole level 0,
    # like QuPath. The viewer adds this offset.
    props = slide.slide.properties
    offset = [int(props.get("openslide.bounds-x", 0) or 0), int(props.get("openslide.bounds-y", 0) or 0)]
    return salus.Response.json({**slide.info(), "offset": offset, "cache": slides.cache.stats()}, headers=NO_CACHE)


@app.route("GET", "/slide/tiles/{slide}/{level}/{tile}")
async def slide_tile(req):
    try:
        data = await slides.tile(req.params["slide"], req.params["level"], req.params["tile"])
    except ViewerError as e:
        raise salus.HTTPError(e.status, str(e)) from None
    return salus.Response.bytes(data, content_type="image/jpeg", headers=TILE_CACHE)


# --------------------------------------------------------------------------- annotations

@app.route("GET", "/annotations")
@guarded
def get_annotations(req):
    store = annotations_of(req.query.get("image", ""))
    with store.lock:
        return salus.Response.json({**store.to_geojson(), **store.summary()}, headers=NO_CACHE)


@app.route("GET", "/annotations/summary")
@guarded
def annotations_summary(req):
    """The numbers of shapes (in all and per class) of an image, without the shapes."""
    store = annotations_of(req.query.get("image", ""))
    with store.lock:
        return salus.Response.json(store.summary(), headers=NO_CACHE)


@app.route("POST", "/annotations/polygon")
@guarded
def add_polygon(req):
    data = body(req)
    image = data.get("image")
    store = annotations_of(image)
    try:
        class_id = int(data.get("class_id"))
        simplify = max(0.0, float(data.get("simplify") or 0))
    except (TypeError, ValueError):
        raise ViewerError("Choose a class first.") from None
    new_id = store.add_polygon(data.get("points"), class_id, simplify)
    reveal(class_id)
    return {"id": new_id, **changed_annotations(image, store)}


@app.route("POST", "/annotations/paint")
@guarded
def paint(req):
    data = body(req)
    image = data.get("image")
    store = annotations_of(image)
    try:
        radius = float(data.get("radius"))
        class_id = int(data.get("class_id"))
    except (TypeError, ValueError):
        raise ViewerError("Choose a class first.") from None
    changed = int(store.paint(data.get("points"), radius, class_id) is not None)
    if changed:
        reveal(class_id)
    return {"changed": changed, **(changed_annotations(image, store) if changed else store.summary())}


@app.route("POST", "/annotations/erase")
@guarded
def erase(req):
    data = body(req)
    image = data.get("image")
    store = annotations_of(image)
    try:
        radius = float(data.get("radius"))
    except (TypeError, ValueError):
        raise ViewerError("Invalid eraser size.") from None
    changed = store.erase(data.get("points"), radius, hidden_classes())
    return {"changed": changed, **(changed_annotations(image, store) if changed else store.summary())}


# --------------------------------------------------------------------------- smart brush (see smartbrush.py)

strokes: dict[tuple[int, str], object] = {}  # (frontend id, stroke id) -> smartbrush.Stroke


def smartbrush_module():
    try:
        import smartbrush
    except ImportError as e:  # the rest of the plugin works without torch
        raise ViewerError(f"The smart brush needs PyTorch, which is not installed ({e}). Run backend/setup.sh.", 503) from None
    return smartbrush


def stroke_class(sb, method):
    """The stroke of a smart mode: Border growth, or the regions of a segmentation model."""
    if method in (None, "border"):
        import borderbrush
        return functools.partial(borderbrush.BorderStroke, reach=border_setting("border_reach"),
                                 follow=border_setting("border_follow", True), sensitivity=state["sensitivity"],
                                 snap=border_setting("border_snap", True), smooth=border_setting("border_smooth", True))
    import modelbrush
    model = modelbrush.SEGMENTERS.get(method)
    if model is None:
        raise ViewerError("Unknown smart mode.")
    return functools.partial(modelbrush.ModelStroke, model)


def border_setting(key, default=None):
    """A Border growth setting (see configurable.toml); `default` if it cannot be read."""
    try:
        return app.config.get_blocking(key, default)
    except Exception as e:  # noqa: BLE001 - a setting must not stop the brush
        log.warning("%s not read: %s", key, e)
        return default


@app.route("POST", "/smartbrush/dab")
@guarded
def smart_dab(req):
    """Adds dabs to a stroke (started by its first dab) up to the point (x, y), in level-0 pixels.
    `scale` is screen pixels per level-0 pixel when the stroke started; `mode` is "paint" or "erase"; `method` the
    smart mode ("border" or a model's id). `segmenting`: some dabs wait for the model; the viewer asks again."""
    sb = smartbrush_module()
    data = body(req)
    image = data.get("image")
    key = (req.frontend_id, str(data.get("stroke")))
    try:
        x, y = float(data["x"]), float(data["y"])
    except (KeyError, TypeError, ValueError):
        raise ViewerError("Invalid point.") from None
    with lock:
        stroke = strokes.get(key)
        if stroke is None:
            if data.get("mode") not in ("paint", "erase"):
                raise ViewerError("Unknown brush mode.")
            path = need_project().image_path(image)
            stroke = stroke_class(sb, data.get("method"))(image, data["mode"], sb.check_scale(data.get("scale")), x, y,
                                                           sb.check_inner(data.get("inner")))
            stroke.slide = slides.open(str(path), req.frontend_id)  # the viewer has it open already
            strokes[key] = stroke
    if stroke.image != image:
        raise ViewerError("This stroke belongs to another image.", 409)
    with stroke.lock:
        dabs = stroke.to_dab(sb.slide_reader(stroke.slide), x, y)
        return {"dabs": dabs, "segmenting": stroke.waiting}


@app.route("POST", "/smartbrush/outline")
@guarded
def smart_outline(req):
    """A smart polygon: the first call (with `points`, level-0 pixels, and `scale`) starts the stroke; each call adds
    the dabs along the outline that fit in about a second. The viewer calls again until `remaining` is 0, then commits."""
    sb = smartbrush_module()
    data = body(req)
    image = data.get("image")
    key = (req.frontend_id, str(data.get("stroke")))
    with lock:
        stroke = strokes.get(key)
        if stroke is None:
            if data.get("points") is None:  # cancelled meanwhile
                raise ViewerError("No such stroke.", 404)
            points = parse_points(data.get("points"))
            if len(points) < 3:
                raise ViewerError("A polygon needs at least three points.")
            path = need_project().image_path(image)
            stroke = stroke_class(sb, data.get("method"))(image, "paint", sb.check_scale(data.get("scale")), *points[0],
                                                           sb.check_inner(data.get("inner")))
            stroke.outline(points)
            stroke.slide = slides.open(str(path), req.frontend_id)
            strokes[key] = stroke
    if stroke.image != image:
        raise ViewerError("This stroke belongs to another image.", 409)
    with stroke.lock:
        dabs = stroke.outline_dabs(sb.slide_reader(stroke.slide))
        return {"dabs": dabs, "remaining": len(stroke.todo), "segmenting": stroke.waiting}


@app.route("POST", "/smartbrush/commit")
@guarded
def smart_commit(req):
    """Ends a stroke: paints its area with the class (merged into touching shapes of that class) or erases it."""
    data = body(req)
    with lock:
        stroke = strokes.pop((req.frontend_id, str(data.get("stroke"))), None)
    if stroke is None:
        raise ViewerError("No such stroke.", 404)
    store = annotations_of(stroke.image)
    with stroke.lock:
        geometry = stroke.geometry()
    if geometry is None:
        return {"changed": 0, **store.summary()}
    if stroke.mode == "erase":
        changed = store.erase_area(geometry, hidden_classes())
    else:
        try:
            class_id = int(data.get("class_id"))
        except (TypeError, ValueError):
            raise ViewerError("Choose a class first.") from None
        changed = int(store.add_area(geometry, class_id) is not None)
        if changed:
            reveal(class_id)
    return {"changed": changed, **(changed_annotations(stroke.image, store) if changed else store.summary())}


@app.route("POST", "/smartbrush/cancel")
def smart_cancel(req):
    with lock:
        strokes.pop((req.frontend_id, str(body(req).get("stroke"))), None)
    return {}


@app.route("POST", "/annotations/delete")
@guarded
def delete_shape(req):
    data = body(req)
    store = annotations_of(data.get("image"))
    store.delete(str(data.get("id")))
    return changed_annotations(data.get("image"), store)


@app.route("POST", "/annotations/rename")
@guarded
def rename_shape(req):
    """Names a shape ({image, id, name}); an empty name gives it back its generic one. Can be undone."""
    data = body(req)
    store = annotations_of(data.get("image"))
    if store.rename(str(data.get("id")), data.get("name")):
        return changed_annotations(data.get("image"), store)
    return store.summary()


@app.route("POST", "/focus")
@guarded
def focus(req):
    """Asks the viewer to show a region ({image, bbox: [x0, y0, x1, y1] in level-0 pixels, id?}), e.g. a shape picked in
    the class editor; the viewer fits it into the middle of its view and outlines the shape `id`. Without a bbox the
    picked shape is let go ({image, id: null}), e.g. after a click beside it in the viewer."""
    data = body(req)
    if data.get("bbox") is None:
        broadcast({"type": "focus", "image": data.get("image"), "bbox": None, "id": None})
        return {}
    try:
        bbox = [float(v) for v in data.get("bbox")]
    except (TypeError, ValueError):
        raise ViewerError("Invalid region.") from None
    if len(bbox) != 4:
        raise ViewerError("Invalid region.")
    broadcast({"type": "focus", "image": data.get("image"), "bbox": bbox, "id": data.get("id")})
    return {}


@app.route("POST", "/annotations/undo")
@guarded
def undo(req):
    image = body(req).get("image")
    store = annotations_of(image)
    return changed_annotations(image, store) if store.undo() else store.summary()


@app.route("POST", "/annotations/redo")
@guarded
def redo(req):
    image = body(req).get("image")
    store = annotations_of(image)
    return changed_annotations(image, store) if store.redo() else store.summary()


# --------------------------------------------------------------------------- importing QuPath GeoJSON

def import_sources(data: dict) -> tuple[list[tuple[Path, str]], list[dict]]:
    """The QuPath files to import and the image each one belongs to: one file for one image, or a folder matched by name."""
    p = need_project()
    path = Path(str(data.get("path") or "").strip()).expanduser()
    if not path.is_absolute():
        raise ViewerError("Enter the full path of the QuPath file or folder.")
    if path.is_dir():
        return qupath.match_folder(path, p.images())
    if path.is_file():
        image = data.get("image")
        if not image:
            raise ViewerError("Open the image the file belongs to first.")
        p.image_path(image)
        return [(path, image)], []
    raise ViewerError("That path does not exist on the server.", 404)


@app.route("POST", "/import/scan")
@guarded
def import_scan(req):
    """What an import would bring in: the files and their images, the QuPath classes with their numbers of shapes."""
    matches, left_out = import_sources(body(req))
    files, classes, skipped = [], {}, 0
    for file, image in matches:
        try:
            counts, not_areas = qupath.summarize(qupath.read(file))
        except ViewerError as e:
            left_out.append({"file": str(file), "reason": str(e)})
            continue
        existing = len(annotations_of(image).shapes)
        files.append({"file": str(file), "image": image, "shapes": sum(counts.values()), "existing": existing})
        skipped += not_areas
        for name, count in counts.items():
            classes[name] = classes.get(name, 0) + count
    return {"files": files, "left_out": left_out, "skipped": skipped,
            "classes": [{"name": n, "count": c} for n, c in sorted(classes.items(), key=lambda kv: (kv[0] == "", kv[0].lower()))]}


@app.route("POST", "/import/apply")
@guarded
def import_apply(req):
    """Writes the matched shapes into the project's annotations; the QuPath files are only read."""
    data = body(req)
    p = need_project()
    mode = data.get("mode")
    if mode not in ("merge", "replace"):
        raise ViewerError("Choose whether to add to or replace the existing annotations.")
    mapping = {}
    for name, class_id in (data.get("mapping") or {}).items():
        if class_id is not None:
            p.cls(int(class_id))
            mapping[str(name)] = int(class_id)
    if not mapping:
        raise ViewerError("Match at least one QuPath class to a class of the project.")
    matches, _ = import_sources(data)
    imported_files, imported_shapes = 0, 0
    for file, image in matches:
        try:
            shapes = qupath.shapes_for(qupath.read(file), mapping)
        except ViewerError:
            continue
        store = annotations_of(image)
        if shapes:  # a file without matched shapes changes nothing, even when replacing
            imported_shapes += store.import_shapes(shapes, replace=mode == "replace")
            imported_files += 1
            changed_annotations(image, store)
    return {"files": imported_files, "shapes": imported_shapes}


# --------------------------------------------------------------------------- exporting pixel maps

export_job: ExportJob | None = None


@app.route("GET", "/export/settings")
@guarded
def export_settings(req):
    return salus.Response.json(need_project().export_settings(), headers=NO_CACHE)


@app.route("POST", "/export/start")
@guarded
def export_start(req):
    global export_job
    data = body(req)
    p = need_project()
    with lock:
        if export_job and export_job.state == "running":
            raise ViewerError("An export is running already.", 409)
        settings = p.save_export_settings(data.get("settings") or {})
        known = set(p.images())
        images = [str(i) for i in data.get("images") or [] if str(i) in known]

        def shapes_of(image):
            with lock:
                store = annotation_cache.get(image) if project is p else None
            return list(store.shapes) if store else load_shapes(p, image)

        export_job = ExportJob(p, images, settings, bool(data.get("overwrite")), shapes_of)
        export_job.start()
    broadcast({"type": "export"})
    return export_job.status()


@app.route("GET", "/export/status")
def export_status(req):
    return salus.Response.json(export_job.status() if export_job else {"state": "idle", "items": []}, headers=NO_CACHE)


@app.route("POST", "/export/cancel")
def export_cancel(req):
    if export_job:
        export_job.cancel()
    return {}


@app.on_frontend_detached
async def detached(frontend_id):
    with lock:
        for key in [k for k in strokes if k[0] == frontend_id]:
            del strokes[key]
    await asyncio.to_thread(slides.release, frontend_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()
