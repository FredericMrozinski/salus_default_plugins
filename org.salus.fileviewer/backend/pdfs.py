"""PDF viewer: renders pages on the server and sends images, so the PDF file itself never reaches the browser.

  GET /pdf/info?file=<path>                 page count and page sizes (in points)
  GET /pdf/page/<doc id>/<page>?scale=<n>   one page as a PNG; zooming asks for a sharper rendering

The browser has nothing to save or print: it only ever holds the images of the pages it displays.
"""
from __future__ import annotations

import hashlib
import io
import logging
import threading
from collections import OrderedDict
from pathlib import Path

from errors import ViewerError

try:
    import pypdfium2 as pdfium
    IMPORT_ERROR = None
except ImportError as e:  # the other viewers keep working without it
    pdfium = None
    IMPORT_ERROR = str(e)

log = logging.getLogger("fileviewer.pdfs")

MAX_OPEN_DOCS = 8
CACHE_BYTES = 256 * 1024 * 1024
MIN_SCALE, MAX_SCALE = 0.25, 8.0
MAX_SIDE_PIXELS = 6000  # one page image never gets larger than this on its longer side


class Doc:
    def __init__(self, path: Path):
        stat = path.stat()
        self.path = path
        self.id = hashlib.sha1(f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:12]
        self.pdf = pdfium.PdfDocument(str(path))
        self.sizes = [tuple(round(v, 2) for v in self.pdf.get_page_size(i)) for i in range(len(self.pdf))]

    def info(self) -> dict:
        return {"id": self.id, "name": self.path.name, "pages": len(self.sizes), "sizes": self.sizes}

    def close(self) -> None:
        try:
            self.pdf.close()
        except Exception:
            pass


class Pdfs:
    def __init__(self):
        self._docs: OrderedDict[str, Doc] = OrderedDict()  # by id, least recently used first
        self._by_path: dict[str, str] = {}
        self._lock = threading.Lock()  # PDFium is not thread-safe: one call at a time
        self._cache: OrderedDict[tuple, bytes] = OrderedDict()
        self._cache_bytes = 0

    def open(self, file: str) -> Doc:
        if pdfium is None:
            raise ViewerError(f"The PDF viewer needs pypdfium2, which is not installed ({IMPORT_ERROR}). Run backend/setup.sh.", 503)
        path = Path(file).expanduser()
        if path.suffix.lower() != ".pdf" or not path.is_file():
            raise ViewerError("That is not a PDF file on the server.", 404)
        with self._lock:
            known = self._docs.get(self._by_path.get(str(path), ""))
            stat = path.stat()
            if known is not None and known.id == hashlib.sha1(f"{path}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()[:12]:
                self._docs.move_to_end(known.id)
                return known
            try:
                doc = Doc(path)
            except pdfium.PdfiumError as e:
                raise ViewerError(f"This PDF cannot be opened (it may be damaged or password protected): {e}", 415) from None
            self._docs[doc.id] = doc
            self._by_path[str(path)] = doc.id
            while len(self._docs) > MAX_OPEN_DOCS:
                _, old = self._docs.popitem(last=False)
                old.close()
            return doc

    def render(self, doc_id: str, page: int, scale: float) -> bytes:
        """The page as PNG. The scale is rounded up to a quarter step, so zooming back and forth reuses renderings."""
        scale = min(MAX_SCALE, max(MIN_SCALE, scale))
        scale = -(-scale * 4 // 1) / 4
        with self._lock:
            doc = self._docs.get(doc_id)
            if doc is None:
                raise ViewerError("This document is no longer open; reload the viewer.", 404)
            if not 0 <= page < len(doc.sizes):
                raise ViewerError("No such page.", 404)
            width, height = doc.sizes[page]
            scale = min(scale, MAX_SIDE_PIXELS / max(width, height, 1))
            key = (doc_id, page, round(scale, 3))
            data = self._cache.get(key)
            if data is not None:
                self._cache.move_to_end(key)
                return data
            pdf_page = doc.pdf[page]
            try:
                image = pdf_page.render(scale=scale).to_pil()
            finally:
                pdf_page.close()
            out = io.BytesIO()
            image.convert("RGB").save(out, "PNG", compress_level=3)
            data = out.getvalue()
            self._cache[key] = data
            self._cache_bytes += len(data)
            while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
                _, evicted = self._cache.popitem(last=False)
                self._cache_bytes -= len(evicted)
            return data
