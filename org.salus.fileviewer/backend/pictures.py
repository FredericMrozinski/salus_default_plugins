"""Image viewer: common image files (PNG, JPEG, ...) as Deep Zoom tiles for OpenSeadragon, rendered with Pillow.

  GET /image/info?file=<path>                         size, tile size and an id
  GET /image/tiles/<id>/<level>/<col>_<row>.<ext>     one tile (PNG or JPEG)

The file is decoded once; the browser only ever receives tiles, never the original file. Big images stay fast because
only the visible tiles are cut out. (Whole slide images have their own viewer, slides.py.)
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import re
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from errors import ViewerError
from slides import TileCache

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
    IMPORT_ERROR = None
except ImportError as e:
    Image = None
    IMPORT_ERROR = str(e)

try:  # decodes HEIC/HEIF inside Pillow, if the package is installed (it is optional)
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_IN_PILLOW = True
except ImportError:
    HEIF_IN_PILLOW = False

log = logging.getLogger("fileviewer.pictures")

TILE_SIZE = 254  # + 1px overlap on each side = 256px tiles
OVERLAP = 1
JPEG_QUALITY = 88
MAX_PIXELS = 120_000_000  # decoded images are kept in memory; refuse anything bigger (also guards against decompression bombs)
MAX_OPEN = 6
CACHE_BYTES = 256 * 1024 * 1024
HEIF_SUFFIXES = {".heic", ".heif"}
SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} | HEIF_SUFFIXES
TILE_NAME = re.compile(r"^(\d+)_(\d+)\.(png|jpeg)$")


# Without pillow-heif, HEIC is converted to PNG by a tool the system already has.
HEIF_CONVERTERS = (
    lambda src, out: ["sips", "-s", "format", "png", str(src), "--out", str(out)],  # macOS
    lambda src, out: ["heif-convert", str(src), str(out)],  # libheif's command line tool (Linux: libheif-examples)
)


def convert_heif(path: Path, folder: Path) -> Path:
    out = folder / "converted.png"
    for command in HEIF_CONVERTERS:
        args = command(path, out)
        if shutil.which(args[0]) is None:
            continue
        try:
            subprocess.run(args, check=True, capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("%s failed for %s: %s", args[0], path, e)
            continue
        produced = sorted(folder.glob("*.png"))  # heif-convert numbers the files of a multi-image HEIC
        if produced:
            return produced[0]
    tools = [c(path, out)[0] for c in HEIF_CONVERTERS if shutil.which(c(path, out)[0])]
    if tools:
        raise ViewerError("This HEIC image cannot be converted: the file is damaged or uses an unsupported variant.", 415)
    raise ViewerError("HEIC images need the pillow-heif package (pip install pillow-heif in backend/.venv), "
                      "or the macOS tool sips, or libheif's heif-convert on the server.", 415)


@contextmanager
def open_image(path: Path):
    """Pillow's lazily decoded image for a file; HEIC without pillow-heif goes through a converter first."""
    if path.suffix.lower() in HEIF_SUFFIXES and not HEIF_IN_PILLOW:
        with tempfile.TemporaryDirectory(prefix="fileviewer-") as folder:  # deleted as soon as the image is decoded
            with Image.open(convert_heif(path, Path(folder))) as image:
                yield image
    else:
        with Image.open(path) as image:
            yield image


def to_rgb(image):
    """Whatever the file holds (palette, 16 bit, grayscale, transparency, CMYK ...) as plain RGB for display."""
    image = ImageOps.exif_transpose(image)  # phone photos carry their rotation in the metadata
    if image.mode in ("I;16", "I;16L", "I;16B", "I"):
        image = image.convert("I").point(lambda v: v * (1 / 256.0)).convert("L")
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))  # transparent areas show white
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


