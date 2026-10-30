"""Smart brush: an edge-aware brush that fills the colour region under the pointer.

The brush works on the image as it is seen on screen, not on the full-resolution slide: every dab reads the region
under the brush at the current zoom (one region pixel = one screen pixel), so the brush sizes below are screen pixels
and the brush behaves the same at every zoom. A stroke collects the dabs in this screen-pixel grid (fixed at the zoom
the stroke started at) and is turned into a polygon in level-0 coordinates when it ends.

One dab:
  1. read the region around the pointer, blur it, quantize it to colour buckets (torch: CUDA, MPS or CPU);
  2. from every seed (lattice points inside the inner circle) flood the region in steps of STRIDE pixels, never
     leaving the outer circle: a pixel joins if its bucket is within TOL steps of the seed's bucket (hash set) and
     its exact blurred colour is within THRESHOLD of the seed's colour; each step checks every pixel it crosses,
     so the flood cannot jump over an edge (CPU, it is a queue);
  3. fill the gaps of up to GAP pixels that are enclosed on all four sides (torch), which also closes the lattice.

The smart polygon uses the same dabs: the drawn polygon is filled, and dabs along its boundary, seeded only inside the
polygon, extend it outwards to the edges around it.
"""
from __future__ import annotations

import base64
import io
import math
import threading
import time
from collections import deque

import numpy as np
import shapely
import torch
import torch.nn.functional as F
from PIL import Image
from shapely.geometry.base import BaseGeometry

from errors import ViewerError
from slides import OVERVIEW_REDUCTION

# Brush settings, in screen pixels (not exposed for now).
INNER = 16        # seeds: every lattice point inside this radius
OUTER = 200       # the flood never leaves this radius
BITS = 3          # bits per colour channel of the buckets
TOL = 1           # buckets within this many steps of the seed's bucket pass the first check
THRESHOLD = 15    # exact check: Euclidean RGB distance to the seed's colour
GAP = 40          # gaps up to this long are filled ...
ALL4 = True       # ... only when enclosed left, right, above and below
STRIDE = 4        # the flood walks every STRIDE-th row and column
BLUR_SIGMA = 5.0  # Gaussian blur of the region before bucketing
SPACING = INNER   # distance between dabs along a stroke
MARGIN = int(math.ceil(3 * BLUR_SIGMA))  # extra pixels read around the brush, so the blur has no border effects
MAX_READ = 4096   # px per side read from a slide level for one dab; zoomed further out, the slide's overview is used


def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


DEVICE = pick_device()
_kernel = None


def _gaussian():
    global _kernel
    if _kernel is None:
        r = MARGIN
        x = torch.arange(-r, r + 1, dtype=torch.float32)
        k = torch.exp(-(x * x) / (2 * BLUR_SIGMA * BLUR_SIGMA))
        _kernel = (k / k.sum()).to(DEVICE)
    return _kernel


