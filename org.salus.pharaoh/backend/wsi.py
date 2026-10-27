"""Finding and inspecting whole slide images (headers only; no pixels are read)."""
from __future__ import annotations

import os
from pathlib import Path

try:
    import pyvips
    # The operation cache would hand back an image that still has the progress callback of an earlier run
    # (a crash), and keeps slide files open. Slides are read once anyway.
    pyvips.cache_set_max(0)
    pyvips.cache_set_max_mem(0)
    IMPORT_ERROR = None
except (ImportError, OSError) as e:  # OSError: libvips is missing
    pyvips = None
    IMPORT_ERROR = str(e)

# What a folder is searched for. A single file can be added whatever it is called.
SLIDE_SUFFIXES = {".tif", ".tiff", ".svs", ".ndpi", ".scn", ".mrxs", ".vms", ".vmu", ".bif"}
PART_SUFFIX = ".pharaoh-part"  # unfinished output
MAX_PAGES = 32


def is_slide_name(name: str) -> bool:
    return Path(name).suffix.lower() in SLIDE_SUFFIXES and not name.startswith(".") and not name.endswith(PART_SUFFIX)


def find_slides(folder: Path) -> list[Path]:
    """Every slide below a folder, sorted by path."""
    found = []
    for directory, subdirs, files in os.walk(folder, onerror=lambda e: None):
        subdirs[:] = sorted(d for d in subdirs if not d.startswith("."))
        found += [Path(directory) / name for name in sorted(files) if is_slide_name(name)]
    return found


def inspect(path: Path) -> dict:
    """Size, bands and the number of resolution levels of a file; raises ValueError with a readable message."""
    if pyvips is None:
        raise ValueError(f"libvips is not available ({IMPORT_ERROR}). Run backend/setup.sh.")
    try:
        image = pyvips.Image.new_from_file(str(path))
    except pyvips.Error:
        raise ValueError("This file cannot be read as an image (libvips does not know the format or the file is damaged).") from None
    pages = image.get("n-pages") if image.get_typeof("n-pages") else 1
    widths = [image.width]
    for page in range(1, min(pages, MAX_PAGES)):
        try:
            widths.append(pyvips.Image.new_from_file(str(path), page=page).width)
        except pyvips.Error:
            break
    # A level is a page that is clearly smaller than the previous one; label and thumbnail pages count as levels, too.
    levels, last = 0, None
    for width in sorted(widths, reverse=True):
        if last is None or width < last / 1.5:
            levels, last = levels + 1, width
    return {"width": image.width, "height": image.height, "bands": image.bands, "pages": pages, "levels": levels}
