"""Border growth: dabs grow the stroke's painted area from its border, through the inner circle's colours."""
import sys
import time
import unittest
from pathlib import Path

import numpy as np
import shapely
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import borderbrush as bb  # noqa: E402
import smartbrush as sb  # noqa: E402

N = 2 * sb.OUTER + 1                   # dab window
R = 2 * (sb.OUTER + sb.MARGIN) + 1     # region read for a dab
HALF = sb.OUTER + sb.MARGIN
RED, BLUE, BLACK = (200, 60, 60), (40, 40, 190), (10, 10, 10)


def reader_of(picture, origin):
    """read_region for a picture whose pixel (origin, origin) is grid pixel (0, 0); outside it is black."""
    pad = np.zeros((picture.shape[0] + 2 * R, picture.shape[1] + 2 * R, 3), np.uint8)
    pad[R:-R, R:-R] = picture

    def read(stroke, u, v):
        y, x = v + origin + R - HALF, u + origin + R - HALF
        return pad[y:y + R, x:x + R]

    def box(stroke, u0, v0, width, height=None):  # any rectangle (the stroke's tiles)
        y, x = v0 + origin + R, u0 + origin + R
        return pad[y:y + (width if height is None else height), x:x + width]
    read.box = box
    return read


class BorderGrowthTest(unittest.TestCase):
    def test_the_first_dab_grows_from_the_centre_and_stops_at_a_colour_edge(self):
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = RED
        picture[:, 560:] = BLUE  # 60 px right of the centre
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0)
        stroke.to_dab(reader_of(picture, 500), 0.0, 0.0)
        mask = stroke.dabs[-1][2]
        o = sb.OUTER
        self.assertTrue(mask[o, o] and mask[o, o - 150])
        self.assertFalse(mask[:, o + 60:].any())
        self.assertTrue(mask[o, o + 60 - int(2.5 * sb.BLUR_SIGMA)])  # up to the edge, less what the blur mixes
        yy, xx = np.mgrid[0:N, 0:N]
        self.assertFalse(mask[(xx - o) ** 2 + (yy - o) ** 2 > (o + 1) ** 2].any())  # inside the outer circle

    def test_later_dabs_grow_only_from_the_painted_border(self):
        # Red left, a black stripe, blue right. The first dab paints the red. A dab centred in the blue, with the red
        # painting inside its outer circle, allows only blue: blue does not touch the painted red, so nothing is added
        # (unless the stroke follows the brush, see below).
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = RED
        picture[:, 540:600] = BLACK
        picture[:, 600:] = BLUE
        read = reader_of(picture, 500)
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=10, follow=False)
        stroke._dab(read, 0, 0)
        first = stroke.dabs[-1][2]
        self.assertTrue(first[sb.OUTER, sb.OUTER] and not first[:, sb.OUTER + 40:].any())
        self.assertIsNone(stroke._dab(read, 130, 0))  # in the blue, 130 px right
        following = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=10)  # border_follow on (the default)
        following._dab(read, 0, 0)
        jump = following._dab(read, 130, 0)
        self.assertIsNotNone(jump)
        self.assertTrue(following.dabs[-1][2][sb.OUTER, sb.OUTER])  # the blue around the brush is painted
        stripe = following.dabs[-1][2][sb.OUTER, sb.OUTER - 90:sb.OUTER - 30]  # x = 40 ... 100: the black stripe
        self.assertFalse(stripe.any())  # ... but not the black stripe between it and the red
        # A dab further down the red grows the painting on from its border: new red below the first dab's circle.
        stroke._dab(read, 0, 150)
        later = stroke.dabs[-1][2]
        self.assertTrue(later[sb.OUTER + 150, sb.OUTER])
        self.assertFalse(later[sb.OUTER - 150, sb.OUTER])  # painted by the first dab already: not again

    def test_a_disconnected_island_of_an_allowed_colour_stays_out(self):
        # A red island inside a black ring, next to the centre; the inner circle holds red (and black, which the blur
        # mixes into other colours, but not the ring's core colour). From the centre, the island is not reachable.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = RED
        yy, xx = np.mgrid[0:1000, 0:1000]
        d = np.hypot(yy - 500, xx - 640)
        picture[(d >= 40) & (d < 70)] = BLACK
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=20)
        stroke._dab(reader_of(picture, 500), 0, 0)
        mask = stroke.dabs[-1][2]
        self.assertTrue(mask[sb.OUTER, sb.OUTER - 100])
        self.assertFalse(mask[sb.OUTER, sb.OUTER + 140])  # the island's centre

    def test_the_limits_are_lenient_near_the_inner_circle_and_strict_further_out(self):
        # inner 20: lenient up to 20 + 10 = 30 px, then falling over FALLOFF px to strict
        self.assertEqual(float(bb.profile(np.array(0.0), 20, 18, 5)), 18)
        self.assertEqual(float(bb.profile(np.array(30.0), 20, 18, 5)), 18)
        self.assertAlmostEqual(float(bb.profile(np.array(30.0 + bb.FALLOFF / 2), 20, 18, 5)), 11.5, places=4)
        self.assertEqual(float(bb.profile(np.array(150.0), 20, 18, 5)), 5)
        self.assertEqual(float(bb.profile(np.array(10.0), 4, 3, 2)), 3)  # small brushes: at least NEAR_MARGIN beyond

    def test_a_colour_near_the_brush_is_allowed_but_not_far_away(self):
        # Grey, and from 20 px right of the centre a grey 12 units bluer: a gentle edge (the neighbour steps pass). The
        # small inner circle holds only the first grey, so the bluer one is 12 away: allowed within the lenient zone
        # (threshold 18 up to 6 + 8 = 14 px, falling to 5 at 74 px: 12 at about 46 px), not beyond.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = (100, 100, 100)
        picture[:, 520:] = (100, 100, 112)
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=6)
        stroke._dab(reader_of(picture, 500), 0, 0)
        mask = stroke.dabs[-1][2]
        o = sb.OUTER
        self.assertTrue(mask[o, o - 180])  # the first grey, out to the outer circle
        self.assertTrue(mask[o, o + 35])  # the bluer grey near the brush
        self.assertFalse(mask[o, o + 80])  # ... but not further out

    def test_the_step_limit_is_loose_at_the_centre(self):
        self.assertEqual(float(bb.step_limit(np.array(0.0), 16)), bb.STEP_CENTRE)
        self.assertEqual(float(bb.step_limit(np.array(16.0), 16)), bb.STEP_NEAR)
        self.assertEqual(float(bb.step_limit(np.array(150.0), 16)), bb.STEP_FAR)
        # A click right on an edge between two greys (blurred, about 8 per pixel across it, too much for STEP_NEAR):
        # with the loose centre the dab gets off the edge to both sides; with a strict one it only follows the edge.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = (100, 100, 100)
        picture[:, 500:] = (160, 160, 160)
        read = reader_of(picture, 500)
        o = sb.OUTER
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16)
        stroke._dab(read, 0, 0)
        mask = stroke.dabs[-1][2]
        self.assertTrue(mask[o, o - 30] and mask[o, o + 30])
        centre, bb.STEP_CENTRE = bb.STEP_CENTRE, bb.STEP_NEAR
        try:
            strict = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16)
            strict._dab(read, 0, 0)
            self.assertFalse(strict.dabs[-1][2][o, o - 30] or strict.dabs[-1][2][o, o + 30])
        finally:
            bb.STEP_CENTRE = centre

    def test_growth_compares_with_the_neighbour(self):
        # Grey left, lighter grey right of the centre + 10. The inner circle holds both and the blurred ramp between
        # them, so every colour is allowed; but across the edge neighbouring pixels differ by more than the step limit.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = (100, 100, 100)
        picture[:, 510:] = (160, 160, 160)
        read = reader_of(picture, 500)
        o = sb.OUTER
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=30)
        stroke._dab(read, 0, 0)
        mask = stroke.dabs[-1][2]
        self.assertTrue(mask[o, o - 100])
        self.assertFalse(mask[o, o + 60])  # the lighter grey is allowed, but not reached
        steps = bb.STEP_NEAR, bb.STEP_FAR
        bb.STEP_NEAR = bb.STEP_FAR = 1000.0  # without the neighbour comparison it is
        try:
            loose = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=30)
            loose._dab(read, 0, 0)
            self.assertTrue(loose.dabs[-1][2][o, o + 60])
        finally:
            bb.STEP_NEAR, bb.STEP_FAR = steps

    def test_smart_polygon_grows_from_inside_only(self):
        # Red left of grid column 60, blue right of it; the polygon's right side is 8 px from the blue.
        picture = np.zeros((1200, 1200, 3), np.uint8)
        picture[:] = RED
        picture[:, 660:] = BLUE
        read = reader_of(picture, 600)
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, -50.0)
        square = [(0, -50), (52, -50), (52, 50), (0, 50)]
        stroke.outline(square)
        while stroke.todo:
            stroke.outline_dabs(read)
        g = stroke.geometry()
        self.assertTrue(g.buffer(1e-6).contains(sb.shapely.Polygon(square)))
        x0, y0, x1, y1 = g.bounds
        self.assertLessEqual(x1, 60)
        self.assertLess(x0, -100)
        self.assertLess(y0, -150)

    def test_canvas_keeps_masks_across_tiles(self):
        canvas = bb.Canvas()
        mask = np.zeros((300, 300), bool)
        mask[10:290, 20:280] = True
        canvas.add(-400, 250, mask)
        self.assertTrue(np.array_equal(canvas.get(-400, 250, 300), mask))
        self.assertEqual(canvas.get(-390, 260, 300).sum(), mask[10:, 10:].sum())
        self.assertFalse(canvas.get(1000, 1000, 100).any())

    def test_a_dab_is_quick(self):
        rng = np.random.default_rng(0)
        picture = rng.integers(90, 160, (1200, 1200, 3), dtype=np.uint8)  # noisy grey: a large, ragged area
        read = reader_of(picture, 600)
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=30)
        stroke._dab(read, 0, 0)
        t = time.perf_counter()
        for i in range(1, 6):
            stroke._dab(read, 30 * i, 0)
        self.assertLess((time.perf_counter() - t) / 5, 0.5)


