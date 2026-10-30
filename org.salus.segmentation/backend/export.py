"""Exporting annotations as pixel maps: one pyramidal TIFF per image in which every pixel has the color of its class.

The map has the size of the image's full-resolution level (divided by the chosen downsample) and the same coordinates as
the annotations. Where shapes of different classes overlap, the class that comes first in the export settings wins.
The map is drawn in tiles (Pillow), so memory stays small for gigapixel slides; libvips joins the tiles and writes the
pyramid. Compression is lossless (deflate), so the colors stay exact.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw
from shapely import STRtree
from shapely.affinity import scale
from shapely.geometry import box

from annotations import Annotations, hex_to_rgb, polygonal
from errors import ViewerError
from project import Project, write_json_atomic

try:
    import pyvips
    pyvips.cache_set_max(0)  # a cached image would keep the progress handler of an earlier export (a crash)
    VIPS_ERROR = None
except (ImportError, OSError) as e:
    pyvips = None
    VIPS_ERROR = str(e)

try:
    import openslide
except ImportError:
    openslide = None

log = logging.getLogger("segmentation.export")

TILE = 4096  # pixels per side of the tiles the map is drawn in
OUT_TILE = 512  # tile size inside the TIFF
PART_SUFFIX = ".part"
LEGEND_FILE = "classes.json"


class Cancelled(Exception):
    pass


def output_name(rel: str) -> str:
    """case1/slide.svs -> case1/slide.tif"""
    folder, _, name = rel.rpartition("/")
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{folder}/{stem}.tif" if folder else f"{stem}.tif"


def image_size(path: Path) -> tuple[int, int, float | None]:
    """Width and height of the full-resolution level, and microns per pixel if known."""
    if openslide is None:
        raise ViewerError("OpenSlide is not installed. Run backend/setup.sh.", 503)
    slide = openslide.OpenSlide(str(path))
    try:
        mpp = slide.properties.get(openslide.PROPERTY_NAME_MPP_X)
        return slide.dimensions[0], slide.dimensions[1], float(mpp) if mpp else None
    finally:
        slide.close()


def render(shapes, size: tuple[int, int], downsample: int, classes: list[dict], background: str,
           check=lambda: None, progress=lambda fraction: None):
    """The pixel map as a libvips image. `classes` are the included classes in priority order (the first wins)."""
    width, height = (math.ceil(n / downsample) for n in size)
    bg = tuple(hex_to_rgb(background))
    # Lowest priority first, so that higher classes are painted over it.
    layers = []
    for c in reversed(classes):
        geometries = [scale(s.geometry, 1 / downsample, 1 / downsample, origin=(0, 0)) for s in shapes if s.class_id == c["id"]]
        if geometries:
            layers.append((tuple(hex_to_rgb(c["color"])), geometries, STRtree(geometries)))

    background_tile = {}

    def plain(w, h):
        if (w, h) not in background_tile:
            background_tile[(w, h)] = (pyvips.Image.black(w, h, bands=3) + list(bg)).cast("uchar")
        return background_tile[(w, h)]

    columns, rows = math.ceil(width / TILE), math.ceil(height / TILE)
    tiles = []
    for row in range(rows):
        for col in range(columns):
            check()
            x0, y0 = col * TILE, row * TILE
            w, h = min(TILE, width - x0), min(TILE, height - y0)
            area = box(x0 - 2, y0 - 2, x0 + w + 2, y0 + h + 2)
            hits = [(color, [geometries[i] for i in tree.query(area, predicate="intersects")]) for color, geometries, tree in layers]
            if not any(found for _, found in hits):
                tiles.append(plain(w, h))
            else:
                picture = Image.new("RGB", (w, h), bg)
                for color, found in hits:
                    if found:
                        picture.paste(color, mask=class_mask(found, area, x0, y0, w, h))
                tile = pyvips.Image.new_from_memory(picture.tobytes(), w, h, 3, "uchar")
                # Kept compressed until libvips writes the file; a plain tile would cost 3 bytes per pixel.
                tiles.append(pyvips.Image.new_from_buffer(tile.tiffsave_buffer(compression="deflate", tile=True), ""))
            progress(len(tiles) / (columns * rows))
    # arrayjoin places every tile on a grid of full-size cells; the edge tiles are smaller, so cut the padding off.
    return pyvips.Image.arrayjoin(tiles, across=columns).crop(0, 0, width, height).copy(interpretation="srgb")


def class_mask(geometries, area, x0: int, y0: int, w: int, h: int) -> Image.Image:
    """Where one class has shapes in a tile: a 1-bit mask. Holes stay empty unless another shape of the class covers them."""
    mask = Image.new("1", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    for geometry in geometries:
        part = polygonal(geometry.intersection(area))  # big shapes: only the piece in this tile
        if part is None:
            continue
        for polygon in getattr(part, "geoms", [part]):
            ring = lambda r: [(x - x0, y - y0) for x, y in r.coords]  # noqa: E731
            if not polygon.interiors:
                draw.polygon(ring(polygon.exterior), fill=1)
                continue
            own = Image.new("1", (w, h), 0)
            own_draw = ImageDraw.Draw(own)
            own_draw.polygon(ring(polygon.exterior), fill=1)
            for hole in polygon.interiors:
                own_draw.polygon(ring(hole), fill=0)
            mask = ImageChops.logical_or(mask, own)
            draw = ImageDraw.Draw(mask)
    return mask


class ExportJob:
    """Exports the pixel maps of some images, one after the other, in a thread."""

    def __init__(self, project: Project, images: list[str], settings: dict, overwrite: bool, shapes_of):
        if pyvips is None:
            raise ViewerError(f"libvips is not available ({VIPS_ERROR}). Run backend/setup.sh.", 503)
        if not settings["folder"]:
            raise ViewerError("Choose the folder the pixel maps go to.")
        if not images:
            raise ViewerError("Choose at least one image.")
        self.project = project
        self.settings = settings
        self.overwrite = overwrite
        self.shapes_of = shapes_of  # image -> its shapes
        self.folder = Path(settings["folder"])
        self.classes = [c for c in settings["classes"] if c["include"]]
        if not self.classes:
            raise ViewerError("Include at least one class.")
        self.items = [{"image": rel, "file": output_name(rel), "status": "pending", "percent": 0.0, "message": ""} for rel in images]
        self.state = "running"
        self.started = time.time()
        self.finished = None
        self.cancelled = threading.Event()
        self.thread = threading.Thread(target=self.run, name="export", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self.cancelled.set()

    def status(self) -> dict:
        return {"state": self.state, "started": self.started, "finished": self.finished, "folder": str(self.folder),
                "items": [dict(item) for item in self.items]}

    def run(self) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.write_legend()
            for item in self.items:
                if self.cancelled.is_set():
                    break
                self.export(item)
        except Exception as e:  # the folder cannot be created, say
            log.exception("export failed")
            for item in self.items:
                if item["status"] == "pending":
                    item.update(status="failed", message=str(e))
        finally:
            for item in self.items:
                if item["status"] == "pending":
                    item.update(status="cancelled", message="Not started")
            self.finished = time.time()
            self.state = "cancelled" if self.cancelled.is_set() else "finished"

    def write_legend(self) -> None:
        """classes.json: which color is which class, so the maps can be read without this plugin."""
        legend = {
            "project": self.project.data["name"],
            "background": {"color": self.settings["background"], "rgb": hex_to_rgb(self.settings["background"])},
            "classes": [{"id": c["id"], "name": c["name"], "color": c["color"], "rgb": hex_to_rgb(c["color"]), "priority": i + 1}
                        for i, c in enumerate(self.classes)],
            "overlaps": "Where shapes of several classes overlap, the pixel gets the class with the smallest priority number.",
            "downsample": self.settings["downsample"],
            "images": [{"image": item["image"], "file": item["file"]} for item in self.items],
        }
        write_json_atomic(self.folder / LEGEND_FILE, legend)

    def export(self, item: dict) -> None:
        target = self.folder / item["file"]
        part = target.with_name(target.name + PART_SUFFIX)
        if target.exists() and not self.overwrite:
            item.update(status="skipped", message="The file exists already")
            return
        item["status"] = "running"

        def check():
            if self.cancelled.is_set():
                raise Cancelled()

        try:
            width, height, mpp = image_size(self.project.image_path(item["image"]))
            downsample = self.settings["downsample"]
            mask = render(self.shapes_of(item["image"]), (width, height), downsample, self.classes, self.settings["background"],
                          check, lambda f: item.update(percent=round(40 * f, 1)))
            mask.set_progress(True)

            def on_progress(image, p):
                item["percent"] = round(40 + 0.6 * p.percent, 1)
                if self.cancelled.is_set():
                    image.set_kill(True)

            mask.signal_connect("eval", on_progress)
            target.parent.mkdir(parents=True, exist_ok=True)
            # The resolution (pixels per millimetre), so that viewers show the right scale; libvips writes it in the unit given.
            resolution = {"xres": 1000 / (mpp * downsample), "yres": 1000 / (mpp * downsample), "resunit": "cm"} if mpp else {}
            mask.tiffsave(str(part), tile=True, tile_width=OUT_TILE, tile_height=OUT_TILE, pyramid=True,
                          compression="deflate", predictor="horizontal", bigtiff=True, **resolution)
            os.replace(part, target)
            item.update(status="done", percent=100.0, message=f"{math.ceil(width / downsample)} × {math.ceil(height / downsample)} px")
        except Cancelled:
            item.update(status="cancelled", message="Stopped")
        except Exception as e:
            if self.cancelled.is_set():
                item.update(status="cancelled", message="Stopped")
            else:
                log.warning("export of %s failed: %s", item["image"], e)
                item.update(status="failed", message=str(e).strip().splitlines()[0] if str(e).strip() else "Failed")
        finally:
            part.unlink(missing_ok=True)


def load_shapes(project: Project, image: str):
    return Annotations(project, image).shapes
