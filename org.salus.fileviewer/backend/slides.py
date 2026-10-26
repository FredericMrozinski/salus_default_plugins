"""Slide viewer: whole slide images as Deep Zoom pyramids for OpenSeadragon.

The frontend asks GET /slide/info?file=<path> (size, tile size, a slide id) and OpenSeadragon then asks for
GET /slide/tiles/<slide id>/<level>/<col>_<row>.jpeg. Each slide is opened once and kept; tiles are rendered on
demand with OpenSlide and the encoded JPEGs are kept in one size-capped in-memory LRU cache shared by all slides.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import threading
from collections import OrderedDict
from pathlib import Path

from errors import ViewerError

try:
    import openslide
    from openslide.deepzoom import DeepZoomGenerator
    from PIL import Image
    IMPORT_ERROR = None
except ImportError as e:  # the other viewers keep working without OpenSlide
    openslide = None
    IMPORT_ERROR = str(e)

log = logging.getLogger("fileviewer.slides")

TILE_SIZE = 254  # + 1px overlap on each side = 256px tiles
OVERLAP = 1
JPEG_QUALITY = 80
CACHE_BYTES = 512 * 1024 * 1024
PREWARM_MAX_TILES_PER_LEVEL = 64  # overview levels are rendered when a slide is opened
SLIDE_SUFFIXES = {".svs", ".tif", ".tiff", ".ndpi", ".scn", ".mrxs", ".vms", ".vmu", ".bif"}
TILE_NAME = re.compile(r"^(\d+)_(\d+)\.jpeg$")


class TileCache:
    """Encoded tiles in RAM: LRU limited by bytes, and concurrent requests for the same tile share one render."""

    def __init__(self, render, max_bytes: int):
        self._render = render  # blocking function (key) -> bytes, runs in a worker thread
        self._max_bytes = max_bytes
        self._entries: OrderedDict = OrderedDict()
        self._inflight: dict = {}
        self.bytes = 0
        self.hits = 0
        self.misses = 0

    async def get(self, key) -> bytes:
        data = self._entries.get(key)
        if data is not None:
            self._entries.move_to_end(key)
            self.hits += 1
            return data
        task = self._inflight.get(key)
        if task is None:
            self.misses += 1
            task = self._inflight[key] = asyncio.ensure_future(self._load(key))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())  # mark as retrieved
        # Shielded: a client giving up on its request must not cancel the shared render.
        return await asyncio.shield(task)

    async def _load(self, key) -> bytes:
        try:
            data = await asyncio.to_thread(self._render, key)
        finally:
            self._inflight.pop(key, None)
        self._entries[key] = data
        self.bytes += len(data)
        while self.bytes > self._max_bytes and len(self._entries) > 1:
            _, evicted = self._entries.popitem(last=False)
            self.bytes -= len(evicted)
        return data

    def stats(self) -> dict:
        return {"tiles": len(self._entries), "bytes": self.bytes, "hits": self.hits, "misses": self.misses}


def _slide_id(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha1(f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:12]


class Slide:
    """An opened slide file."""

    def __init__(self, path: Path):
        self.path = path
        self.slide = openslide.OpenSlide(str(path))
        self.id = _slide_id(path)
        self.dz = DeepZoomGenerator(self.slide, tile_size=TILE_SIZE, overlap=OVERLAP, limit_bounds=True)

    def render(self, level: int, col: int, row: int) -> bytes:
        tile = self.dz.get_tile(level, (col, row))
        if "A" in tile.getbands():
            # Transparent areas outside the scanned region: show them white like a glass slide.
            background = Image.new("RGB", tile.size, (255, 255, 255))
            background.paste(tile, mask=tile.getchannel("A"))
            tile = background
        out = io.BytesIO()
        tile.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
        return out.getvalue()

    def info(self) -> dict:
        props = self.slide.properties
        return {
            "id": self.id,
            "name": self.path.name,
            "width": self.slide.dimensions[0],
            "height": self.slide.dimensions[1],
            "tile_size": TILE_SIZE,
            "overlap": OVERLAP,
            "format": "jpeg",
            "vendor": props.get("openslide.vendor"),
            "objective": props.get("openslide.objective-power"),
            "mpp": props.get("openslide.mpp-x"),
        }


class Slides:
    """The opened slides and their shared tile cache."""

    def __init__(self):
        self.by_id: dict[str, Slide] = {}
        self._by_path: dict[str, Slide] = {}
        self._lock = threading.Lock()
        self.cache = TileCache(lambda key: self.by_id[key[0]].render(*key[1:]), CACHE_BYTES)

    def open(self, file: str) -> Slide:
        """Opens (or finds) the slide for a file path."""
        if openslide is None:
            raise ViewerError(f"The slide viewer needs OpenSlide, which is not installed ({IMPORT_ERROR}). Run backend/setup.sh.", 503)
        path = Path(file).expanduser()
        if path.suffix.lower() not in SLIDE_SUFFIXES or not path.is_file():
            raise ViewerError("That is not a slide file on the server.", 404)
        with self._lock:
            slide = self._by_path.get(str(path))
            if slide is not None and slide.id == _slide_id(path):
                return slide
            try:
                slide = Slide(path)
            except openslide.OpenSlideError as e:
                raise ViewerError(f"OpenSlide cannot read this file: {e}", 415) from None
            self.by_id[slide.id] = slide
            self._by_path[str(path)] = slide
            log.info("Opened %s: %dx%d px, %d deep zoom levels", path.name, *slide.slide.dimensions, slide.dz.level_count)
            return slide

    async def tile(self, slide_id: str, level: str, tile: str) -> bytes:
        slide = self.by_id.get(slide_id)
        match = TILE_NAME.match(tile)
        if slide is None or not match or not level.isdigit():
            raise ViewerError("No such tile.", 404)
        dz = slide.dz
        lv, col, row = int(level), int(match[1]), int(match[2])
        if lv >= dz.level_count or col >= dz.level_tiles[lv][0] or row >= dz.level_tiles[lv][1]:
            raise ViewerError("No such tile.", 404)
        return await self.cache.get((slide.id, lv, col, row))

    async def prewarm(self, slide: Slide) -> None:
        """Render the overview levels up front so the first view appears immediately."""
        dz = slide.dz
        for level in range(dz.level_count):
            cols, rows = dz.level_tiles[level]
            if cols * rows > PREWARM_MAX_TILES_PER_LEVEL:
                break
            await asyncio.gather(*(self.cache.get((slide.id, level, c, r)) for c in range(cols) for r in range(rows)))
        log.info("Overview levels of %s cached: %s", slide.path.name, self.cache.stats())