if __name__ == "__main__":
    unittest.main()


def random_window(rng, n, kind):
    """Blurred-looking packed colours of an n x n window: smooth regions with noise, and sharp edges between them."""
    yy, xx = np.mgrid[0:n, 0:n]
    base = np.zeros((n, n, 3), np.int64)
    for _ in range(6):
        cx, cy, r = rng.integers(0, n, 2).tolist() + [int(rng.integers(n // 8, n // 2))]
        base[(xx - cx) ** 2 + (yy - cy) ** 2 <= r * r] = rng.integers(40, 220, 3)
    if kind == "smooth":  # a ramp, like a blurred edge
        base = base + (xx[..., None] * rng.integers(0, 3, 3) // 8)
    noise = rng.integers(-2 if kind != "noisy" else -6, 3 if kind != "noisy" else 7, (n, n, 3))
    rgb = np.clip(base + noise, 0, 255)
    return ((rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]).astype(np.int32)


class FloodMatchesLabellingTest(unittest.TestCase):
    """The flood (numba) and the labelling of the whole window apply the same rule: the same pixels, always."""

    def check(self, exact, painted, inner, outer, keep_seeds=None):
        expected = bb.grow_labelled(exact, painted, inner, keep_seeds, outer)
        got = bb.grow(exact, painted, inner, keep_seeds, outer)
        np.testing.assert_array_equal(got, expected)
        return expected

    def test_random_windows_and_painted_areas(self):
        if bb.numba is None:
            self.skipTest("numba is not installed")
        rng = np.random.default_rng(7)
        reached_something = 0
        for trial in range(40):
            outer = int(rng.choice([40, 64, 120]))
            n = 2 * outer + 1
            inner = int(rng.integers(3, outer // 2))
            exact = random_window(rng, n, ["smooth", "noisy", "edges"][trial % 3])
            painted = np.zeros((n, n), bool)
            if trial % 4:  # an earlier dab's area left of the centre, with a hole, partly outside the outer circle
                painted[:, : outer - int(rng.integers(0, outer // 2))] = True
                painted[outer - 5:outer + 5, 10:20] = False
                painted &= rng.random((n, n)) < 0.995
            keep = None
            if trial % 5 == 0:
                keep = lambda rows, cols: cols < outer  # noqa: E731  only the left half's colours (a smart polygon)
            reached_something += int(self.check(exact, painted, inner, outer, keep).any())
        self.assertGreater(reached_something, 20, "the comparison has to cover dabs that grow")

    def test_the_first_dab_and_an_empty_inner_circle(self):
        if bb.numba is None:
            self.skipTest("numba is not installed")
        rng = np.random.default_rng(3)
        exact = random_window(rng, 81, "smooth")
        none = np.zeros((81, 81), bool)
        self.assertTrue(self.check(exact, none, 10, 40)[40, 40], "the centre is always part of the first dab")
        self.check(exact, none, 10, 40, keep_seeds=lambda r, c: np.zeros(r.shape, bool))

    def test_the_flood_is_quick(self):
        if bb.numba is None:
            self.skipTest("numba is not installed")
        rng = np.random.default_rng(5)
        n = 2 * sb.OUTER + 1
        exact = random_window(rng, n, "smooth")
        painted = np.zeros((n, n), bool)
        painted[:, : sb.OUTER] = True
        bb.grow(exact, painted, 16)  # compiled on the first call
        t = time.perf_counter()
        for _ in range(5):
            bb.grow(exact, painted, 16)
        flood = (time.perf_counter() - t) / 5
        t = time.perf_counter()
        bb.grow_labelled(exact, painted, 16)
        labelled = time.perf_counter() - t
        self.assertLess(flood, labelled)


class StrokeSpeedupsTest(unittest.TestCase):
    def picture(self):
        rng = np.random.default_rng(11)
        picture = np.zeros((1600, 1600, 3), np.uint8)
        picture[:] = RED
        yy, xx = np.mgrid[0:1600, 0:1600]
        for _ in range(60):
            cx, cy, r = rng.integers(0, 1600, 2).tolist() + [int(rng.integers(20, 90))]
            picture[(xx - cx) ** 2 + (yy - cy) ** 2 <= r * r] = rng.integers(40, 220, 3)
        return np.clip(picture.astype(int) + rng.integers(-4, 5, picture.shape), 0, 255).astype(np.uint8)

    def test_the_tile_cache_paints_what_reading_each_window_paints(self):
        picture = self.picture()
        tiled = reader_of(picture, 800)
        windows = reader_of(picture, 800)
        del windows.box  # the old way: one centred region per dab
        results = []
        for read in (tiled, windows):
            stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16)
            for i in range(12):
                stroke.to_dab(read, float(i * 16), float(i * 7))
            results.append(stroke)
        a, b = (sum(m.sum() for _, _, m in s.dabs) for s in results)
        self.assertGreater(a, 1000)
        # The blur of a tile and of a window can round a colour differently now and then (another convolution size).
        self.assertLess(abs(int(a) - int(b)), 0.002 * a)
        self.assertGreater(len(results[0].tiles), 9)

    def test_reach_sets_the_window(self):
        self.assertEqual(bb.window_radius(None, 16), sb.OUTER)
        self.assertEqual(bb.window_radius("x", 16), sb.OUTER)
        self.assertEqual(bb.window_radius(80, 16), 80)
        self.assertEqual(bb.window_radius(5, 16), bb.MIN_REACH)
        self.assertEqual(bb.window_radius(50, 60), 60 + bb.NEAR_MARGIN)
        self.assertEqual(bb.window_radius(999, 16), sb.OUTER)
        picture = self.picture()
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16, reach=64)
        stroke.to_dab(reader_of(picture, 800), 0.0, 0.0)
        stroke.to_dab(reader_of(picture, 800), 64.0, 0.0)
        self.assertTrue(all(m.shape == (129, 129) for _, _, m in stroke.dabs))
        self.assertIsNotNone(stroke.geometry())

    def test_replies_are_cut_to_what_the_dab_paints(self):
        stroke = bb.BorderStroke("a.svs", "paint", 2.0, 100.0, 50.0)
        mask = np.zeros((401, 401), bool)
        mask[10:20, 30:35] = True
        reply = sb.dab_reply(stroke, -200, -200, mask)
        self.assertEqual((reply["x"], reply["y"], reply["w"], reply["h"]), (100 + (-200 + 30) / 2, 50 + (-200 + 10) / 2, 2.5, 5.0))
        self.assertIsNone(sb.dab_reply(stroke, 0, 0, np.zeros((5, 5), bool)))


class ThreadsTest(unittest.TestCase):
    def test_usable_cpus_follows_the_cgroup_quota(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            Path(root, "sys/fs/cgroup").mkdir(parents=True)
            Path(root, "sys/fs/cgroup/cpu.max").write_text("250000 100000\n")
            self.assertEqual(sb.usable_cpus(root), min(3, sb.usable_cpus("/nonexistent")))
            Path(root, "sys/fs/cgroup/cpu.max").write_text("max 100000\n")
            self.assertEqual(sb.usable_cpus(root), sb.usable_cpus("/nonexistent"))
        self.assertLessEqual(sb.torch.get_num_threads(), sb.usable_cpus())


class FollowTheBrushTest(unittest.TestCase):
    """A dab that does not reach the brush's centre also grows from the core around it (the `border_follow` setting)."""

    def edge_picture(self):
        # Two greys with a hard edge 40 px right of the start: blurred, about 8 per pixel across it, above every step limit.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = (100, 100, 100)
        picture[:, 540:] = (170, 170, 170)
        return reader_of(picture, 500)

    def drag(self, follow, read, points):
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16, follow=follow)
        for x, y in points:
            stroke.to_dab(read, float(x), float(y))
        return stroke

    def painted_at(self, stroke, u, v):
        n = 2 * stroke.outer + 1
        return bool(stroke.canvas.get(u, v, 1)[0, 0]) if n else False

    def test_dragging_across_an_edge_is_followed(self):
        read = self.edge_picture()
        path = [(x, 0) for x in range(0, 161, 16)]
        stuck = self.drag(False, read, path)
        self.assertTrue(self.painted_at(stuck, 0, 0))
        self.assertFalse(self.painted_at(stuck, 120, 0), "the old way: stopped at the edge, the brush went on")
        follows = self.drag(True, read, path)
        for x in (60, 100, 160):
            self.assertTrue(self.painted_at(follows, x, 0), f"followed to x={x}")
        self.assertTrue(self.painted_at(follows, 160, 60), "beyond the core, the light grey grows as usual")

    def test_a_stroke_that_keeps_up_is_not_changed(self):
        rng = np.random.default_rng(2)
        picture = np.clip(np.full((1000, 1000, 3), 150) + rng.integers(-3, 4, (1000, 1000, 3)), 0, 255).astype(np.uint8)
        read = reader_of(picture, 500)
        path = [(x, x // 3) for x in range(0, 200, 16)]
        a, b = self.drag(True, read, path), self.drag(False, read, path)
        for (_, _, ma), (_, _, mb) in zip(a.dabs, b.dabs):
            np.testing.assert_array_equal(ma, mb)

    def test_the_core_is_painted_even_on_an_edge(self):
        # Centre right on a hard edge, nothing painted near it: at least the core is painted.
        read = self.edge_picture()
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16)
        stroke._dab(read, 0, 0)  # starts in the dark grey
        stroke._dab(read, 40, 0)  # far enough that the first dab's painting does not reach this centre
        mask = stroke.dabs[-1][2]
        o = stroke.outer
        radius = max(bb.MIN_CORE, bb.CORE * 16)
        self.assertTrue(mask[o, o] or stroke.dabs[0][2][o, o + 40])
        self.assertTrue(self.painted_at(stroke, 40, 0) and self.painted_at(stroke, 40 + int(radius) - 1, 0))

    def test_smart_polygons_do_not_follow(self):
        n = 81
        exact = np.full((n, n), (100 << 16) | (100 << 8) | 100, np.int32)
        exact[:, 50:] = (170 << 16) | (170 << 8) | 170
        painted = np.zeros((n, n), bool)
        painted[:, :20] = True
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=10)
        self.assertTrue(stroke.follow)
        # keep_seeds (a smart polygon) switches it off: the check in _dab is `keep_seeds is None`
        self.assertFalse(bb.grow(exact, painted, 10, lambda r, c: c < 30, 40)[40, 40 + 20])

    def test_the_flood_and_the_labelling_agree_with_a_seed(self):
        if bb.numba is None:
            self.skipTest("numba is not installed")
        rng = np.random.default_rng(9)
        for trial in range(15):
            outer = 64
            n = 2 * outer + 1
            exact = random_window(rng, n, ["smooth", "noisy", "edges"][trial % 3])
            painted = np.zeros((n, n), bool)
            painted[:, :20] = True
            seed = bb.core_disk(outer, int(rng.integers(4, 30)))
            np.testing.assert_array_equal(bb.grow(exact, painted, 16, None, outer, seed),
                                          bb.grow_labelled(exact, painted, 16, None, outer, seed))


class FollowAlongTheLineTest(unittest.TestCase):
    """Following the brush seeds the core along the line from the previous dab's centre: no islands along a drag."""

    def lined_picture(self):
        # Grey with thin dark lines across the path every 40 px: the growth cannot cross them.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = (150, 150, 150)
        for x in range(520, 900, 40):
            picture[:, x:x + 6] = (30, 30, 30)
        return reader_of(picture, 500)

    def drag(self, read, centre_only=False):
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16)
        for x in range(0, 321, 8):
            if centre_only:
                stroke.previous = None  # how it was: a core around the centre only
            stroke.to_dab(read, float(x), 0.0)
        return stroke

    def path_gaps(self, stroke, v=0, last=300):
        row = stroke.canvas.get(0, v, last + 1)[0]
        return int((~row).sum())

    def test_the_path_is_painted_without_gaps(self):
        read = self.lined_picture()
        islands = self.drag(read, centre_only=True)
        self.assertGreater(self.path_gaps(islands), 0, "seeding the centre only leaves gaps at the lines")
        joined = self.drag(read)
        self.assertEqual(self.path_gaps(joined), 0)
        # the band is as wide as the core on both sides of the path, not more
        core = max(bb.MIN_CORE, bb.CORE * 16)
        self.assertEqual(self.path_gaps(joined, v=int(core) - 1), 0)

    def test_the_core_along_the_line(self):
        line = bb.core_disk(40, 16, (-16, 0))  # previous centre 16 px to the left
        self.assertTrue(line[40, 40] and line[40, 32] and line[40, 24])  # centre, halfway, previous centre
        self.assertFalse(line[40, 19] or line[40, 45] or line[46, 32])  # beyond the ends and the core's radius (4)
        np.testing.assert_array_equal(bb.core_disk(40, 16, (0, 0)), bb.core_disk(40, 16))


class StrictColoursTest(unittest.TestCase):
    """The brush panel's Colors switch: strict colour limits beyond the inner circle, the same steps."""

    def test_the_strict_limit_falls_as_a_gaussian_beyond_the_inner_circle(self):
        inner = 16
        sigma = max(bb.MIN_STRICT_SIGMA, bb.STRICT_SIGMA * inner)
        at = lambda d: float(bb.strict_profile(np.array(float(d)), inner))  # noqa: E731
        self.assertEqual(at(0), bb.STRICT_NEAR)
        self.assertEqual(at(inner), bb.STRICT_NEAR)  # the same everywhere inside the inner circle
        self.assertLess(bb.STRICT_NEAR, bb.THRESHOLD_NEAR, "stricter than normal there too")
        self.assertAlmostEqual(at(inner + sigma), bb.STRICT_FAR + (bb.STRICT_NEAR - bb.STRICT_FAR) * np.exp(-0.5), places=4)
        self.assertAlmostEqual(at(inner + 6 * sigma), bb.STRICT_FAR, places=3)
        distance = np.linspace(0, 200, 401)
        normal = bb.profile(distance, inner, bb.THRESHOLD_NEAR, bb.THRESHOLD_FAR)
        strict = bb.strict_profile(distance, inner)
        self.assertTrue(np.all(strict <= normal + 1e-6), "strict is never more lenient than normal")
        self.assertTrue(np.all(np.diff(strict) <= 1e-6), "and falls with the distance")
        self.assertFalse(np.array_equal(bb.window_maps(60, 16, True).threshold, bb.window_maps(60, 16).threshold))
        np.testing.assert_array_equal(bb.window_maps(60, 16, True).step, bb.window_maps(60, 16).step)

    def gradient(self):
        # A slow colour ramp to the right (1 per 4 px, far below every step limit): only the colour limit stops the growth.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        ramp = np.clip(100 + (np.arange(1000) - 500) / 4, 0, 255).astype(np.uint8)
        picture[:, :, 0] = ramp[None, :]
        picture[:, :, 1] = 120
        picture[:, :, 2] = 140
        return reader_of(picture, 500)

    def reach_right(self, sensitivity):
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16, sensitivity=sensitivity)
        stroke._dab(self.gradient(), 0, 0)
        row = stroke.dabs[-1][2][stroke.outer, stroke.outer:]
        return int(np.argmin(row)) if not row.all() else row.size

    def test_strict_leaks_less_into_a_slowly_changing_colour(self):
        normal, strict = self.reach_right("normal"), self.reach_right("strict")
        self.assertGreater(normal, strict + 10)
        self.assertGreater(strict, 16, "still past the inner circle")
        self.assertEqual(bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0).strict, False, "normal is the default")

    def test_the_flood_and_the_labelling_agree_in_strict_mode(self):
        if bb.numba is None:
            self.skipTest("numba is not installed")
        rng = np.random.default_rng(4)
        for trial in range(15):
            outer = 64
            n = 2 * outer + 1
            exact = random_window(rng, n, ["smooth", "noisy", "edges"][trial % 3])
            painted = np.zeros((n, n), bool)
            if trial % 2:
                painted[:, :30] = True
            np.testing.assert_array_equal(bb.grow(exact, painted, 12, None, outer, strict=True),
                                          bb.grow_labelled(exact, painted, 12, None, outer, strict=True))


class SnapToEdgesTest(unittest.TestCase):
    """The coarse blur makes the growth stop a few pixels short of an edge; snapping moves the border up to it."""

    def edge(self, left, right, noise=5, at=60):
        rng = np.random.default_rng(0)
        picture = np.zeros((1000, 1000, 3), np.int64)
        picture[:] = left
        picture[:, 500 + at:] = right
        return reader_of(np.clip(picture + rng.integers(-noise, noise + 1, picture.shape), 0, 255).astype(np.uint8), 500)

    def last_painted(self, read, snap, rows=(-30, 0, 30), **kw):
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16, snap=snap, **kw)
        stroke._dab(read, 0, 0)
        mask, o = stroke.dabs[-1][2], stroke.outer
        return [int(np.flatnonzero(mask[o + r, o:o + 150])[-1]) for r in rows]

    def test_the_border_moves_up_to_a_sharp_edge(self):
        read = self.edge((200, 60, 60), (40, 40, 190))
        short = self.last_painted(read, snap=False)
        self.assertTrue(all(x <= 55 for x in short), f"without snapping it stops short: {short}")
        snapped = self.last_painted(read, snap=True)
        # The lightly blurred copy still mixes the last two pixels with the other side, and the snapped colour has to be
        # close to the painted side's (snap_tolerance): within 3 px of the edge, never beyond.
        self.assertTrue(all(56 <= x <= 59 for x in snapped), snapped)
        self.assertTrue(all(a >= b + 5 for a, b in zip(snapped, short)), "clearly nearer than without snapping")

    def test_a_weaker_edge_too_and_strict_mode(self):
        read = self.edge((150, 120, 130), (120, 95, 115))
        self.assertTrue(all(57 <= x <= 59 for x in self.last_painted(read, snap=True)))
        self.assertTrue(all(56 <= x <= 59 for x in self.last_painted(read, snap=True, sensitivity="strict")))

    def no_edge(self, picture):
        read = reader_of(picture, 500)
        masks = []
        for snap in (False, True):
            stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16, snap=snap)
            stroke._dab(read, 0, 0)
            masks.append(stroke.dabs[-1][2])
        return int(masks[1].sum()) - int(masks[0].sum())

    def test_nothing_is_added_where_there_is_no_edge(self):
        ramp = np.zeros((1000, 1000, 3), np.uint8)  # a slow colour change: the growth stops at the colour limit
        ramp[:, :, 0] = np.clip(100 + (np.arange(1000) - 500) / 4, 0, 255).astype(np.uint8)[None, :]
        ramp[:, :, 1:] = 130
        self.assertLessEqual(self.no_edge(ramp), 0.01 * 3.14 * 60 * 60)
        rng = np.random.default_rng(1)  # texture: noise of +-12 on one colour
        texture = np.clip(np.full((1000, 1000, 3), 160) + rng.integers(-12, 13, (1000, 1000, 3)), 0, 255).astype(np.uint8)
        self.assertLessEqual(self.no_edge(texture), 50)

    def test_snapping_never_crosses_a_thin_line(self):
        # A dark line 3 px wide, then the same colour again: the far side must stay out.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = (180, 150, 160)
        picture[:, 560:563] = (40, 30, 40)
        last = self.last_painted(reader_of(picture, 500), snap=True)
        self.assertTrue(all(x < 60 for x in last), last)

    def test_the_stroke_keeps_fine_colours_in_its_tiles(self):
        read = self.edge((200, 60, 60), (40, 40, 190))
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16)
        coarse, fine = stroke._colours(read, -100, -100, 201)
        self.assertEqual(coarse.shape, fine.shape)
        # the fine copy keeps the edge sharp: the colours change within a few pixels, the coarse ones over many
        row = 100
        jumps = lambda c: int((np.abs(np.diff(_unpack_r(c[row]))) > 3).sum())  # noqa: E731
        self.assertLess(jumps(fine), jumps(coarse))
        self.assertTrue(stroke.snap, "on by default")


def _unpack_r(packed):
    return ((packed >> 16) & 255).astype(int)


class SnapCompiledMatchesNumpyTest(unittest.TestCase):
    def test_random_regions(self):
        if bb.numba is None:
            self.skipTest("numba is not installed")
        rng = np.random.default_rng(12)
        snapped_something = 0
        for trial in range(30):
            outer = int(rng.choice([40, 64, 120]))
            n = 2 * outer + 1
            fine = random_window(rng, n, ["smooth", "noisy", "edges"][trial % 3])
            region = np.zeros((n, n), bool)
            yy, xx = np.mgrid[0:n, 0:n]
            for _ in range(3):
                cx, cy, r = rng.integers(0, n, 2).tolist() + [int(rng.integers(5, outer))]
                region |= (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
            new = region & (rng.random((n, n)) < 0.7) if trial % 2 else region.copy()
            outer_mask = bb.window_maps(outer, 10).outer
            expected = bb.snap_to_edges_numpy(fine, region, new, outer_mask)
            np.testing.assert_array_equal(bb.snap_to_edges(fine, region, new, outer_mask), expected)
            snapped_something += int(expected.any())
        self.assertGreater(snapped_something, 10, "the comparison has to cover cases that snap")



class SnapLeavesHolesAloneTest(unittest.TestCase):
    def test_a_dark_spot_inside_the_painting_stays_unpainted(self):
        # A dark spot (a nucleus) 34 x 34 px in a light region. The growth stops some pixels short of it, so the hole is
        # wider than the gap filling's 40 px and stays open; snapping moves the border onto the spot, the hole shrinks to
        # 34 px, and the gap filling must not then close it over the spot, in this dab nor in later ones.
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = (235, 225, 235)
        picture[483:517, 543:577] = (90, 40, 90)
        read = reader_of(picture, 500)
        holes = {}
        for snap in (False, True):
            stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16, snap=snap)
            for x in range(0, 161, 16):  # past the spot, 45 px below its centre (the brush itself never covers it)
                stroke.to_dab(read, float(x), 45.0)
            area = stroke.canvas.get(20, -40, 80)  # u 20..99, v -40..39: the spot and around it
            spot = area[25:55, 25:55]  # u 45..74, v -15..14: the spot without its outermost 2 px (snapping rounds its corners)
            self.assertFalse(spot.any(), f"snap={snap}: the spot was painted over")
            holes[snap] = int((~area).sum())
            self.assertTrue(stroke.canvas.get(0, 0, 1)[0, 0] and stroke.canvas.get(140, 0, 1)[0, 0],
                            "the light region around the spot is painted")
        self.assertLess(holes[True], holes[False] - 100, "snapping moved the border onto the spot")
        self.assertGreaterEqual(holes[True], 34 * 34 - 8, "the hole is the spot (its corners rounded), no more")



class SnapDoesNotLeakTest(unittest.TestCase):
    def test_snapping_does_not_build_on_itself(self):
        # Two regions differing by 20: a weak edge. Painted along it, dab by dab, snapping adds a strip at most SNAP wide
        # (it is measured from grown pixels, and the growth never starts from snapped ones).
        rng = np.random.default_rng(3)
        picture = np.zeros((1000, 1000, 3), np.int64)
        picture[:] = (200, 160, 180)
        picture[:, 560:] = (186, 150, 170)
        picture = np.clip(picture + rng.integers(-4, 5, picture.shape), 0, 255).astype(np.uint8)
        read = reader_of(picture, 500)
        canvases = []
        for snap in (False, True):
            stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=16, snap=snap)
            for y in range(-150, 151, 16):
                stroke.to_dab(read, 20.0, float(y))
            canvases.append(stroke.canvas.get(-300, -150, 600))
        off, on = canvases
        added = on & ~off
        near = ndimage.binary_dilation(off, iterations=bb.SNAP + 4)
        self.assertEqual(int((added & ~near).sum()), 0)

    def test_the_tolerance_falls_with_the_distance(self):
        t = bb.snap_tolerance(np.arange(bb.SNAP + 1))
        self.assertEqual(float(t[0]), bb.SNAP_TOLERANCE)
        self.assertTrue(np.all(np.diff(t) < 0))
        self.assertLess(float(t[bb.SNAP]), bb.SNAP_TOLERANCE / 3)


