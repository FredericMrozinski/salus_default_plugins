"""Slide viewer: whole slide images as Deep Zoom pyramids for OpenSeadragon.

The frontend asks GET /slide/info?file=<path> (size, tile size, a slide id) and OpenSeadragon then asks for
GET /slide/tiles/<slide id>/<level>/<col>_<row>.jpeg. Each slide is opened once and shared by all viewers of it; tiles
are rendered on demand with OpenSlide and the encoded JPEGs are kept in one size-capped in-memory LRU cache shared by
all slides. A slide is let go of (file handle, overview, cached tiles) when the last viewer that opened it is closed.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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
# Slides without a pyramid (a single full-resolution level) would make OpenSlide read the whole slide into memory
# for every zoomed-out tile. Those levels are cut from one overview, built once in small blocks.
MAX_DIRECT_READ = 4096  # px per side that may be read from a slide level for one tile; larger reads use the overview
OVERVIEW_REDUCTION = 16  # the overview is this much smaller than level 0
OVERVIEW_BLOCK = 4096  # level-0 pixels per side of the blocks the overview is read in
SLIDE_SUFFIXES = {".svs", ".tif", ".tiff", ".ndpi", ".scn", ".mrxs", ".vms", ".vmu", ".bif"}
TILE_NAME = re.compile(r"^(\d+)_(\d+)\.jpeg$")


class TileCache:
    """Encoded tiles in RAM: LRU limited by bytes, and concurrent requests for the same tile share one render."""

    def __init__(self, render, max_bytes: int, alive=lambda key: True):
        self._render = render  # blocking function (key) -> bytes, runs in a worker thread
        self._max_bytes = max_bytes
        self._alive = alive  # False for a key whose slide has been let go of meanwhile: its tile is not kept
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
        if not self._alive(key):
            return data
        self._entries[key] = data
        self.bytes += len(data)
        while self.bytes > self._max_bytes and len(self._entries) > 1:
            _, evicted = self._entries.popitem(last=False)
            self.bytes -= len(evicted)
        return data

    def drop(self, matches) -> None:
        """Forget the tiles whose key `matches` (a function of the key)."""
        for key in [k for k in self._entries if matches(k)]:
            self.bytes -= len(self._entries.pop(key))

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
        self._overview = None
        self._overview_lock = threading.Lock()
        self.viewers: set[int] = set()  # frontend ids that opened this slide; the slide is closed when none is left
        self._state = threading.Lock()
        self._active = 0  # renders in progress: the file is not closed under them
        self._closing = False

    def close(self) -> None:
        """Lets go of the file and the overview, now or (if tiles are being rendered) when the last one is done."""
        with self._state:
            self._closing = True
            if not self._active:
                self._free()

    def _free(self) -> None:
        self._overview = None
        self.slide.close()

    def _enter(self) -> None:
        with self._state:
            if self._closing:
                raise ViewerError("This slide was closed.", 404)
            self._active += 1

    def _exit(self) -> None:
        with self._state:
            self._active -= 1
            if self._closing and not self._active:
                self._free()

    def _needs_overview(self, level: int, col: int, row: int) -> bool:
        """True if OpenSlide would have to read a huge region of the slide for this tile (no usable pyramid level)."""
        _, _, read_size = self.dz.get_tile_coordinates(level, (col, row))
        return max(read_size) > MAX_DIRECT_READ

    def overview(self):
        """The whole slide at 1/OVERVIEW_REDUCTION, on white, read block by block so that memory stays small."""
        with self._overview_lock:
            if self._overview is None:
                width, height = self.slide.dimensions
                step = OVERVIEW_REDUCTION
                image = Image.new("RGB", (-(-width // step), -(-height // step)), (255, 255, 255))
                def read(origin):
                    x, y = origin
                    w, h = min(OVERVIEW_BLOCK, width - x), min(OVERVIEW_BLOCK, height - y)
                    block = self.slide.read_region((x, y), 0, (w, h))
                    flat = Image.new("RGB", block.size, (255, 255, 255))
                    flat.paste(block, mask=block.getchannel("A"))
                    return origin, flat.reduce(step)

                origins = [(x, y) for y in range(0, height, OVERVIEW_BLOCK) for x in range(0, width, OVERVIEW_BLOCK)]
                with ThreadPoolExecutor(max_workers=4) as pool:  # decoding releases the GIL; 4 blocks of ~64 MB at a time
                    for (x, y), part in pool.map(read, origins):
                        image.paste(part, (x // step, y // step))
                self._overview = image
                log.info("Built the overview of %s: %dx%d px", self.path.name, *image.size)
            return self._overview

    def _overview_tile(self, level: int, col: int, row: int):
        (x, y), _, _ = self.dz.get_tile_coordinates(level, (col, row))
        size = self.dz.get_tile_dimensions(level, (col, row))
        reduction = 2 ** (self.dz.level_count - 1 - level)
        overview = self.overview()
        box = (x / OVERVIEW_REDUCTION, y / OVERVIEW_REDUCTION,
               min(overview.width, (x + size[0] * reduction) / OVERVIEW_REDUCTION),
               min(overview.height, (y + size[1] * reduction) / OVERVIEW_REDUCTION))
        return overview.resize(size, Image.BILINEAR, box=box, reducing_gap=2.0)

    def read_region(self, location: tuple[int, int], level: int, size: tuple[int, int]):
        """OpenSlide's read_region (RGBA), safe against the slide being closed meanwhile (used by the smart brush)."""
        self._enter()
        try:
            return self.slide.read_region(location, level, size)
        finally:
            self._exit()

    def render(self, level: int, col: int, row: int) -> bytes:
        self._enter()
        try:
            return self._render(level, col, row)
        finally:
            self._exit()

    def _render(self, level: int, col: int, row: int) -> bytes:
        tile = self._overview_tile(level, col, row) if self._needs_overview(level, col, row) else self.dz.get_tile(level, (col, row))
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
        self.cache = TileCache(self._render, CACHE_BYTES, alive=lambda key: key[0] in self.by_id)

    def _render(self, key) -> bytes:
        slide = self.by_id.get(key[0])
        if slide is None:
            raise ViewerError("No such tile.", 404)
        return slide.render(*key[1:])

    def open(self, file: str, viewer: int) -> Slide:
        """Opens (or finds) the slide for a file path on behalf of a viewer (a frontend id), which holds it until
        `release(viewer)`."""
        if openslide is None:
            raise ViewerError(f"The slide viewer needs OpenSlide, which is not installed ({IMPORT_ERROR}). Run backend/setup.sh.", 503)
        path = Path(file).expanduser()
        if path.suffix.lower() not in SLIDE_SUFFIXES or not path.is_file():
            raise ViewerError("That is not a slide file on the server.", 404)
        with self._lock:
            slide = self._by_path.get(str(path))
            if slide is not None and slide.id == _slide_id(path):
                slide.viewers.add(viewer)
                return slide
            try:
                slide = Slide(path)
            except openslide.OpenSlideError as e:
                raise ViewerError(f"OpenSlide cannot read this file: {e}", 415) from None
            slide.viewers.add(viewer)
            self.by_id[slide.id] = slide
            self._by_path[str(path)] = slide
            log.info("Opened %s: %dx%d px, %d deep zoom levels", path.name, *slide.slide.dimensions, slide.dz.level_count)
            return slide

    def release(self, viewer: int) -> None:
        """A viewer was closed: slides that nobody else has open are closed and their tiles dropped."""
        with self._lock:
            for slide in [s for s in self.by_id.values() if viewer in s.viewers]:
                slide.viewers.discard(viewer)
                if slide.viewers:
                    continue
                del self.by_id[slide.id]
                if self._by_path.get(str(slide.path)) is slide:
                    del self._by_path[str(slide.path)]
                self.cache.drop(lambda key, id=slide.id: key[0] == id)
                slide.close()
                log.info("Closed %s (no viewer left)", slide.path.name)

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
        try:
            for level in range(dz.level_count):
                cols, rows = dz.level_tiles[level]
                if cols * rows > PREWARM_MAX_TILES_PER_LEVEL:
                    break
                await asyncio.gather(*(self.cache.get((slide.id, level, c, r)) for c in range(cols) for r in range(rows)))
        except ViewerError:
            return  # the viewer was closed meanwhile
        log.info("Overview levels of %s cached: %s", slide.path.name, self.cache.stats())
