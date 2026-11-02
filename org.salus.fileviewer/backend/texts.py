"""Text files for the text viewer: read, decoded, and cut off when they are very large.

Binary files are refused (a NUL byte near the start). The encoding is UTF-8 (with or without a byte order mark), UTF-16
with a byte order mark, or else Windows-1252 / Latin-1, which can decode any byte, so old files still show.
"""
from __future__ import annotations

import codecs
from pathlib import Path

from errors import ViewerError

MAX_BYTES = 8 * 1024 * 1024  # more is shown cut off: the browser has to lay out every line
SNIFF_BYTES = 8192


def decode(data: bytes, truncated: bool) -> tuple[str, str]:
    """The text and the name of its encoding."""
    for bom, name in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            return data.decode(name, errors="replace"), "UTF-16" if name == "utf-16" else "UTF-8 (BOM)"
    if b"\0" in data[:SNIFF_BYTES]:
        raise ViewerError("This is not a text file (it contains binary data).", 415)
    for cut in range(4 if truncated else 1):  # a cut-off file may end inside a character
        try:
            return data[: len(data) - cut].decode("utf-8"), "UTF-8"
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("cp1252"), "Windows-1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "Latin-1"


def read(file: str) -> dict:
    path = Path(file).expanduser()
    if not path.is_file():
        raise ViewerError("That file does not exist on the server.", 404)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            data = f.read(MAX_BYTES)
    except OSError as e:
        raise ViewerError(f"The file cannot be read: {e.strerror or e}", 403) from None
    truncated = size > len(data)
    text, encoding = decode(data, truncated)
    return {"name": path.name, "size": size, "encoding": encoding, "truncated": truncated, "shown_bytes": len(data), "text": text}