class SmoothOutlineTest(unittest.TestCase):
    """The outline of a finished Border growth stroke is smoothed (border_smooth)."""

    def test_the_staircase_becomes_a_curve(self):
        yy, xx = np.mgrid[0:120, 0:120]
        disk = (xx - 60) ** 2 + (yy - 60) ** 2 <= 40 ** 2  # its pixel outline is a staircase
        steps = sb.mask_to_geometry(disk)
        smooth = sb.smooth_mask_outline(disk)
        self.assertAlmostEqual(smooth.area, np.pi * 40 ** 2, delta=0.02 * np.pi * 40 ** 2)
        # a staircase has only horizontal and vertical edges; the smooth outline has (almost) none
        def axis_edges(g):
            c = np.asarray(g.exterior.coords)
            d = np.diff(c, axis=0)
            return np.mean((np.abs(d[:, 0]) < 1e-9) | (np.abs(d[:, 1]) < 1e-9))
        self.assertGreater(axis_edges(steps), 0.99)
        self.assertLess(axis_edges(smooth), 0.1)
        # and it stays close to the circle (the pixel disk itself is up to about 0.7 px off it)
        radius = np.hypot(*(np.asarray(smooth.exterior.coords) - 60).T)
        self.assertLess(np.max(np.abs(radius - 40)), 1.25)

    def test_lone_pixels_spurs_and_notches_go_holes_stay(self):
        mask = np.zeros((100, 100), bool)
        mask[20:80, 20:80] = True
        mask[40:60, 40:60] = False  # a hole (a nucleus): stays
        mask[10, 10] = True  # a lone pixel: goes
        mask[50, 80:84] = True  # a spur one pixel wide: goes
        mask[20, 50] = False  # a notch of one pixel: filled
        g = sb.smooth_mask_outline(mask)
        self.assertEqual(g.geom_type, "Polygon")
        self.assertEqual(len(g.interiors), 1)
        self.assertFalse(g.contains(shapely.Point(10.5, 10.5)))
        self.assertFalse(g.contains(shapely.Point(82.5, 50.5)))
        self.assertTrue(g.contains(shapely.Point(50.5, 20.5)))
        self.assertAlmostEqual(g.area, 60 * 60 - 20 * 20, delta=0.03 * (60 * 60 - 20 * 20))

    def test_a_stroke_commits_a_smooth_outline_unless_switched_off(self):
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = (200, 60, 60)
        picture[:, 560:] = (40, 40, 190)
        read = reader_of(picture, 500)
        geometries = {}
        for smooth in (False, True):
            stroke = bb.BorderStroke("a.svs", "paint", 0.5, 1000.0, 1000.0, inner=16, smooth=smooth)
            for x in range(0, 100, 16):
                stroke.to_dab(read, 1000.0 + 2 * x, 1000.0)
            geometries[smooth] = stroke.geometry()
        rough, smooth = geometries[False], geometries[True]
        self.assertTrue(smooth.is_valid)
        self.assertLess(abs(smooth.area - rough.area), 0.02 * rough.area)
        self.assertLess(smooth.symmetric_difference(rough).area, 0.05 * rough.area)
