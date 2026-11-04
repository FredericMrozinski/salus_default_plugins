"""Model brushes: smart brushes driven by a segmentation model running in the background.

A model is registered with `register()` (see models/__init__.py) and becomes a mode in the tools, next to Exact and
Border Growth. When a stroke reaches a part of the slide the model has not segmented yet, the view around the brush
(the model's `view` x `view` screen pixels, at the stroke's zoom) is read and handed to the model in a background thread.
A model returns either

  - labels (output="labels"): one region label per pixel (0 = no region). A dab paints (or erases) everything inside the
    brush circle (the width set in the tools) that belongs to the region under the brush's centre; or
  - edges (output="edges"): 1 (or a value above `threshold`, e.g. a probability) where there is an edge, 0 elsewhere.
    A dab casts rays from the brush's centre, one per degree, that stop at the first edge (or the circle), and paints
    the area within their ends, up to and including the edge line. A ray far longer (or shorter) than its neighbours
    on both sides (within `neighbours` degrees), e.g. one that went through a hole in an edge, is interpolated between them,
    so holes in the edges do not let the fill out. Gaps up to 2 * `close` pixels wide are also closed beforehand.

Brush, eraser and polygon work like their colour versions otherwise.

Models may take seconds, so a dab that has to wait raises smartbrush.Pending: the request returns at once with
`segmenting`, and the viewer asks again. Segmented views are kept (CACHE_SIZE), so painting again in the same place and
zoom is immediate. One view is segmented at a time.
"""
from __future__ import annotations

import functools
import importlib
import logging
import pkgutil
import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import numpy as np

import smartbrush as sb
from errors import ViewerError

log = logging.getLogger("segmentation.models")
CACHE_SIZE = 12


@dataclass(frozen=True)
class Segmenter:
    id: str        # the mode's id in the shared state ("exact" and "border" are taken)
    name: str      # shown in the tools
    segment: Callable[[np.ndarray], np.ndarray]  # RGB view (view x view x 3, uint8) -> view x view map (see `output`)
    about: str = ""  # tooltip
    view: int = 1024  # screen pixels per side of the view the model gets
    output: str = "labels"  # "labels": region ids (0 = none); "edges": edge map (above `threshold` = edge)
    threshold: float = 0.5  # edges: a pixel is an edge if its value is above this
    close: int = 1  # edges: gaps up to 2 * close pixels wide are closed (0: none)
    neighbours: float = 10  # edges: a ray is compared with the rays this many degrees on either side


SEGMENTERS: dict[str, Segmenter] = {}


def register(id: str, name: str, segment: Callable[[np.ndarray], np.ndarray], about: str = "", view: int = 1024,
             output: str = "labels", threshold: float = 0.5, close: int = 1, neighbours: float = 10) -> Segmenter:
    """Makes a model available as a smart mode. `segment` is called in a background thread, one view at a time; load
    heavy things (weights) on its first call rather than at import, so the plugin starts quickly. `output` says what it
    returns: "labels" (a region id per pixel) or "edges" (an edge map, see the module docstring)."""
    if id in ("exact", "border") or not id.isidentifier():
        raise ValueError(f"invalid mode id {id!r}")
    if output not in ("labels", "edges"):
        raise ValueError(f"output must be 'labels' or 'edges', not {output!r}")
    SEGMENTERS[id] = Segmenter(id, name, segment, about, view, output, threshold, max(0, int(close)), float(neighbours))
    return SEGMENTERS[id]


def load_models() -> list[str]:
    """Imports the modules in models/ (each registers its model). A module that fails is logged and left out."""
    import models
    for info in pkgutil.iter_modules(models.__path__):
        try:
            importlib.import_module(f"models.{info.name}")
        except Exception:
            log.exception("model module %s could not be loaded", info.name)
    return list(SEGMENTERS)


# --------------------------------------------------------------------------- segmented views

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="segmentation-model")
_lock = threading.Lock()
_views: OrderedDict[tuple, "View"] = OrderedDict()
_jobs: dict[tuple, tuple["View", Future]] = {}


class View:
    """A segmented square of the slide: label map (size x size; for edge models a bool edge map) whose pixel (0, 0)
    starts at the level-0 point (x, y), at `scale` screen pixels per level-0 pixel."""

    def __init__(self, model: Segmenter, image: str, scale: float, x: float, y: float):
        self.model, self.image, self.scale, self.x, self.y = model, image, scale, x, y
        self.size = model.view
        self.labels: np.ndarray | None = None

    @property
    def key(self) -> tuple:
        return (self.model.id, self.image, self.scale, round(self.x, 3), round(self.y, 3))

    def pixel(self, px: float, py: float) -> tuple[float, float]:
        return (px - self.x) * self.scale, (py - self.y) * self.scale

    def covers(self, model: Segmenter, image: str, scale: float, px: float, py: float, r: float) -> bool:
        if model.id != self.model.id or image != self.image or abs(scale / self.scale - 1) > 1e-6:
            return False
        cx, cy = self.pixel(px, py)
        return r <= cx <= self.size - r and r <= cy <= self.size - r


