"""Border growth: the smart brush that grows the stroke from its own border, through the colours of its inner circle.

A dab has an inner and an outer circle (on the stroke machinery of smartbrush.py), and the inner circle defines the
allowed colours: a pixel may join if its exact (blurred) colour is close to the colour of some pixel in the inner
circle. In addition, the growth compares with the neighbour: a pixel is only entered from a 4-neighbour whose colour
differs from its own by at most a step limit. Inside a region the colour changes little from pixel to pixel (after the
blur), across an edge it changes more, so the growth does not chain through a series of allowed colours across an edge.

Both limits depend on the distance from the inner circle (`profile`): lenient (THRESHOLD_NEAR, STEP_NEAR) inside it and
up to NEAR_MARGIN beyond, then falling linearly over FALLOFF pixels to strict (THRESHOLD_FAR, STEP_FAR) for the rest of
the outer circle. So the brush is generous where it is held and strict further away, and the user steers the growth
by where the brush goes and by its width. Inside the inner circle the step limit is loosened further, from STEP_CENTRE
at the centre down to STEP_NEAR at the inner circle's rim (`step_limit`): otherwise a click on a fine structure or an
edge, where the colour changes by more than STEP_NEAR from one pixel to the next, could not leave its centre pixel.

The growth starts from what the stroke has painted so far (not from seeds in the inner circle, at every dab anew): the dab adds everything inside
the outer circle that is connected, through allowed pixels, to the painted area there. With nothing painted in the outer
circle yet (the first dab), the brush's centre pixel is the start. So the area spreads out from its border as the
brush moves, and an allowed colour that is not connected to the area is never reached.

The growth is per pixel (no stride), 4-connected: a flood from the border of the painted area (`_flood`, compiled with
numba). It visits only the pixels that join and their rejected neighbours; the painted area itself is never walked
through, and each test is made only where the flood gets to: the neighbour step for the links it tries, the allowed-colour
test (the nearest colour of the inner circle, looked up in a grid of the RGB cube) once per pixel it reaches. So a dab costs
about what it adds, not the size of its window. Then gaps up to GAP pixels enclosed on all four sides are filled
(sb.fill_gaps).

`grow_labelled` is the same rule done as one connected-component labelling (scipy) of the whole window: on a grid of
twice the resolution, in which the links between neighbouring pixels are cells of their own, with a KD-tree for the
allowed colours. It gives the same pixels (the tests compare the two); it is used where numba is not installed.

Speed. Consecutive dabs (`inner` apart) overlap by most of their window, so the stroke keeps the blurred image in tiles
of BLUR_TILE grid pixels (`BorderStroke._exact`): each part of the slide is read and blurred once per stroke, not for every
dab that covers it. The limits by distance (`window_maps`) are computed once per brush size. The window's radius is
the stroke's `outer`: sb.OUTER by default, smaller with the plugin's `border_reach` setting (a dab's
cost grows with its square: half the radius is about a quarter of the work, but a dab reaches less far).
"""
from __future__ import annotations

import functools
import math
from collections import OrderedDict
from typing import NamedTuple

import numpy as np
import shapely
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.spatial import cKDTree

import smartbrush as sb
import timing

try:
    import numba
except ImportError:  # the labelling below does the same, more slowly
    numba = None

# Limits by distance from the inner circle (see `profile`): NEAR inside it and up to NEAR_MARGIN pixels beyond (at least
# half the inner radius), then falling linearly over FALLOFF pixels to FAR.
NEAR_MARGIN = 8
FALLOFF = 60
# Allowed colours: Euclidean RGB distance to the nearest colour of the inner circle.
THRESHOLD_NEAR = 18
THRESHOLD_FAR = 5
# Neighbour comparison: largest colour difference between neighbouring pixels (blurred colours, as above). They are
# whole numbers, so steps come in sqrt(1), sqrt(2), sqrt(3), 2, sqrt(5), sqrt(6), ...: 2 lets through one unit in up to
# three channels or two in one; 3 also two units in two channels.
STEP_NEAR = 3.0
STEP_FAR = 2.0
STEP_CENTRE = 10.0  # ... and at the brush's centre, falling to STEP_NEAR at the inner circle's rim
# Strict colours (the brush panel's switch, for less leaking): STRICT_NEAR inside the inner circle, then falling as a
# Gaussian of the distance beyond its rim, with STRICT_SIGMA of the inner radius (at least MIN_STRICT_SIGMA pixels) as
# its width, to STRICT_FAR (`strict_profile`). The neighbour steps are the same as in the normal mode.
STRICT_NEAR = 14.0
STRICT_FAR = 2.0
STRICT_SIGMA = 1 / 3
MIN_STRICT_SIGMA = 5.0
SENSITIVITIES = ("normal", "strict")
# Snapping to edges (`snap_to_edges`): the coarse blur (sb.BLUR_SIGMA) turns an edge into a ramp, and the growth stops on
# the way into it, a few pixels short of the edge. Up to SNAP pixels beyond what a dab painted, a pixel joins if its colour
# in a lightly blurred copy (SNAP_SIGMA) is nearer to the painted side's than to the other side's (the mean colours of
# rings SNAP_RING wide on both sides, nearby), where the two sides differ by at least SNAP_CONTRAST.
SNAP = int(round(2 * sb.BLUR_SIGMA))
SNAP_SIGMA = 1.0
SNAP_RING = 6
SNAP_CONTRAST = 12.0
# ... and only if its colour is also close to the painted side's itself: within SNAP_TOLERANCE right at the grown border,
# falling as a Gaussian of the distance (SNAP_TOLERANCE_SIGMA) beyond it (`snap_tolerance`). Without it, any pixel nearer
# to the painted side than to the other one joined, which in tissue lets the border creep into neighbouring structures.
SNAP_TOLERANCE = 10.0
SNAP_TOLERANCE_SIGMA = 6.0
# The outline of a finished stroke (BorderStroke.outline): a majority vote of the neighbours (Gaussian of SMOOTH_SIGMA grid
# pixels), the pixel staircase straightened (SMOOTH_STRAIGHTEN pixels) and corners cut in SMOOTH_ROUNDS rounds (Chaikin).
SMOOTH_SIGMA = 1.0
SMOOTH_STRAIGHTEN = 0.75
SMOOTH_ROUNDS = 3
SNAP_BLOCK = 4  # the side means are taken on a grid of blocks this large (they change slowly: 16 x less to sum)
TILE = 256  # the stroke's painted area is kept in tiles of this many grid pixels per side
FOUR = ndimage.generate_binary_structure(2, 1)  # 4-connectivity
BLUR_TILE = 128  # the blurred image is kept per stroke in tiles of this many grid pixels per side ...
MAX_TILES = 1024  # ... up to this many (128 KB each: coarse and fine colours)
MIN_REACH = 40  # the smallest window radius `border_reach` may set (and at least the inner radius plus NEAR_MARGIN)
CORE = 0.25  # following the brush: the core around the centre that is always painted, as a share of the inner radius ...
MIN_CORE = 2  # ... and at least this many pixels


