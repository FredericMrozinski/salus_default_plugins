"""Border growth: an experimental colour-similarity brush that grows the stroke from its own border.

Like the colour-similarity brush (smartbrush.py), a dab has an inner and an outer circle, and the inner circle defines
the allowed colours: a pixel may join if its exact (blurred) colour is close to the colour of some pixel in the inner
circle. In addition, the growth compares with the neighbour: a pixel is only entered from a 4-neighbour whose colour
differs from its own by at most a step limit. Inside a region the colour changes little from pixel to pixel (after the
blur), across an edge it changes more, so the growth does not chain through a series of allowed colours across an edge.

Both limits depend on the distance from the inner circle (`profile`): lenient (THRESHOLD_NEAR, STEP_NEAR) inside it and
up to NEAR_MARGIN beyond, then falling linearly over FALLOFF pixels to strict (THRESHOLD_FAR, STEP_FAR) for the rest of
the outer circle. So the brush is generous where it is held and strict further away, and the user steers the growth
by where the brush goes and by its width. Inside the inner circle the step limit is loosened further, from STEP_CENTRE
at the centre down to STEP_NEAR at the inner circle's rim (`step_limit`): otherwise a click on a fine structure or an
edge, where the colour changes by more than STEP_NEAR from one pixel to the next, could not leave its centre pixel.

The difference to the colour brush is where the growth starts. The colour brush floods from seeds spread over the
inner circle, at every dab anew. Border growth starts from what the stroke has painted so far: the dab adds everything inside
the outer circle that is connected, through allowed pixels, to the painted area there. With nothing painted in the outer
circle yet (the first dab), the brush's centre pixel is the start. So the area spreads out from its border as the
brush moves, and an allowed colour that is not connected to the area is never reached.

The growth is per pixel (no stride), 4-connected, done as one connected-component labelling (scipy, in C) of the
allowed pixels plus the painted ones: a component touching the painted area is what a flood from its border reaches.
For the neighbour comparison the labelling runs on a grid of twice the resolution, in which the links between
neighbouring pixels are cells of their own, set only if the two colours are within the step limit there.
Then gaps up to GAP pixels enclosed on all four sides are filled, as in the colour brush. The allowed-colour test looks
up each distinct colour of the window once in a KD-tree of the inner circle's colours (scipy); the colour buckets of the
colour brush are not needed.
"""
from __future__ import annotations

import numpy as np
import shapely
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.spatial import cKDTree

import smartbrush as sb

# Limits by distance from the inner circle (see `profile`): NEAR inside it and up to NEAR_MARGIN pixels beyond (at least
# half the inner radius), then falling linearly over FALLOFF pixels to FAR.
NEAR_MARGIN = 8
FALLOFF = 60
# Allowed colours: Euclidean RGB distance to the nearest colour of the inner circle (the colour brush: 15 everywhere).
THRESHOLD_NEAR = 18
THRESHOLD_FAR = 5
# Neighbour comparison: largest colour difference between neighbouring pixels (blurred colours, as above). They are
# whole numbers, so steps come in sqrt(1), sqrt(2), sqrt(3), 2, sqrt(5), sqrt(6), ...: 2 lets through one unit in up to
# three channels or two in one; 3 also two units in two channels.
STEP_NEAR = 3.0
STEP_FAR = 2.0
STEP_CENTRE = 10.0  # ... and at the brush's centre, falling to STEP_NEAR at the inner circle's rim
TILE = 256  # the stroke's painted area is kept in tiles of this many grid pixels per side
FOUR = ndimage.generate_binary_structure(2, 1)  # 4-connectivity, like the colour brush's flood


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


def grow(exact: np.ndarray, painted: np.ndarray, inner: int, keep_seeds=None) -> np.ndarray:
    """The dab without gap filling: the pixels of the (2*OUTER+1)^2 window (centred on the brush) that are not painted
    yet and connect, through allowed colours inside the outer circle and steps between neighbours within the step limit
    (both by `profile`), to the `painted` area (or to the centre if nothing in the outer circle is
    painted). `keep_seeds(rows, cols)` restricts the inner circle's colours."""
    n = 2 * sb.OUTER + 1
    c = sb.OUTER
    yy, xx = np.mgrid[0:n, 0:n]
    d2 = (xx - c) ** 2 + (yy - c) ** 2
    outer = d2 <= sb.OUTER * sb.OUTER
    inner_mask = d2 <= inner * inner
    if keep_seeds is not None:
        rows, cols = np.nonzero(inner_mask)
        inner_mask = np.zeros_like(inner_mask)
        keep = keep_seeds(rows, cols)
        inner_mask[rows[keep], cols[keep]] = True
    distance = np.sqrt(d2)
    allowed = allowed_colours(exact, inner_mask, profile(distance, inner, THRESHOLD_NEAR, THRESHOLD_FAR)) & outer
    start = painted & outer
    if not start.any():
        start = np.zeros_like(start)
        start[c, c] = True
        allowed[c, c] = True  # the centre's own colour, even when the polygon's restriction left the inner circle empty
    labels = _label_with_steps(exact, allowed | start, start, step_limit(distance, inner))
    touching = np.unique(labels[start])
    return np.isin(labels, touching[touching > 0]) & ~painted


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


class BorderStroke(sb.Stroke):
    """A stroke whose dabs grow its painted area from its border (see the module docstring)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.canvas = Canvas()

    def _dab(self, read_region, pu: int, pv: int, keep_seeds=None) -> dict | None:
        n = 2 * sb.OUTER + 1
        u0, v0 = pu - sb.OUTER, pv - sb.OUTER
        _, exact = sb.prepare(read_region(self, pu, pv))
        painted = self.canvas.get(u0, v0, n)
        if self.polygon is not None and self.polygon.intersects(shapely.box(u0, v0, u0 + n, v0 + n)):
            painted |= _polygon_in_window(self.polygon, u0, v0, n)
        reached = grow(exact, painted, self.inner, keep_seeds)
        mask = sb.fill_gaps(painted | reached) & ~painted  # gaps between the new and the earlier painting too
        self.canvas.add(u0, v0, mask)
        self.dabs.append((u0, v0, mask))
        if not mask.any():
            return None
        return {"x": self.x0 + u0 / self.scale, "y": self.y0 + v0 / self.scale,
                "w": n / self.scale, "h": n / self.scale, "png": sb.png_of(mask)}