def _segment(view: View, slide) -> View:
    rgb = np.ascontiguousarray(sb.read_view(slide, view.x, view.y, view.scale, view.size))
    out = np.asarray(view.model.segment(rgb))
    if out.shape != rgb.shape[:2]:
        raise ValueError(f"the model returned a map of shape {out.shape}, not {rgb.shape[:2]}")
    if view.model.output == "edges":
        view.labels = out > view.model.threshold
    else:
        view.labels = out.astype(np.int32, copy=False)
    return view


# --------------------------------------------------------------------------- edge maps

def _grow(mask: np.ndarray, steps: int = 1, diagonal: bool = False) -> np.ndarray:
    """`mask` grown by `steps` pixels to the 4 neighbours (a diamond), or the 8 with `diagonal` (a square)."""
    for _ in range(steps):
        g = mask.copy()
        g[1:] |= mask[:-1]
        g[:-1] |= mask[1:]
        g[:, 1:] |= mask[:, :-1]
        g[:, :-1] |= mask[:, 1:]
        if diagonal:
            g[1:, 1:] |= mask[:-1, :-1]
            g[1:, :-1] |= mask[:-1, 1:]
            g[:-1, 1:] |= mask[1:, :-1]
            g[:-1, :-1] |= mask[1:, 1:]
        mask = g
    return mask


RAYS = 360       # rays cast from the brush's centre, one per degree
RAY_STEP = 0.5   # pixels between the samples along a ray (below 1, so that thin diagonal edges are not stepped over)
OUTLIER_PX = 3   # a ray is an outlier if it is longer (shorter) than its neighbours on both sides by more than this
OUTLIER_FRACTION = 0.25  # ... and more than this fraction of its length