class Canvas:
    """A boolean grid without bounds, stored in tiles (a stroke can wander far)."""

    def __init__(self):
        self.tiles: dict[tuple[int, int], np.ndarray] = {}

    def _spans(self, u0: int, v0: int, n: int):
        for tv in range(v0 // TILE, (v0 + n - 1) // TILE + 1):
            for tu in range(u0 // TILE, (u0 + n - 1) // TILE + 1):
                a_u, a_v = max(u0, tu * TILE), max(v0, tv * TILE)  # the overlap of the window and the tile
                b_u, b_v = min(u0 + n, (tu + 1) * TILE), min(v0 + n, (tv + 1) * TILE)
                yield (tu, tv), (slice(a_v - tv * TILE, b_v - tv * TILE), slice(a_u - tu * TILE, b_u - tu * TILE)), \
                    (slice(a_v - v0, b_v - v0), slice(a_u - u0, b_u - u0))

    def get(self, u0: int, v0: int, n: int) -> np.ndarray:
        out = np.zeros((n, n), bool)
        for key, in_tile, in_window in self._spans(u0, v0, n):
            tile = self.tiles.get(key)
            if tile is not None:
                out[in_window] = tile[in_tile]
        return out

    def add(self, u0: int, v0: int, mask: np.ndarray) -> None:
        for key, in_tile, in_window in self._spans(u0, v0, mask.shape[0]):
            part = mask[in_window]
            if part.any():
                tile = self.tiles.get(key)
                if tile is None:
                    tile = self.tiles[key] = np.zeros((TILE, TILE), bool)
                tile[in_tile] |= part


def _rgb(packed: np.ndarray) -> np.ndarray:
    return np.stack([(packed >> 16) & 255, (packed >> 8) & 255, packed & 255], axis=1).astype(np.float32)


def profile(distance: np.ndarray, inner: int, near: float, far: float) -> np.ndarray:
    """A limit at these distances (pixels) from the brush's centre: `near` within the inner circle and a margin beyond
    it, then falling linearly over FALLOFF pixels to `far`."""
    plateau = inner + max(NEAR_MARGIN, inner / 2)
    t = np.clip((np.asarray(distance, np.float32) - plateau) / FALLOFF, 0, 1)
    return near + (far - near) * t


def strict_profile(distance: np.ndarray, inner: int) -> np.ndarray:
    """The allowed-colour limit of the strict mode at these distances from the brush's centre: STRICT_NEAR within the
    inner circle, beyond it a Gaussian falling to STRICT_FAR (sigma: STRICT_SIGMA of the inner radius, at least
    MIN_STRICT_SIGMA). With the default brush (inner radius 16): 14 inside, 11.1 at 4 px beyond the rim, 5.9 at 8 px,
    4.1 at 10 px, 2.1 at 16 px, where the normal mode allows 18 (16.3 at 16 px)."""
    distance = np.asarray(distance, np.float32)
    sigma = max(MIN_STRICT_SIGMA, STRICT_SIGMA * inner)
    beyond = np.maximum(distance - inner, 0) / sigma
    return (STRICT_FAR + (STRICT_NEAR - STRICT_FAR) * np.exp(-0.5 * beyond * beyond)).astype(np.float32)


def step_limit(distance: np.ndarray, inner: int) -> np.ndarray:
    """The neighbour step limit at these distances from the brush's centre: STEP_CENTRE at the centre, falling linearly
    to STEP_NEAR at the inner circle's rim; beyond, `profile` (STEP_NEAR, then falling to STEP_FAR)."""
    distance = np.asarray(distance, np.float32)
    centre = STEP_CENTRE + (STEP_NEAR - STEP_CENTRE) * np.clip(distance / max(inner, 1), 0, 1)
    return np.where(distance <= inner, centre, profile(distance, inner, STEP_NEAR, STEP_FAR))


def allowed_colours(exact: np.ndarray, inner_mask: np.ndarray, limit: np.ndarray) -> np.ndarray:
    """Pixels whose colour is within `limit` (a map, or one number) of some colour in `inner_mask` (maps of the window's
    size). Each distinct colour of the window is looked up once (a blurred window has far fewer colours than pixels)
    in a KD-tree of the inner circle's colours."""
    reference = np.unique(exact[inner_mask])
    if reference.size == 0:
        return np.zeros(exact.shape, bool)
    colours, inverse = np.unique(exact, return_inverse=True)
    distance, _ = cKDTree(_rgb(reference)).query(_rgb(colours), distance_upper_bound=float(np.max(limit)) + 1e-6)
    return distance[inverse].reshape(exact.shape) <= limit


class WindowMaps(NamedTuple):
    outer: np.ndarray      # the outer circle
    inner: np.ndarray      # the inner circle
    threshold: np.ndarray  # allowed-colour limit by distance (`profile`)
    step: np.ndarray       # neighbour step limit by distance (`step_limit`)


def window_maps(outer: int, inner: int, strict: bool = False) -> WindowMaps:
    """The maps of a dab window of radius `outer` for a brush of radius `inner` (`strict`: strict colours); the same for
    every dab of a stroke. Kept per brush size (and per value of the limits, which can be tuned while the plugin runs)."""
    return _window_maps(outer, inner, bool(strict), THRESHOLD_NEAR, THRESHOLD_FAR, STEP_NEAR, STEP_FAR, STEP_CENTRE,
                        NEAR_MARGIN, FALLOFF, STRICT_NEAR, STRICT_FAR, STRICT_SIGMA, MIN_STRICT_SIGMA)


@functools.lru_cache(maxsize=64)
def _window_maps(outer: int, inner: int, strict: bool, *limits) -> WindowMaps:
    n = 2 * outer + 1
    yy, xx = np.mgrid[0:n, 0:n]
    d2 = (xx - outer) ** 2 + (yy - outer) ** 2
    distance = np.sqrt(d2)
    threshold = strict_profile(distance, inner) if strict else profile(distance, inner, THRESHOLD_NEAR, THRESHOLD_FAR)
    maps = WindowMaps(d2 <= outer * outer, d2 <= inner * inner, threshold, step_limit(distance, inner))
    for array in maps:
        array.setflags(write=False)
    return maps


def _reference_colours(exact: np.ndarray, inner_mask: np.ndarray, keep_seeds):
    """The inner circle's colours (`keep_seeds(rows, cols)` restricts them), as packed colours."""
    if keep_seeds is not None:
        rows, cols = np.nonzero(inner_mask)
        keep = keep_seeds(rows, cols)
        inner_mask = np.zeros_like(inner_mask)
        inner_mask[rows[keep], cols[keep]] = True
    return np.unique(exact[inner_mask]), inner_mask


def core_disk(outer: int, inner: int, previous: tuple[int, int] | None = None) -> np.ndarray:
    """The core of the brush in a window of radius `outer`: the pixels within CORE of the inner radius of the centre, or,
    with `previous` (the previous dab's centre, relative to this one: du, dv), of the line from there to the centre. Dabs
    are one inner radius apart, so cores of the centres alone would leave gaps between them; along the line they join."""
    radius = max(MIN_CORE, CORE * inner)
    n = 2 * outer + 1
    yy, xx = np.mgrid[0:n, 0:n]
    px, py = xx - outer, yy - outer  # relative to the centre
    if previous is None or previous == (0, 0):
        return px * px + py * py <= radius * radius
    du, dv = previous
    t = np.clip((px * du + py * dv) / float(du * du + dv * dv), 0.0, 1.0)  # nearest point of the line, 0 = centre
    ex, ey = px - t * du, py - t * dv
    return ex * ex + ey * ey <= radius * radius


def _start(painted: np.ndarray, outer_mask: np.ndarray, centre: int, seed=None) -> tuple[np.ndarray, bool]:
    """Where the growth starts: the painted pixels inside the outer circle and `seed`, else (the first dab) the centre."""
    start = painted & outer_mask
    if seed is not None:
        return start | (seed & outer_mask), False
    if start.any():
        return start, False
    start = np.zeros_like(start)
    start[centre, centre] = True
    return start, True


def grow(exact: np.ndarray, painted: np.ndarray, inner: int, keep_seeds=None, outer: int = sb.OUTER,
         seed: np.ndarray | None = None, strict: bool = False) -> np.ndarray:
    """The dab without gap filling: the pixels of the (2*outer+1)^2 window (centred on the brush) that are not painted
    yet and connect, through allowed colours inside the outer circle and steps between neighbours within the step limit
    (both by `profile`), to the `painted` area (or to the centre if nothing in the outer circle is painted).
    `keep_seeds(rows, cols)` restricts the inner circle's colours. `seed` (a mask) is painted and grown from as well,
    whatever its colours (following the brush, see BorderStroke). `strict`: the strict colour limits (`strict_profile`)."""
    if numba is None:
        return grow_labelled(exact, painted, inner, keep_seeds, outer, seed, strict)
    maps = window_maps(outer, inner, strict)
    reference, _ = _reference_colours(exact, maps.inner, keep_seeds)
    start, from_centre = _start(painted, maps.outer, outer, seed)
    # The inner circle's colours sorted into cubes of the RGB space as large as the largest colour limit: a colour within
    # the limit of a pixel lies in the pixel's cube or one next to it.
    cell = max(1, int(math.ceil(float(maps.threshold.max()))))
    k = 255 // cell + 1
    r, g, b = (reference >> 16) & 255, (reference >> 8) & 255, reference & 255
    ids = ((r // cell) * k + g // cell) * k + b // cell
    order = np.argsort(ids, kind="stable")
    cell_start = np.searchsorted(ids[order], np.arange(k ** 3 + 1)).astype(np.int64)
    reached = _flood(np.ascontiguousarray(exact, np.int32), start, maps.outer, maps.threshold, maps.step,
                     r[order].astype(np.int32), g[order].astype(np.int32), b[order].astype(np.int32),
                     cell_start, cell, k).view(bool)
    if from_centre:
        reached[outer, outer] = True  # the start pixel itself (unpainted, unlike a start in the painted area)
    if seed is not None:
        reached |= seed & maps.outer
    return reached & ~painted


def _flood_python(exact, start, outer, threshold, step, ref_r, ref_g, ref_b, cell_start, cell, k):
    """The flood from the border of `start`: the pixels (not in `start`) reached through links within the step limit
    (the stricter of the two pixels') onto pixels inside `outer` whose colour is allowed, i.e. within `threshold` of a
    reference colour. Compiled with numba (`_flood`); the arithmetic is that of grow_labelled (float32 steps, float64
    colour distances), so both give the same pixels."""
    n = exact.shape[0]
    reached = np.zeros((n, n), np.uint8)
    allowed = np.zeros((n, n), np.int8)  # 0 not tested yet, 1 allowed, 2 not allowed
    queue = np.empty(n * n, np.int64)
    tail = 0
    for row in range(n):
        for col in range(n):
            if start[row, col] and ((row > 0 and not start[row - 1, col]) or (row < n - 1 and not start[row + 1, col])
                                    or (col > 0 and not start[row, col - 1]) or (col < n - 1 and not start[row, col + 1])):
                queue[tail] = row * n + col  # only the border of the start area can lead anywhere new
                tail += 1
    head = 0
    while head < tail:
        p = queue[head]
        head += 1
        row, col = p // n, p % n
        e = exact[row, col]
        pr, pg, pb = (e >> 16) & 255, (e >> 8) & 255, e & 255
        for direction in range(4):
            qr, qc = row, col
            if direction == 0:
                qr -= 1
            elif direction == 1:
                qr += 1
            elif direction == 2:
                qc -= 1
            else:
                qc += 1
            if qr < 0 or qr >= n or qc < 0 or qc >= n:
                continue
            if start[qr, qc] or reached[qr, qc] or not outer[qr, qc]:
                continue
            v = exact[qr, qc]
            dr, dg, db = ((v >> 16) & 255) - pr, ((v >> 8) & 255) - pg, (v & 255) - pb
            if np.float32(math.sqrt(dr * dr + dg * dg + db * db)) > min(step[row, col], step[qr, qc]):
                continue
            state = allowed[qr, qc]
            if state == 0:
                state = 2
                limit = threshold[qr, qc]
                vr, vg, vb = (v >> 16) & 255, (v >> 8) & 255, v & 255
                cr, cg, cb = vr // cell, vg // cell, vb // cell
                for ar in range(max(cr - 1, 0), min(cr + 1, k - 1) + 1):
                    for ag in range(max(cg - 1, 0), min(cg + 1, k - 1) + 1):
                        for ab in range(max(cb - 1, 0), min(cb + 1, k - 1) + 1):
                            c = (ar * k + ag) * k + ab
                            for i in range(cell_start[c], cell_start[c + 1]):
                                fr, fg, fb = vr - ref_r[i], vg - ref_g[i], vb - ref_b[i]
                                if math.sqrt(fr * fr + fg * fg + fb * fb) <= limit:
                                    state = 1
                                    break
                            if state == 1:
                                break
                        if state == 1:
                            break
                    if state == 1:
                        break
                allowed[qr, qc] = state
            if state == 2:
                continue
            reached[qr, qc] = 1
            queue[tail] = qr * n + qc
            tail += 1
    return reached


_flood = numba.njit(cache=True, nogil=True)(_flood_python) if numba is not None else None


def grow_labelled(exact: np.ndarray, painted: np.ndarray, inner: int, keep_seeds=None, outer: int = sb.OUTER,
                  seed: np.ndarray | None = None, strict: bool = False) -> np.ndarray:
    """`grow` done as a connected-component labelling of the whole window (see the module docstring)."""
    maps = window_maps(outer, inner, strict)
    reference, inner_mask = _reference_colours(exact, maps.inner, keep_seeds)
    allowed = _allowed_colours_of(exact, reference, maps.threshold) & maps.outer
    start, from_centre = _start(painted, maps.outer, outer, seed)
    if from_centre:
        allowed[outer, outer] = True  # the centre's own colour, even when the polygon's restriction left the inner circle empty
    labels = _label_with_steps(exact, allowed | start, start, maps.step)
    touching = np.unique(labels[start])
    return np.isin(labels, touching[touching > 0]) & ~painted


def _allowed_colours_of(exact: np.ndarray, reference: np.ndarray, limit: np.ndarray) -> np.ndarray:
    if reference.size == 0:
        return np.zeros(exact.shape, bool)
    colours, inverse = np.unique(exact, return_inverse=True)
    distance, _ = cKDTree(_rgb(reference)).query(_rgb(colours), distance_upper_bound=float(np.max(limit)) + 1e-6)
    return distance[inverse].reshape(exact.shape) <= limit


def _label_with_steps(exact: np.ndarray, passable: np.ndarray, start: np.ndarray, step: np.ndarray) -> np.ndarray:
    """Connected components of `passable` in which two neighbours are only connected if their colours differ by at
    most the stricter `step` (a map) of the two (painted pixels, `start`, always connect with each other). Labelled on a grid of twice the resolution:
    pixel (r, c) is cell (2r, 2c), the link to its right neighbour (2r, 2c+1), the one to the neighbour below
    (2r+1, 2c)."""
    n = exact.shape[0]
    rgb = _rgb(exact.ravel()).reshape(n, n, 3)
    right = passable[:, :-1] & passable[:, 1:] & (
        (np.linalg.norm(rgb[:, 1:] - rgb[:, :-1], axis=2) <= np.minimum(step[:, 1:], step[:, :-1]))
        | (start[:, :-1] & start[:, 1:]))
    down = passable[:-1] & passable[1:] & (
        (np.linalg.norm(rgb[1:] - rgb[:-1], axis=2) <= np.minimum(step[1:], step[:-1])) | (start[:-1] & start[1:]))
    grid = np.zeros((2 * n - 1, 2 * n - 1), bool)
    grid[::2, ::2] = passable
    grid[::2, 1::2] = right
    grid[1::2, ::2] = down
    labels, _ = ndimage.label(grid, structure=FOUR)
    return labels[::2, ::2]


def _polygon_in_window(polygon, u0: int, v0: int, n: int) -> np.ndarray:
    """The smart polygon (grid coordinates) rasterised into the window whose top-left is (u0, v0)."""
    picture = Image.new("1", (n, n), 0)
    draw = ImageDraw.Draw(picture)
    for part in getattr(polygon, "geoms", [polygon]):
        shift = lambda ring: [(x - u0 - 0.5, y - v0 - 0.5) for x, y in ring.coords]  # noqa: E731  pixel centres
        draw.polygon(shift(part.exterior), fill=1)
        for hole in part.interiors:
            draw.polygon(shift(hole), fill=0)
    return np.array(picture, bool)


def _unpack(packed: np.ndarray) -> np.ndarray:
    return np.stack([(packed >> 16) & 255, (packed >> 8) & 255, packed & 255], axis=-1).astype(np.float32)


def snap_tolerance(distance) -> np.ndarray:
    """How close to the painted side's mean colour a pixel at this distance (pixels, chessboard) beyond the grown border
    must be to be snapped: SNAP_TOLERANCE at the border, then a Gaussian of SNAP_TOLERANCE_SIGMA."""
    d = np.asarray(distance, np.float64)
    return SNAP_TOLERANCE * np.exp(-0.5 * (d / SNAP_TOLERANCE_SIGMA) ** 2)


def snap_to_edges(fine: np.ndarray, region: np.ndarray, new: np.ndarray, outer_mask: np.ndarray) -> np.ndarray:
    """`snap_to_edges_numpy`, compiled with numba where it is installed (one pass over the window instead of a dozen
    array operations; the same pixels, the tests compare the two)."""
    if _snap is None:
        return snap_to_edges_numpy(fine, region, new, outer_mask)
    tolerance2 = snap_tolerance(np.arange(SNAP + 1)) ** 2  # by distance 0 ... SNAP
    return _snap(np.ascontiguousarray(fine, np.int32), np.ascontiguousarray(region), np.ascontiguousarray(new),
                 np.ascontiguousarray(outer_mask), SNAP, SNAP_RING, SNAP_BLOCK, SNAP_CONTRAST * SNAP_CONTRAST,
                 tolerance2).view(bool)


def snap_to_edges_numpy(fine: np.ndarray, region: np.ndarray, new: np.ndarray, outer_mask: np.ndarray) -> np.ndarray:
    """The pixels that move what is painted (`region`) up to the edge it stopped short of (see SNAP): within SNAP of the
    region and inside the outer circle, nearer in colour (`fine`, the lightly blurred colours) to the region's side than to
    the other side, where both sides differ by SNAP_CONTRAST or more, connected to the region. Only looked at around `new`
    (what this dab added: the rest was looked at by the dabs before)."""
    out = np.zeros_like(region)
    # Where the new part borders on the unpainted: only there can the border move.
    frontier = new & ~ndimage.binary_erosion(region, FOUR, border_value=1)
    rows, cols = np.flatnonzero(frontier.any(axis=1)), np.flatnonzero(frontier.any(axis=0))
    if rows.size == 0:
        return out
    pad = 2 * (SNAP + SNAP_RING) + 1
    n = region.shape[0]
    r0, r1 = max(rows[0] - pad, 0), min(rows[-1] + pad + 1, n)
    c0, c1 = max(cols[0] - pad, 0), min(cols[-1] + pad + 1, n)
    inside = region[r0:r1, c0:c1]
    # Distances to the region and into it (chessboard: a fast two-pass transform; enough for bands this narrow).
    distance_out = ndimage.distance_transform_cdt(~inside, metric="chessboard")  # 0 in the region
    band = ~inside & (distance_out <= SNAP) & outer_mask[r0:r1, c0:c1]
    if not band.any():
        return out
    outside_ring = ~inside & (distance_out > SNAP) & (distance_out <= SNAP + SNAP_RING)
    inside_ring = inside & (ndimage.distance_transform_cdt(inside, metric="chessboard") <= SNAP_RING)
    rgb = _unpack(fine[r0:r1, c0:c1])
    br, bc = np.nonzero(band)  # only the band's pixels are judged: the means are looked up for them alone
    h, w = inside.shape
    # The means are over squares of about 2 * (SNAP + SNAP_RING) pixels: taken on a grid of SNAP_BLOCK x SNAP_BLOCK blocks.
    b = SNAP_BLOCK
    hb, wb = -(-h // b), -(-w // b)
    half = -(-(SNAP + SNAP_RING) // b)
    rb, cb = br // b, bc // b
    top, bottom = np.maximum(rb - half, 0), np.minimum(rb + half + 1, hb)
    left, right = np.maximum(cb - half, 0), np.minimum(cb + half + 1, wb)

    def local_mean(ring):
        """The mean colour of `ring` in the square of blocks around each band pixel, and whether it has any: box sums of
        the colours and the count, from a summed-area table of the blocks."""
        stacked = np.zeros((hb * b, wb * b, 4), np.float32)
        np.multiply(rgb, ring[..., None], out=stacked[:h, :w, :3])
        stacked[:h, :w, 3] = ring
        # a block holds at most 16 pixels: float32 sums are exact; the table of the (few) blocks is summed in float64
        blocks = stacked.reshape(hb, b, wb * b, 4).sum(axis=1).reshape(hb, wb, b, 4).sum(axis=2).astype(np.float64)
        table = np.zeros((hb + 1, wb + 1, 4), np.float64)
        table[1:, 1:] = blocks.cumsum(axis=0).cumsum(axis=1)
        sums = table[bottom, right] - table[top, right] - table[bottom, left] + table[top, left]
        weight = sums[:, 3]
        return sums[:, :3] / np.maximum(weight, 1e-9)[:, None], weight > 0.5

    mean_in, has_in = local_mean(inside_ring)
    mean_out, has_out = local_mean(outside_ring)
    colour = rgb[br, bc]
    to_in = ((colour - mean_in) ** 2).sum(axis=-1)
    to_out = ((colour - mean_out) ** 2).sum(axis=-1)
    contrast = ((mean_in - mean_out) ** 2).sum(axis=-1) >= SNAP_CONTRAST * SNAP_CONTRAST
    close = to_in <= snap_tolerance(distance_out[br, bc]) ** 2
    keep = has_in & has_out & contrast & (to_in < to_out) & close
    candidates = np.zeros_like(band)
    candidates[br[keep], bc[keep]] = True
    if not candidates.any():
        return out
    labels, _ = ndimage.label(inside | candidates, structure=FOUR)
    touching = np.unique(labels[inside])
    out[r0:r1, c0:c1] = candidates & np.isin(labels, touching[touching > 0])
    return out


def _chessboard(mask, r0, r1, c0, c1, cap):
    """Chessboard distance of every pixel of the crop to the nearest `mask` pixel in the crop (0 on it), capped at `cap`:
    two passes, as distance_transform_cdt does."""
    h, w = r1 - r0, c1 - c0
    d = np.full((h, w), cap, np.int32)
    for r in range(h):
        for c in range(w):
            if mask[r0 + r, c0 + c]:
                d[r, c] = 0
                continue
            best = d[r, c]
            if r > 0:
                best = min(best, d[r - 1, c] + 1)
                if c > 0:
                    best = min(best, d[r - 1, c - 1] + 1)
                if c < w - 1:
                    best = min(best, d[r - 1, c + 1] + 1)
            if c > 0:
                best = min(best, d[r, c - 1] + 1)
            d[r, c] = best
    for r in range(h - 1, -1, -1):
        for c in range(w - 1, -1, -1):
            best = d[r, c]
            if best == 0:
                continue
            if r < h - 1:
                best = min(best, d[r + 1, c] + 1)
                if c > 0:
                    best = min(best, d[r + 1, c - 1] + 1)
                if c < w - 1:
                    best = min(best, d[r + 1, c + 1] + 1)
            if c < w - 1:
                best = min(best, d[r, c + 1] + 1)
            d[r, c] = best
    return d


def _snap_python(fine, region, new, outer, snap, ring_width, block, contrast2, tolerance2):
    """snap_to_edges_numpy as loops (compiled with numba as `_snap`); the same steps and arithmetic."""
    n = region.shape[0]
    out = np.zeros((n, n), np.uint8)
    # the frontier: new pixels with a 4-neighbour (inside the window) that is not painted
    r_min, r_max, c_min, c_max = n, -1, n, -1
    for r in range(n):
        for c in range(n):
            if new[r, c] and ((r > 0 and not region[r - 1, c]) or (r < n - 1 and not region[r + 1, c])
                              or (c > 0 and not region[r, c - 1]) or (c < n - 1 and not region[r, c + 1])):
                r_min, r_max = min(r_min, r), max(r_max, r)
                c_min, c_max = min(c_min, c), max(c_max, c)
    if r_max < 0:
        return out
    pad = 2 * (snap + ring_width) + 1
    r0, r1 = max(r_min - pad, 0), min(r_max + pad + 1, n)
    c0, c1 = max(c_min - pad, 0), min(c_max + pad + 1, n)
    h, w = r1 - r0, c1 - c0
    cap = snap + ring_width + 1
    distance_out = _chessboard(region, r0, r1, c0, c1, cap)
    not_region = np.empty((n, n), np.bool_)
    for r in range(r0, r1):
        for c in range(c0, c1):
            not_region[r, c] = not region[r, c]
    distance_in = _chessboard(not_region, r0, r1, c0, c1, ring_width + 1)
    # block sums of the colours and counts of both rings
    hb, wb = (h + block - 1) // block, (w + block - 1) // block
    sums_in = np.zeros((hb + 1, wb + 1, 4), np.float64)
    sums_out = np.zeros((hb + 1, wb + 1, 4), np.float64)
    band_count = 0
    for r in range(h):
        for c in range(w):
            inside = region[r0 + r, c0 + c]
            d = distance_out[r, c]
            if not inside and 0 < d <= snap and outer[r0 + r, c0 + c]:
                band_count += 1
            target = 0
            if inside and distance_in[r, c] <= ring_width:
                target = 1
            elif not inside and snap < d <= snap + ring_width:
                target = 2
            if target == 0:
                continue
            v = fine[r0 + r, c0 + c]
            br, bc = r // block + 1, c // block + 1
            table = sums_in if target == 1 else sums_out
            table[br, bc, 0] += (v >> 16) & 255
            table[br, bc, 1] += (v >> 8) & 255
            table[br, bc, 2] += v & 255
            table[br, bc, 3] += 1.0
    if band_count == 0:
        return out
    for table in (sums_in, sums_out):  # summed-area tables of the blocks
        for k in range(4):
            for r in range(1, hb + 1):
                for c in range(1, wb + 1):
                    table[r, c, k] += table[r - 1, c, k] + table[r, c - 1, k] - table[r - 1, c - 1, k]
    half = (snap + ring_width + block - 1) // block
    candidates = np.zeros((h, w), np.uint8)
    for r in range(h):
        for c in range(w):
            if region[r0 + r, c0 + c] or not (0 < distance_out[r, c] <= snap) or not outer[r0 + r, c0 + c]:
                continue
            rb, cb = r // block, c // block
            top, bottom = max(rb - half, 0), min(rb + half + 1, hb)
            left, right = max(cb - half, 0), min(cb + half + 1, wb)
            v = fine[r0 + r, c0 + c]
            colour = (np.float64((v >> 16) & 255), np.float64((v >> 8) & 255), np.float64(v & 255))
            means = []
            ok = True
            for table in (sums_in, sums_out):
                weight = table[bottom, right, 3] - table[top, right, 3] - table[bottom, left, 3] + table[top, left, 3]
                if weight <= 0.5:
                    ok = False
                    break
                m0 = (table[bottom, right, 0] - table[top, right, 0] - table[bottom, left, 0] + table[top, left, 0]) / weight
                m1 = (table[bottom, right, 1] - table[top, right, 1] - table[bottom, left, 1] + table[top, left, 1]) / weight
                m2 = (table[bottom, right, 2] - table[top, right, 2] - table[bottom, left, 2] + table[top, left, 2]) / weight
                means.append((m0, m1, m2))
            if not ok:
                continue
            (a0, a1, a2), (b0, b1, b2) = means[0], means[1]
            if (a0 - b0) ** 2 + (a1 - b1) ** 2 + (a2 - b2) ** 2 < contrast2:
                continue
            to_in = (colour[0] - a0) ** 2 + (colour[1] - a1) ** 2 + (colour[2] - a2) ** 2
            to_out = (colour[0] - b0) ** 2 + (colour[1] - b1) ** 2 + (colour[2] - b2) ** 2
            if to_in < to_out and to_in <= tolerance2[distance_out[r, c]]:
                candidates[r, c] = 1
    # keep the candidates connected (4-neighbours) to the painted area inside the crop
    queue = np.empty(h * w, np.int64)
    tail = 0
    for r in range(h):
        for c in range(w):
            if candidates[r, c] and ((r > 0 and region[r0 + r - 1, c0 + c]) or (r < h - 1 and region[r0 + r + 1, c0 + c])
                                     or (c > 0 and region[r0 + r, c0 + c - 1]) or (c < w - 1 and region[r0 + r, c0 + c + 1])):
                candidates[r, c] = 2
                queue[tail] = r * w + c
                tail += 1
    head = 0
    while head < tail:
        p = queue[head]
        head += 1
        r, c = p // w, p % w
        out[r0 + r, c0 + c] = 1
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            qr, qc = r + dr, c + dc
            if 0 <= qr < h and 0 <= qc < w and candidates[qr, qc] == 1:
                candidates[qr, qc] = 2
                queue[tail] = qr * w + qc
                tail += 1
    return out


if numba is not None:
    _chessboard = numba.njit(cache=True, nogil=True)(_chessboard)
    _snap = numba.njit(cache=True, nogil=True)(_snap_python)
else:
    _snap = None


def window_radius(reach, inner: int) -> int:
    """The dab window's radius for the `border_reach` setting (None: sb.OUTER)."""
    try:
        reach = int(reach)
    except (TypeError, ValueError):
        return sb.OUTER
    return max(MIN_REACH, inner + NEAR_MARGIN, min(sb.OUTER, reach))


class BorderStroke(sb.Stroke):
    """A stroke whose dabs grow its painted area from its border (see the module docstring). `reach` is the radius of
    its dabs' window (the `border_reach` setting; None: OUTER).

    With `follow` (the `border_follow` setting), the stroke cannot be left behind: growing only from the painted area,
    a dab adds nothing when an edge sharper than the step limit lies between that area and the brush, nor does the next,
    and the stroke stops while the brush moves on. So a dab that does not reach the brush's centre also grows from the
    centre, as the first dab does: the core around it (CORE of the inner radius, at least MIN_CORE pixels) is painted and
    grown from by the same rules, and so is the core along the line from the previous dab's centre, so that the new part
    joins what is painted instead of becoming an island. A stroke that keeps up with the brush is not affected, and a smart
    polygon never (only its inside counts there)."""

    def __init__(self, *args, reach=None, follow=True, sensitivity="normal", snap=True, smooth=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.smooth = smooth is not False  # the `border_smooth` setting: a smooth outline when the stroke ends (painted_outline)
        self.snap = snap is not False  # the `border_snap` setting: move the border up to the edge (snap_to_edges)
        self.outer = window_radius(reach, self.inner)
        self.follow = follow is not False
        self.strict = sensitivity == "strict"  # the brush panel's colour switch, fixed for the stroke
        self.previous: tuple[int, int] | None = None  # the last dab's centre (grid), for following along the line
        self.canvas = Canvas()  # everything the stroke painted: where the growth starts from
        self.grown = Canvas()  # ... without the pixels added by snapping: what the gaps are filled between (see _dab)
        self.tiles: OrderedDict[tuple[int, int], np.ndarray] = OrderedDict()  # (coarse, fine) colours, see _colours

    def painted_outline(self, canvas: np.ndarray):
        """The painted pixels as a polygon; with `smooth`, a smooth one (sb.smooth_mask_outline): no lone pixels, spurs
        or notches, no pixel staircase. The preview while painting shows the pixels; the annotation gets this."""
        if not self.smooth:
            return sb.mask_to_geometry(canvas)
        return sb.smooth_mask_outline(canvas, SMOOTH_SIGMA, SMOOTH_STRAIGHTEN, SMOOTH_ROUNDS)

    @property
    def simplify_tolerance(self) -> float:
        return 0.2 if self.smooth else 0.5  # the smooth curve keeps its points; the pixel outline loses its staircase

    def _colours(self, read_region, u0: int, v0: int, n: int) -> tuple[np.ndarray, np.ndarray]:
        """The (coarse, fine) blurred colours of the n x n grid pixels from (u0, v0), assembled from the stroke's tiles:
        coarse (sb.BLUR_SIGMA) for the growth, fine (SNAP_SIGMA) for snapping to edges. Tiles not read yet are read and
        blurred together, one rectangle per row of them (the first dab: one rectangle, then a strip as the brush moves on).
        A reader without `box` gets the old way: one centred region per dab."""
        box = getattr(read_region, "box", None)
        m = sb.MARGIN
        if box is None:
            exact, fine = sb.blur_exact_and_fine(read_region(self, u0 + self.outer, v0 + self.outer), SNAP_SIGMA)
            o = (exact.shape[0] - n) // 2
            return exact[o:o + n, o:o + n], fine[o:o + n, o:o + n]
        t = BLUR_TILE
        tu0, tv0, tu1, tv1 = u0 // t, v0 // t, (u0 + n - 1) // t, (v0 + n - 1) // t
        for tv in range(tv0, tv1 + 1):
            missing = [tu for tu in range(tu0, tu1 + 1) if (tu, tv) not in self.tiles]
            if not missing:
                continue
            a, b = missing[0], missing[-1]  # one read for the row, from its first to its last missing tile
            exact, fine = sb.blur_exact_and_fine(box(self, a * t - m, tv * t - m, (b - a + 1) * t + 2 * m, t + 2 * m),
                                                 SNAP_SIGMA)
            for tu in missing:
                self.tiles[(tu, tv)] = np.stack([exact[:, (tu - a) * t:(tu - a + 1) * t], fine[:, (tu - a) * t:(tu - a + 1) * t]])
        out = np.empty((2, n, n), np.int32)
        for tv in range(tv0, tv1 + 1):
            for tu in range(tu0, tu1 + 1):
                tile = self.tiles[(tu, tv)]
                self.tiles.move_to_end((tu, tv))
                r0, c0 = max(v0, tv * t), max(u0, tu * t)
                r1, c1 = min(v0 + n, (tv + 1) * t), min(u0 + n, (tu + 1) * t)
                out[:, r0 - v0:r1 - v0, c0 - u0:c1 - u0] = tile[:, r0 - tv * t:r1 - tv * t, c0 - tu * t:c1 - tu * t]
        while len(self.tiles) > MAX_TILES:
            self.tiles.popitem(last=False)
        return out[0], out[1]

    def _dab(self, read_region, pu: int, pv: int, keep_seeds=None) -> dict | None:
        o = self.outer
        n = 2 * o + 1
        u0, v0 = pu - o, pv - o
        exact, fine = self._colours(read_region, u0, v0, n)
        # Two kinds of painted pixels: grown ones (the growth and its gap filling) and snapped ones (snap_to_edges). The
        # growth starts only from grown pixels: snapped ones lie on the edge, past the steep part of its colour ramp, and a
        # growth from there would step into the next region wherever its colours are allowed. Gaps are filled only between
        # grown pixels too: snapping makes holes smaller (a nucleus' hole shrinks to the nucleus), and filling would then
        # close them over what they hold. Snapped pixels only count as painted (they are not painted again).
        with timing.step("canvas"):
            painted = self.canvas.get(u0, v0, n)
            grown = self.grown.get(u0, v0, n)
        if self.polygon is not None and self.polygon.intersects(shapely.box(u0, v0, u0 + n, v0 + n)):
            polygon = _polygon_in_window(self.polygon, u0, v0, n)
            painted |= polygon
            grown |= polygon
        with timing.step("grow"):
            reached = grow(exact, grown, self.inner, keep_seeds, o, strict=self.strict) & ~painted
        if self.follow and keep_seeds is None and not (painted[o, o] or reached[o, o]):
            # Left behind (see the class docstring): grow from the brush's core as well, along the line from the previous
            # dab's centre (painted already), so the new part joins the painting instead of starting an island.
            previous = None
            if self.previous is not None:
                du, dv = self.previous[0] - pu, self.previous[1] - pv
                # Dabs of a drag are `spacing` apart; farther is a jump (e.g. a call that starts elsewhere): the centre only.
                previous = (du, dv) if max(abs(du), abs(dv)) <= 2 * self.spacing else None
            with timing.step("grow (follow)"):
                reached |= grow(exact, grown | reached, self.inner, None, o, seed=core_disk(o, self.inner, previous),
                                strict=self.strict) & ~painted
        if keep_seeds is None:
            self.previous = (pu, pv)
        mask = sb.fill_gaps(grown | reached) & ~painted
        with timing.step("canvas"):
            self.grown.add(u0, v0, mask)
        if self.snap:
            # measured from the grown pixels: snapping never builds on earlier snapping (it would creep on, dab by dab)
            with timing.step("snap"):
                mask |= snap_to_edges(fine, grown | mask, mask, window_maps(o, self.inner).outer) & ~painted
        with timing.step("canvas"):
            self.canvas.add(u0, v0, mask)
        self.dabs.append((u0, v0, mask))
        return sb.dab_reply(self, u0, v0, mask)