class Picture:
    def __init__(self, path: Path):
        stat = path.stat()
        self.path = path
        self.id = hashlib.sha1(f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:12]
        with open_image(path) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ViewerError(f"This image is too large to show ({source.width} × {source.height} px; the limit is {MAX_PIXELS // 1_000_000} megapixels).", 413)
            source.seek(0)  # animated GIF/WebP: the first frame
            self.base = to_rgb(source)
        self.width, self.height = self.base.size
        self.max_level = max(0, math.ceil(math.log2(max(self.width, self.height, 1))))
        self.format = "jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "png"
        self._levels = {self.max_level: self.base}
        self._lock = threading.Lock()

    def level_size(self, level: int) -> tuple[int, int]:
        scale = 2 ** (self.max_level - level)
        return max(1, math.ceil(self.width / scale)), max(1, math.ceil(self.height / scale))

    def level_image(self, level: int):
        """The image at a Deep Zoom level: each level is the one above it, halved."""
        with self._lock:
            return self._level(level)

    def _level(self, level: int):
        if level not in self._levels:
            self._levels[level] = self._level(level + 1).resize(self.level_size(level), Image.LANCZOS)
        return self._levels[level]

    def tile(self, level: int, col: int, row: int) -> bytes:
        image = self.level_image(level)
        x0, y0 = max(0, col * TILE_SIZE - OVERLAP), max(0, row * TILE_SIZE - OVERLAP)
        x1, y1 = min(image.width, (col + 1) * TILE_SIZE + OVERLAP), min(image.height, (row + 1) * TILE_SIZE + OVERLAP)
        out = io.BytesIO()
        crop = image.crop((x0, y0, x1, y1))
        if self.format == "jpeg":
            crop.save(out, "JPEG", quality=JPEG_QUALITY)
        else:
            crop.save(out, "PNG", compress_level=3)
        return out.getvalue()

    def info(self) -> dict:
        return {"id": self.id, "name": self.path.name, "width": self.width, "height": self.height,
                "tile_size": TILE_SIZE, "overlap": OVERLAP, "format": self.format}


class Pictures:
    def __init__(self):
        self.by_id: OrderedDict[str, Picture] = OrderedDict()  # least recently used first
        self._by_path: dict[str, str] = {}
        self._lock = threading.Lock()
        self.cache = TileCache(lambda key: self.by_id[key[0]].tile(*key[1:]), CACHE_BYTES)

    def open(self, file: str) -> Picture:
        if Image is None:
            raise ViewerError(f"The image viewer needs Pillow, which is not installed ({IMPORT_ERROR}). Run backend/setup.sh.", 503)
        path = Path(file).expanduser()
        if path.suffix.lower() not in SUFFIXES or not path.is_file():
            raise ViewerError("That is not an image file on the server.", 404)
        with self._lock:
            stat = path.stat()
            known = self.by_id.get(self._by_path.get(str(path), ""))
            if known is not None and known.id == hashlib.sha1(f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:12]:
                self.by_id.move_to_end(known.id)
                return known
            try:
                picture = Picture(path)
            except ViewerError:
                raise
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
                log.warning("Cannot read %s: %s", path, e)  # the details (with server paths) stay in the log
                raise ViewerError("This image cannot be read: the file is damaged or not a supported image.", 415) from None
            self.by_id[picture.id] = picture
            self._by_path[str(path)] = picture.id
            while len(self.by_id) > MAX_OPEN:
                self.by_id.popitem(last=False)
            log.info("Opened %s: %dx%d px, %d levels", path.name, picture.width, picture.height, picture.max_level + 1)
            return picture

    async def tile(self, picture_id: str, level: str, tile: str) -> tuple[bytes, str]:
        picture = self.by_id.get(picture_id)
        match = TILE_NAME.match(tile)
        if picture is None or not match or not level.isdigit():
            raise ViewerError("No such tile.", 404)
        lv, col, row = int(level), int(match[1]), int(match[2])
        if lv > picture.max_level:
            raise ViewerError("No such tile.", 404)
        width, height = picture.level_size(lv)
        if col * TILE_SIZE >= width or row * TILE_SIZE >= height:
            raise ViewerError("No such tile.", 404)
        return await self.cache.get((picture.id, lv, col, row)), picture.format