@functools.lru_cache(maxsize=64)
def _ray_offsets(length: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample offsets along RAYS rays: x and y (RAYS x samples, rounded to pixels) and the samples' distances."""
    d = np.arange(0, length + RAY_STEP, RAY_STEP)
    a = np.arange(RAYS) * (2 * np.pi / RAYS)
    return (np.rint(np.cos(a)[:, None] * d).astype(np.intp), np.rint(np.sin(a)[:, None] * d).astype(np.intp), d)


def _polar(n: int, ox: int, oy: int) -> tuple[np.ndarray, np.ndarray]:
    """For each pixel of an n x n window: its distance from (ox, oy) and its angle in rays (0..RAYS)."""
    yy, xx = np.mgrid[:n, :n]
    dx, dy = xx - ox, yy - oy
    return np.hypot(dx, dy), np.arctan2(dy, dx) % (2 * np.pi) * (RAYS / (2 * np.pi))


_polar_centred = functools.lru_cache(maxsize=64)(lambda n: _polar(n, n // 2, n // 2))


def repair_outliers(lengths: np.ndarray, neighbours: float) -> np.ndarray:
    """Ray lengths in which outlier rays are interpolated between the nearest rays that are not. A ray is too long (it
    slipped through a hole in an edge) if it is longer than the shortest ray on its left AND the shortest on its right,
    each within `neighbours` degrees: the rays on both sides of a hole stopped, so holes up to `neighbours` degrees wide
    are closed. Too short (stopped by a speck) likewise against the longest on either side. Where the rays on one side
    are as long as it, it is a genuine step (an edge ending inside the circle) and stays."""
    n = len(lengths)
    k = max(1, int(round(neighbours * n / 360)))
    windows = np.lib.stride_tricks.sliding_window_view(np.concatenate([lengths[-k:], lengths, lengths[:k]]), k)
    left, right = windows[:n], windows[k + 1:k + 1 + n]  # the k rays before and the k rays after each ray
    margin = np.maximum(OUTLIER_PX, OUTLIER_FRACTION * lengths)
    too_long = lengths > np.maximum(left.min(axis=1), right.min(axis=1)) + margin
    too_short = lengths < np.minimum(left.max(axis=1), right.max(axis=1)) - margin
    bad = too_long | too_short
    if not bad.any():
        return lengths
    if bad.all():
        return np.full_like(lengths, np.median(lengths))
    good = np.flatnonzero(~bad)
    fixed = lengths.copy()
    fixed[bad] = np.interp(np.flatnonzero(bad), good, lengths[good], period=n)
    return fixed


def fill_between_edges(edges: np.ndarray, inside: np.ndarray, close: int, neighbours: float = 10) -> np.ndarray:
    """The area around the centre of the window (bool maps of the same odd size) bounded by `edges` and `inside` (the
    brush circle), including the edge line along it. Rays are cast from the centre, one per degree; each stops at the
    first edge (edges thickened by `close`, so that gaps up to 2 * close pixels hold for a single ray). Rays whose length
    is an outlier among their neighbours (`neighbours` degrees on either side) - most often a ray through a hole in an
    edge - are interpolated between the neighbours that stopped (repair_outliers), so holes narrower than the
    neighbourhood do not let the fill out. The fill is the star-shaped area within the rays' ends; it then gets back
    what the thickening took and takes the edge line. If the centre is on an edge, the rays start from the nearest pixel
    off it."""
    n = edges.shape[0]
    c = n // 2
    walls = _grow(edges, close) if close else edges
    stop = walls | ~inside
    dist, angle = _polar_centred(n)
    ox = oy = c
    if stop[c, c]:  # the centre is on an edge (or a closed gap): start from the nearest pixel off it
        free = np.flatnonzero(~stop)
        if not free.size:
            return np.zeros_like(edges)
        oy, ox = divmod(int(free[np.argmin(dist.ravel()[free])]), n)
        dist, angle = _polar(n, ox, oy)
    xs, ys, d = _ray_offsets(n)  # long enough to cross the window from anywhere in it
    padded = np.pad(stop, n + 1, constant_values=True)  # outside the window: stop
    blocked = padded[ys + oy + n + 1, xs + ox + n + 1]
    lengths = repair_outliers(d[blocked.argmax(axis=1)] - RAY_STEP, neighbours) + close  # the last free sample, + the thickening
    i0 = np.floor(angle).astype(np.intp) % RAYS  # the pixel's length limit: between its two rays
    frac = angle - np.floor(angle)
    limit = lengths[i0] * (1 - frac) + lengths[(i0 + 1) % RAYS] * frac
    reached = (dist <= limit + 0.5) & inside & ~edges
    return reached | (_grow(reached, diagonal=True) & edges & inside)


def view_at(model: Segmenter, slide, image: str, scale: float, px: float, py: float, r: float) -> View:
    """The segmented view containing the circle of radius `r` (screen pixels) around the level-0 point (px, py).
    Raises smartbrush.Pending while it is being segmented (and starts that if needed)."""
    with _lock:
        for key, view in _views.items():
            if view.covers(model, image, scale, px, py, r):
                _views.move_to_end(key)
                return view
        for key, (view, job) in list(_jobs.items()):
            if not view.covers(model, image, scale, px, py, r):
                continue
            if not job.done():
                raise sb.Pending()
            del _jobs[key]
            if job.exception() is not None:
                raise ViewerError(f"{model.name} failed: {job.exception()}", 500)
            _views[key] = view
            while len(_views) > CACHE_SIZE:
                _views.popitem(last=False)
            return view
        half = model.view / 2 / scale
        view = View(model, image, scale, px - half, py - half)  # centred on the brush
        _jobs[view.key] = (view, _executor.submit(_segment, view, slide))
        raise sb.Pending()


class ModelStroke(sb.Stroke):
    """A smart brush stroke whose dabs follow a model: inside the brush circle, the region under the brush's centre
    (label models), or what the centre reaches without crossing an edge (edge models)."""

    def __init__(self, model: Segmenter, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model
        self.slide = None  # set by the route, like for colour strokes

    def _dab(self, read_region, pu: int, pv: int, keep_seeds=None) -> dict | None:
        s, r = self.scale, self.inner
        px, py = self.x0 + (pu + 0.5) / s, self.y0 + (pv + 0.5) / s  # the centre of grid pixel (pu, pv), level 0
        view = view_at(self.model, self.slide, self.image, s, px, py, r + 1)
        cx, cy = (int(np.floor(c)) for c in view.pixel(px, py))
        n, o = 2 * sb.OUTER + 1, sb.OUTER
        mask = np.zeros((n, n), bool)
        window = view.labels[cy - r:cy + r + 1, cx - r:cx + r + 1]  # inside the view: covers() made sure
        yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
        disk = xx * xx + yy * yy <= r * r
        if self.model.output == "edges":
            mask[o - r:o + r + 1, o - r:o + r + 1] = fill_between_edges(window, disk, self.model.close, self.model.neighbours)
        elif view.labels[cy, cx]:
            mask[o - r:o + r + 1, o - r:o + r + 1] = (window == view.labels[cy, cx]) & disk
        u0, v0 = pu - o, pv - o
        self.dabs.append((u0, v0, mask))
        return sb.dab_reply(self, u0, v0, mask)
