"""The machinery of the smart brushes (Border growth in borderbrush.py, the model brushes in modelbrush.py).

A smart brush works on the image as it is seen on screen, not on the full-resolution slide: every dab reads the region
under the brush at the current zoom (one region pixel = one screen pixel), so the brush sizes below are screen pixels
and the brush behaves the same at every zoom. A stroke collects the dabs in this screen-pixel grid (fixed at the zoom
the stroke started at) and is turned into a polygon in level-0 coordinates when it ends.

Here: the stroke (`Stroke`: dabs one inner radius apart along the pointer's path, the smart polygon's dabs along its
outline, the polygon of the result), reading the slide at the stroke's zoom (`slide_reader`, `read_view`), the blur
(PyTorch on CUDA, MPS or the CPU), the gap filling (`fill_gaps`), masks to polygons and the viewer's replies. What a dab
paints is up to the subclass (`Stroke._dab`).
"""
from __future__ import annotations

import base64
import io
import math
import os
import threading
import time
from collections import deque

import numpy as np
import shapely
import torch
import torch.nn.functional as F
from PIL import Image
from shapely.geometry.base import BaseGeometry

import timing
from errors import ViewerError
from slides import OVERVIEW_REDUCTION

# Brush settings, in screen pixels (the inner radius is half the width set in the tools).
INNER = 16        # the inner circle's radius (the default; strokes get theirs from the viewer)
MAX_INNER = 100
OUTER = 200       # a dab never paints beyond this radius (its window: 2 * OUTER + 1 square)
GAP = 40          # gaps up to this long are filled ...
ALL4 = True       # ... only when enclosed left, right, above and below
BLUR_SIGMA = 5.0  # Gaussian blur of the region before its colours are compared
SPACING = INNER   # distance between dabs along a stroke: the inner radius (at least MIN_SPACING)
MIN_SPACING = 4
MARGIN = int(math.ceil(3 * BLUR_SIGMA))  # extra pixels read around the brush, so the blur has no border effects
MAX_READ = 4096   # px per side read from a slide level for one dab; zoomed further out, the slide's overview is used


class Pending(Exception):
    """A dab cannot be made yet (a segmentation model is still working on the view there, see modelbrush.py); the
    stroke tries again later."""


def pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


DEVICE = pick_device()
_kernel = None