def prepare(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Blurs an HxWx3 uint8 region and returns (bucket ids, packed exact colours), both HxW int32, without the margin."""
    x = torch.from_numpy(np.array(rgb)).to(DEVICE).permute(2, 0, 1)[:, None].float()  # (3, 1, H, W)
    k = _gaussian()
    x = F.conv2d(x, k.view(1, 1, 1, -1))  # valid convolutions: the margin is used up by the blur
    x = F.conv2d(x, k.view(1, 1, -1, 1))[:, 0]
    q = x.round_().clamp_(0, 255).to(torch.int32)
    exact = (q[0] << 16) | (q[1] << 8) | q[2]
    b = q >> (8 - BITS)
    ids = (b[0] << 16) | (b[1] << 8) | b[2]
    return ids.cpu().numpy(), exact.cpu().numpy()


def _accept_set(bucket: int) -> frozenset:
    r, g, b = bucket >> 16, (bucket >> 8) & 255, bucket & 255
    span = range(-TOL, TOL + 1)
    return frozenset(((r + dr) << 16) | ((g + dg) << 8) | (b + db)
                     for dr in span for dg in span for db in span
                     if r + dr >= 0 and g + dg >= 0 and 0 <= b + db <= 255 and g + dg <= 255)


def flood(ids: np.ndarray, exact: np.ndarray, keep_seeds=None) -> np.ndarray:
    """The dab without gap filling: a bool mask of the (2*OUTER+1)^2 window centred on the brush.
    `keep_seeds(rows, cols)` (window coordinates) can restrict the seeds, e.g. to the inside of a polygon."""
    n = 2 * OUTER + 1
    c = OUTER
    yy, xx = np.mgrid[0:n, 0:n]
    d2 = (xx - c) ** 2 + (yy - c) ** 2
    pw = n + 2
    # One-pixel border, and everything outside the outer circle, is -1: it never matches a bucket.
    pad = np.full((n + 2, pw), -1, np.int32)
    pad[1:-1, 1:-1] = np.where(d2 <= OUTER * OUTER, ids, -1)
    colors = pad.ravel().tolist()
    epad = np.zeros((n + 2, pw), np.int32)
    epad[1:-1, 1:-1] = exact
    rgb = epad.ravel().tolist()

    seeds = (d2 <= INNER * INNER) & ((yy - c) % STRIDE == 0) & ((xx - c) % STRIDE == 0)
    rc = np.argwhere(seeds)
    if keep_seeds is not None:
        rc = rc[keep_seeds(rc[:, 0], rc[:, 1])]
    seed_pos = [(r + 1) * pw + col + 1 for r, col in rc.tolist()]

    thr2 = THRESHOLD * THRESHOLD
    dirs = (1, -1, pw, -pw)
    visited = bytearray(len(colors))
    node_seen = bytearray(len(colors))
    accept_cache: dict[int, frozenset] = {}
    for start in seed_pos:
        if visited[start]:
            continue
        bucket = colors[start]
        accept = accept_cache.get(bucket)
        if accept is None:
            accept = accept_cache[bucket] = _accept_set(bucket)
        s = rgb[start]
        sr, sg, sb = s >> 16, (s >> 8) & 255, s & 255
        visited[start] = node_seen[start] = 1
        queue = deque([start])
        while queue:
            p = queue.popleft()
            for d in dirs:
                q = p
                for _ in range(STRIDE):
                    q += d
                    if colors[q] not in accept:  # stops at the -1 border, so q never leaves the array
                        break
                    v = rgb[q]
                    dr, dg, db = (v >> 16) - sr, ((v >> 8) & 255) - sg, (v & 255) - sb
                    if dr * dr + dg * dg + db * db > thr2:
                        break
                    visited[q] = 1
                else:
                    if not node_seen[q]:
                        node_seen[q] = 1
                        queue.append(q)
    return np.frombuffer(visited, dtype=np.uint8).reshape(n + 2, pw)[1:-1, 1:-1].astype(bool)


def _gaps_along_rows(m: torch.Tensor, gap: int) -> torch.Tensor:
    """Unset pixels whose nearest set pixels to the left and right are at most `gap` - 1 pixels apart."""
    n = m.shape[1]
    idx = torch.arange(n, device=m.device).expand_as(m)
    none = torch.full_like(idx, -n - 1)
    left = torch.where(m, idx, none).cummax(1).values  # nearest set column at or left of x
    right = n - 1 - torch.where(m.flip(1), idx, none).cummax(1).values.flip(1)  # ... at or right of x
    return ~m & (left >= 0) & (right < n) & (right - left - 1 <= gap)


def fill_gaps(mask: np.ndarray, gap: int = GAP, all4: bool = ALL4) -> np.ndarray:
    """Fills runs of up to `gap` unset pixels between set pixels, horizontally and vertically (both, with all4).

    The same result as the line-filter formulation (a line kernel of k ones on each side of the centre, clamped,
    paired as L_k * R_(gap+1-k)), computed with a cumulative max, so the cost does not grow with the gap size.
    """
    if gap <= 0 or not mask.any():
        return mask
    m = torch.from_numpy(mask).to(DEVICE)
    h = _gaps_along_rows(m, gap)
    v = _gaps_along_rows(m.T.contiguous(), gap).T
    return (m | ((h & v) if all4 else (h | v))).cpu().numpy()


def dab_mask(region: np.ndarray, keep_seeds=None) -> np.ndarray:
    """A region of (2*(OUTER+MARGIN)+1)^2 RGB pixels centred on the brush -> the dab's mask, (2*OUTER+1)^2."""
    ids, exact = prepare(region)
    return fill_gaps(flood(ids, exact, keep_seeds))


# --------------------------------------------------------------------------- masks to polygons

def mask_to_geometry(mask: np.ndarray) -> BaseGeometry | None:
    """The set pixels as (Multi)Polygon in pixel units, pixel (col, row) covering [col, col+1] x [row, row+1]."""
    if not mask.any():
        return None
    d = np.diff(np.pad(mask, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    starts_r, starts_c = np.nonzero(d == 1)
    _, ends_c = np.nonzero(d == -1)  # same row order as the starts: runs alternate start, end
    boxes = shapely.box(starts_c, starts_r, ends_c, starts_r + 1)
    return shapely.union_all(boxes)


def png_of(mask: np.ndarray) -> str:
    """The mask as a white PNG with the mask as alpha, base64 (the viewer tints it)."""
    rgba = np.zeros(mask.shape + (4,), np.uint8)
    rgba[mask] = 255
    out = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(out, "PNG", compress_level=1)
    return base64.b64encode(out.getvalue()).decode()


# --------------------------------------------------------------------------- strokes

class Stroke:
    """The dabs of one stroke in a grid of screen pixels: grid pixel (u, v) covers level-0
    [x0 + u/scale, x0 + (u+1)/scale) x [y0 + v/scale, ...), with (x0, y0) the stroke's first point."""

    def __init__(self, image: str, mode: str, scale: float, x: float, y: float):
        self.image, self.mode, self.scale = image, mode, scale
        self.x0, self.y0 = x, y
        self.last: tuple[int, int] | None = None
        self.dabs: list[tuple[int, int, np.ndarray]] = []  # (u, v) of the window's top-left, mask
        self.polygon: BaseGeometry | None = None  # smart polygon: the drawn polygon, in grid coordinates
        self.todo: deque[tuple[int, int]] = deque()  # smart polygon: dab centres not done yet
        self.lock = threading.Lock()

    def grid(self, x: float, y: float) -> tuple[int, int]:
        return int(math.floor((x - self.x0) * self.scale)), int(math.floor((y - self.y0) * self.scale))

    def _dab(self, read_region, pu: int, pv: int, keep_seeds=None) -> dict | None:
        mask = dab_mask(read_region(self, pu, pv), keep_seeds)
        u0, v0 = pu - OUTER, pv - OUTER
        self.dabs.append((u0, v0, mask))
        if not mask.any():
            return None
        n = mask.shape[0]
        return {"x": self.x0 + u0 / self.scale, "y": self.y0 + v0 / self.scale,
                "w": n / self.scale, "h": n / self.scale, "png": png_of(mask)}

    def outline(self, points: list[tuple[float, float]]) -> None:
        """Makes this stroke a smart polygon: the polygon (level-0 points) is filled, and dabs placed SPACING apart
        along its boundary, seeded only inside it, grow it out to the edges around it."""
        s = self.scale
        ring = [((x - self.x0) * s, (y - self.y0) * s) for x, y in points]
        parts = [p for p in shapely.get_parts(shapely.make_valid(shapely.Polygon(ring)))
                 if p.geom_type in ("Polygon", "MultiPolygon") and p.area > 0]
        if not parts:
            raise ViewerError("The polygon has no area.")
        self.polygon = shapely.union_all(parts)
        shapely.prepare(self.polygon)
        for line in shapely.get_parts(self.polygon.boundary):
            for d in np.arange(0, line.length, SPACING):
                p = line.interpolate(d)
                self.todo.append((int(math.floor(p.x)), int(math.floor(p.y))))

    def outline_dabs(self, read_region, budget: float = 1.0) -> list[dict]:
        """Works through the smart polygon's dab centres for about `budget` seconds. Returns the new dabs."""
        out, end = [], time.monotonic() + budget
        while self.todo and time.monotonic() < end:
            pu, pv = self.todo.popleft()
            # grid pixel (u, v) has its centre at (u + 0.5, v + 0.5)
            keep = lambda r, c, pu=pu, pv=pv: shapely.contains_xy(self.polygon, pu - OUTER + c + 0.5, pv - OUTER + r + 0.5)  # noqa: E731
            dab = self._dab(read_region, pu, pv, keep)
            if dab:
                out.append(dab)
        return out

    def to_dab(self, read_region, x: float, y: float) -> list[dict]:
        """Adds dabs from the last point to (x, y) (level 0), SPACING screen pixels apart. Returns them for the viewer."""
        u, v = self.grid(x, y)
        if self.last is None:
            points = [(u, v)]
        else:
            lu, lv = self.last
            steps = max(abs(u - lu), abs(v - lv)) // SPACING
            if steps == 0:
                return []
            points = [(lu + (u - lu) * i // steps, lv + (v - lv) * i // steps) for i in range(1, steps + 1)]
        out = []
        for pu, pv in points:
            dab = self._dab(read_region, pu, pv)
            self.last = (pu, pv)
            if dab:
                out.append(dab)
        return out

    def geometry(self) -> BaseGeometry | None:
        """All dabs (and the smart polygon's polygon) as one polygon in level-0 coordinates, simplified by half a
        screen pixel."""
        parts = [self.polygon] if self.polygon is not None else []
        if self.dabs:
            n = self.dabs[0][2].shape[0]
            u_min = min(u for u, _, _ in self.dabs)
            v_min = min(v for _, v, _ in self.dabs)
            width = max(u for u, _, _ in self.dabs) - u_min + n
            height = max(v for _, v, _ in self.dabs) - v_min + n
            canvas = np.zeros((height, width), bool)
            for u, v, mask in self.dabs:
                canvas[v - v_min:v - v_min + n, u - u_min:u - u_min + n] |= mask
            painted = mask_to_geometry(canvas)
            if painted is not None:
                parts.append(shapely.transform(painted, lambda c: c + (u_min, v_min)))
        if not parts:
            return None
        geometry = shapely.union_all(parts).simplify(0.5, preserve_topology=True)
        s = self.scale
        return shapely.transform(geometry, lambda c: c / s + (self.x0, self.y0))


def slide_reader(slide):
    """read_region(stroke, u, v) for a Slide: the RGB region around grid pixel (u, v) at the stroke's scale."""
    os_slide = slide.slide
    size = 2 * (OUTER + MARGIN) + 1

    def read(stroke: Stroke, u: int, v: int) -> np.ndarray:
        s = stroke.scale
        down = 1 / s  # level-0 pixels per screen pixel
        # level-0 rectangle covered by the grid pixels u - OUTER - MARGIN ... + size
        x = stroke.x0 + (u - OUTER - MARGIN) / s
        y = stroke.y0 + (v - OUTER - MARGIN) / s
        span = size * down
        level = os_slide.get_best_level_for_downsample(max(down, 1.0))
        ds = os_slide.level_downsamples[level]
        if span / ds > MAX_READ:  # no pyramid level small enough: use the overview
            overview = slide.overview()
            r = OVERVIEW_REDUCTION
            region = overview.resize((size, size), Image.BILINEAR, box=(x / r, y / r, (x + span) / r, (y + span) / r))
            return np.asarray(region.convert("RGB"))
        ix, iy = int(math.floor(x)), int(math.floor(y))
        read_w = int(math.ceil((x - ix + span) / ds)) + 1
        block = slide.read_region((ix, iy), level, (read_w, read_w))
        flat = Image.new("RGB", block.size, (255, 255, 255))  # outside the scanned area: white, like the viewer
        flat.paste(block, mask=block.getchannel("A"))
        box = ((x - ix) / ds, (y - iy) / ds, (x - ix + span) / ds, (y - iy + span) / ds)
        return np.asarray(flat.resize((size, size), Image.BILINEAR, box=box))

    return read


def check_scale(scale) -> float:
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        raise ViewerError("Invalid zoom.") from None
    if not (1e-4 < scale < 1e3):
        raise ViewerError("Invalid zoom.")
    return scale