def usable_cpus(root: str = "/") -> int:
    """How many CPUs this process may really use: its CPU affinity (a container's cpuset), a cgroup CPU quota
    (cgroup v2 cpu.max, v1 cpu.cfs_quota_us) and JupyterHub's CPU_LIMIT, whichever is smallest. PyTorch and OpenMP count
    the machine's cores instead, which in a container limited to a few cores starts far too many threads: they are throttled
    by the quota and the blur gets much slower."""
    counts = [os.cpu_count() or 1]
    if hasattr(os, "sched_getaffinity"):
        try:
            counts.append(len(os.sched_getaffinity(0)))
        except OSError:
            pass

    def read(path):
        try:
            with open(os.path.join(root, path), encoding="utf-8") as f:
                return f.read().split()
        except OSError:
            return []
    quota = read("sys/fs/cgroup/cpu.max")
    if len(quota) == 2 and quota[0] != "max" and int(quota[1]) > 0:
        counts.append(math.ceil(int(quota[0]) / int(quota[1])))
    v1_quota, v1_period = read("sys/fs/cgroup/cpu/cpu.cfs_quota_us"), read("sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if v1_quota and v1_period and int(v1_quota[0]) > 0 and int(v1_period[0]) > 0:
        counts.append(math.ceil(int(v1_quota[0]) / int(v1_period[0])))
    try:
        counts.append(math.ceil(float(os.environ.get("CPU_LIMIT", ""))))
    except ValueError:
        pass
    return max(1, min(count for count in counts if count > 0))


if torch.get_num_threads() > usable_cpus():
    torch.set_num_threads(usable_cpus())


def _gaussian():
    global _kernel
    if _kernel is None:
        r = MARGIN
        x = torch.arange(-r, r + 1, dtype=torch.float32)
        k = torch.exp(-(x * x) / (2 * BLUR_SIGMA * BLUR_SIGMA))
        _kernel = (k / k.sum()).to(DEVICE)
    return _kernel


def _blurred(rgb: np.ndarray) -> torch.Tensor:
    """The region blurred and rounded, (3, H, W) int32 on DEVICE, without the margin."""
    x = torch.from_numpy(np.array(rgb)).to(DEVICE).permute(2, 0, 1)[:, None].float()  # (3, 1, H, W)
    k = _gaussian()
    x = F.conv2d(x, k.view(1, 1, 1, -1))  # valid convolutions: the margin is used up by the blur
    x = F.conv2d(x, k.view(1, 1, -1, 1))[:, 0]
    return x.round_().clamp_(0, 255).to(torch.int32)


_fine_kernels: dict[float, torch.Tensor] = {}


def blur_exact_and_fine(rgb: np.ndarray, fine_sigma: float) -> tuple[np.ndarray, np.ndarray]:
    """`blur_exact` of the region and the same region blurred only by `fine_sigma` (packed the same way, without the
    margin), in one pass on DEVICE. The coarse colours are exactly those of `blur_exact`."""
    with timing.step("blur"):
        return _blur_exact_and_fine(rgb, fine_sigma)


def _blur_exact_and_fine(rgb: np.ndarray, fine_sigma: float) -> tuple[np.ndarray, np.ndarray]:
    x = torch.from_numpy(np.array(rgb)).to(DEVICE).permute(2, 0, 1)[:, None].float()  # (3, 1, H, W)
    k = _gaussian()
    coarse = F.conv2d(F.conv2d(x, k.view(1, 1, 1, -1)), k.view(1, 1, -1, 1))[:, 0].round_().clamp_(0, 255).to(torch.int32)
    kf = _fine_kernels.get(fine_sigma)
    if kf is None:
        r = int(math.ceil(4 * fine_sigma))
        t = torch.arange(-r, r + 1, dtype=torch.float32)
        kf = torch.exp(-(t * t) / (2 * fine_sigma * fine_sigma))
        kf = _fine_kernels[fine_sigma] = (kf / kf.sum()).to(DEVICE)
    r = (kf.numel() - 1) // 2
    cut = MARGIN - r  # the fine kernel is narrower: it needs less of the margin
    xf = x[:, :, cut:x.shape[2] - cut, cut:x.shape[3] - cut]
    fine = F.conv2d(F.conv2d(xf, kf.view(1, 1, 1, -1)), kf.view(1, 1, -1, 1))[:, 0].round_().clamp_(0, 255).to(torch.int32)
    both = torch.stack([(q[0] << 16) | (q[1] << 8) | q[2] for q in (coarse, fine)]).cpu().numpy()
    return both[0], both[1]


def blur_exact(rgb: np.ndarray) -> np.ndarray:
    """Blurs an HxWx3 uint8 region and returns its packed exact colours (0xRRGGBB, int32), without the margin."""
    with timing.step("blur"):
        q = _blurred(rgb)
        return ((q[0] << 16) | (q[1] << 8) | q[2]).cpu().numpy()


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
    with timing.step("fill gaps"):
        m = torch.from_numpy(mask).to(DEVICE)
        h = _gaps_along_rows(m, gap)
        v = _gaps_along_rows(m.T.contiguous(), gap).T
        return (m | ((h & v) if all4 else (h | v))).cpu().numpy()


# --------------------------------------------------------------------------- masks to polygons

def _chaikin(ring: np.ndarray, iterations: int) -> np.ndarray:
    """Corner cutting of a closed ring (first point = last): every corner becomes two points a quarter of the way along its
    edges. A few rounds turn a polyline into a smooth curve close to it."""
    points = ring[:-1]
    for _ in range(iterations):
        following = np.roll(points, -1, axis=0)
        points = np.stack([0.75 * points + 0.25 * following, 0.25 * points + 0.75 * following], axis=1).reshape(-1, 2)
    return np.vstack([points, points[:1]])


def smooth_mask_outline(mask: np.ndarray, sigma: float = 1.0, straighten: float = 0.75, rounds: int = 3) -> BaseGeometry | None:
    """The set pixels as a smooth (Multi)Polygon: a majority vote of the neighbours (Gaussian of `sigma`, at least half set)
    takes away lone pixels, spurs and notches; the outline's pixel staircase is straightened (`straighten` pixels) and
    its corners are cut (`rounds` of Chaikin), holes as well. In pixel units like mask_to_geometry."""
    from scipy import ndimage
    if sigma > 0:
        mask = ndimage.gaussian_filter(mask.astype(np.float32), sigma, mode="constant") >= 0.5
    geometry = mask_to_geometry(mask)
    if geometry is None:
        return None
    geometry = geometry.simplify(straighten, preserve_topology=True)
    polygons = []
    for polygon in shapely.get_parts(geometry):
        if polygon.geom_type != "Polygon" or polygon.is_empty:
            continue
        exterior = _chaikin(np.asarray(polygon.exterior.coords), rounds)
        holes = [_chaikin(np.asarray(hole.coords), rounds) for hole in polygon.interiors if len(hole.coords) >= 4]
        smooth = shapely.make_valid(shapely.Polygon(exterior, holes))
        polygons.extend(p for p in shapely.get_parts(smooth) if p.geom_type == "Polygon" and p.area > 0)
    return shapely.union_all(polygons) if polygons else None


def mask_to_geometry(mask: np.ndarray) -> BaseGeometry | None:
    """The set pixels as (Multi)Polygon in pixel units, pixel (col, row) covering [col, col+1] x [row, row+1]."""
    if not mask.any():
        return None
    d = np.diff(np.pad(mask, ((0, 0), (1, 1))).astype(np.int8), axis=1)
    starts_r, starts_c = np.nonzero(d == 1)
    _, ends_c = np.nonzero(d == -1)  # same row order as the starts: runs alternate start, end
    boxes = shapely.box(starts_c, starts_r, ends_c, starts_r + 1)
    return shapely.union_all(boxes)


def dab_reply(stroke, u0: int, v0: int, mask: np.ndarray) -> dict | None:
    """What the viewer gets for a dab: the mask cut to the pixels it sets (a dab adds a band, not its whole window, so
    there is far less to encode and send), placed in level-0 coordinates. None if it sets nothing."""
    rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if rows.size == 0:
        return None
    r0, r1, c0, c1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    s = stroke.scale
    return {"x": stroke.x0 + (u0 + c0) / s, "y": stroke.y0 + (v0 + r0) / s, "w": (c1 - c0) / s, "h": (r1 - r0) / s,
            "png": png_of(mask[r0:r1, c0:c1])}


def png_of(mask: np.ndarray) -> str:
    """The mask as a white PNG with the mask as alpha, base64 (the viewer tints it)."""
    with timing.step("png"):
        rgba = np.zeros(mask.shape + (4,), np.uint8)
        rgba[mask] = 255
        out = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(out, "PNG", compress_level=1)
        return base64.b64encode(out.getvalue()).decode()


# --------------------------------------------------------------------------- strokes

class Stroke:
    """The dabs of one stroke in a grid of screen pixels: grid pixel (u, v) covers level-0
    [x0 + u/scale, x0 + (u+1)/scale) x [y0 + v/scale, ...), with (x0, y0) the stroke's first point."""

    def __init__(self, image: str, mode: str, scale: float, x: float, y: float, inner: int = INNER):
        self.image, self.mode, self.scale = image, mode, scale
        self.inner = inner
        self.outer = OUTER  # the dab window's radius (border growth can make it smaller, see borderbrush)
        self.spacing = max(MIN_SPACING, inner)
        self.x0, self.y0 = x, y
        self.last: tuple[int, int] | None = None
        self.dabs: list[tuple[int, int, np.ndarray]] = []  # (u, v) of the window's top-left, mask
        self.polygon: BaseGeometry | None = None  # smart polygon: the drawn polygon, in grid coordinates
        self.todo: deque[tuple[int, int]] = deque()  # smart polygon: dab centres not done yet
        self.waiting = False  # the last call stopped at a dab that is not ready yet (see Pending)
        self.lock = threading.Lock()

    def grid(self, x: float, y: float) -> tuple[int, int]:
        return int(math.floor((x - self.x0) * self.scale)), int(math.floor((y - self.y0) * self.scale))

    def _dab(self, read_region, pu: int, pv: int, keep_seeds=None) -> dict | None:
        """One dab centred on grid pixel (pu, pv): appends (u0, v0, mask) to `dabs` and returns the viewer's reply
        (`dab_reply`). `keep_seeds(rows, cols)` (window coordinates) says which pixels may seed it (a smart polygon: its
        inside). Raises Pending if the dab cannot be made yet."""
        raise NotImplementedError

    def outline(self, points: list[tuple[float, float]]) -> None:
        """Makes this stroke a smart polygon: the polygon (level-0 points) is filled, and dabs placed `spacing` apart
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
            for d in np.arange(0, line.length, self.spacing):
                p = line.interpolate(d)
                self.todo.append((int(math.floor(p.x)), int(math.floor(p.y))))

    def outline_dabs(self, read_region, budget: float = 1.0) -> list[dict]:
        """Works through the smart polygon's dab centres for about `budget` seconds. Returns the new dabs."""
        out, end = [], time.monotonic() + budget
        self.waiting = False
        while self.todo and time.monotonic() < end:
            pu, pv = self.todo.popleft()
            # grid pixel (u, v) has its centre at (u + 0.5, v + 0.5)
            o = self.outer
            keep = lambda r, c, pu=pu, pv=pv, o=o: shapely.contains_xy(self.polygon, pu - o + c + 0.5, pv - o + r + 0.5)  # noqa: E731
            try:
                dab = self._dab(read_region, pu, pv, keep)
            except Pending:
                self.todo.appendleft((pu, pv))
                self.waiting = True
                break
            if dab:
                out.append(dab)
        return out

    def to_dab(self, read_region, x: float, y: float) -> list[dict]:
        """Adds dabs from the last point to (x, y) (level 0), `spacing` screen pixels apart. Returns them for the viewer."""
        u, v = self.grid(x, y)
        if self.last is None:
            points = [(u, v)]
        else:
            lu, lv = self.last
            steps = max(abs(u - lu), abs(v - lv)) // self.spacing
            if steps == 0:
                return []
            points = [(lu + (u - lu) * i // steps, lv + (v - lv) * i // steps) for i in range(1, steps + 1)]
        out = []
        self.waiting = False
        for pu, pv in points:
            try:
                dab = self._dab(read_region, pu, pv)
            except Pending:  # the rest of the way is done on a later call, from the last dab made
                self.waiting = True
                break
            self.last = (pu, pv)
            if dab:
                out.append(dab)
        return out

    def painted_canvas(self) -> tuple[np.ndarray, int, int] | None:
        """All dabs' masks in one array, and the grid pixel of its top-left (None without dabs)."""
        if not self.dabs:
            return None
        n = self.dabs[0][2].shape[0]
        u_min = min(u for u, _, _ in self.dabs)
        v_min = min(v for _, v, _ in self.dabs)
        width = max(u for u, _, _ in self.dabs) - u_min + n
        height = max(v for _, v, _ in self.dabs) - v_min + n
        canvas = np.zeros((height, width), bool)
        for u, v, mask in self.dabs:
            canvas[v - v_min:v - v_min + n, u - u_min:u - u_min + n] |= mask
        return canvas, u_min, v_min

    def painted_outline(self, canvas: np.ndarray) -> BaseGeometry | None:
        """The painted pixels as a polygon (grid pixels, the canvas' top-left at 0, 0). Border growth smooths it."""
        return mask_to_geometry(canvas)

    simplify_tolerance = 0.5  # grid pixels

    def geometry(self) -> BaseGeometry | None:
        """All dabs (and the smart polygon's polygon) as one polygon in level-0 coordinates, simplified by half a
        screen pixel."""
        parts = [self.polygon] if self.polygon is not None else []
        painted_canvas = self.painted_canvas()
        if painted_canvas is not None:
            canvas, u_min, v_min = painted_canvas
            painted = self.painted_outline(canvas)
            if painted is not None and not painted.is_empty:
                parts.append(shapely.transform(painted, lambda c: c + (u_min, v_min)))
        if not parts:
            return None
        geometry = shapely.union_all(parts).simplify(self.simplify_tolerance, preserve_topology=True)
        s = self.scale
        return shapely.transform(geometry, lambda c: c / s + (self.x0, self.y0))


def _from_overview(overview, x: float, y: float, span: float, size: int, span_y: float | None = None,
                   height: int | None = None) -> np.ndarray:
    """The level-0 rectangle (x, y, span, span_y) cut from the overview and scaled to `size` x `height` (a square by
    default); what lies outside the image is white, like the viewer shows it (Pillow cannot cut outside an image)."""
    span_y, height = (span if span_y is None else span_y), (size if height is None else height)
    r = OVERVIEW_REDUCTION
    x0, y0, x1, y1 = x / r, y / r, (x + span) / r, (y + span_y) / r
    k = size / (x1 - x0)  # output pixels per overview pixel
    out = Image.new("RGB", (size, height), (255, 255, 255))
    cx0, cy0, cx1, cy1 = max(x0, 0), max(y0, 0), min(x1, overview.width), min(y1, overview.height)
    ox0, oy0 = round((cx0 - x0) * k), round((cy0 - y0) * k)
    ox1, oy1 = round((cx1 - x0) * k), round((cy1 - y0) * k)
    if ox1 > ox0 and oy1 > oy0:
        part = overview.resize((ox1 - ox0, oy1 - oy0), Image.BILINEAR, box=(cx0, cy0, cx1, cy1))
        out.paste(part.convert("RGB"), (ox0, oy0))
    return np.asarray(out)


def read_view(slide, x: float, y: float, scale: float, size: int, height: int | None = None) -> np.ndarray:
    """The slide as seen at `scale` (screen pixels per level-0 pixel): `size` x `height` RGB pixels (a square by default)
    whose top-left is the level-0 point (x, y)."""
    with timing.step("read slide"):
        return _read_view(slide, x, y, scale, size, height)


def _read_view(slide, x: float, y: float, scale: float, size: int, height: int | None = None) -> np.ndarray:
    height = size if height is None else height
    os_slide = slide.slide
    down = 1 / scale  # level-0 pixels per screen pixel
    span, span_y = size * down, height * down
    level = os_slide.get_best_level_for_downsample(max(down, 1.0))
    ds = os_slide.level_downsamples[level]
    if max(span, span_y) / ds > MAX_READ:  # no pyramid level small enough: use the overview
        return _from_overview(slide.overview(), x, y, span, size, span_y, height)
    ix, iy = int(math.floor(x)), int(math.floor(y))
    read_w = int(math.ceil((x - ix + span) / ds)) + 1
    read_h = int(math.ceil((y - iy + span_y) / ds)) + 1
    block = slide.read_region((ix, iy), level, (read_w, read_h))
    flat = Image.new("RGB", block.size, (255, 255, 255))  # outside the scanned area: white, like the viewer
    flat.paste(block, mask=block.getchannel("A"))
    box = ((x - ix) / ds, (y - iy) / ds, (x - ix + span) / ds, (y - iy + span_y) / ds)
    return np.asarray(flat.resize((size, height), Image.BILINEAR, box=box))


def slide_reader(slide):
    """read_region(stroke, u, v) for a Slide: the RGB region around grid pixel (u, v) at the stroke's scale.
    `read_region.box(stroke, u0, v0, width, height)` reads any rectangle of grid pixels (top-left u0, v0), e.g. tiles."""
    size = 2 * (OUTER + MARGIN) + 1

    def box(stroke: Stroke, u0: int, v0: int, width: int, height: int | None = None) -> np.ndarray:
        s = stroke.scale
        return read_view(slide, stroke.x0 + u0 / s, stroke.y0 + v0 / s, s, width, height)

    def read(stroke: Stroke, u: int, v: int) -> np.ndarray:
        # level-0 rectangle covered by the grid pixels u - OUTER - MARGIN ... + size
        return box(stroke, u - OUTER - MARGIN, v - OUTER - MARGIN, size)

    read.box = box
    return read


def check_inner(inner) -> int:
    """The inner radius a viewer asks for (missing: the default)."""
    if inner is None:
        return INNER
    try:
        return max(1, min(MAX_INNER, int(round(float(inner)))))
    except (TypeError, ValueError):
        raise ViewerError("Invalid brush width.") from None


def check_scale(scale) -> float:
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        raise ViewerError("Invalid zoom.") from None
    if not (1e-4 < scale < 1e3):
        raise ViewerError("Invalid zoom.")
    return scale
