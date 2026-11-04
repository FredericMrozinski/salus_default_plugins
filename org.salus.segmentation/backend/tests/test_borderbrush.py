"""Border growth: dabs grow the stroke's painted area from its border, through the inner circle's colours."""
import sys
import time
import unittest
from pathlib import Path

import numpy as np

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
        # (the colour brush would paint the blue there).
        picture = np.zeros((1000, 1000, 3), np.uint8)
        picture[:] = RED
        picture[:, 540:600] = BLACK
        picture[:, 600:] = BLUE
        read = reader_of(picture, 500)
        stroke = bb.BorderStroke("a.svs", "paint", 1.0, 0.0, 0.0, inner=10)
        stroke._dab(read, 0, 0)
        first = stroke.dabs[-1][2]
        self.assertTrue(first[sb.OUTER, sb.OUTER] and not first[:, sb.OUTER + 40:].any())
        self.assertIsNone(stroke._dab(read, 130, 0))  # in the blue, 130 px right
        self.assertTrue(sb.dab_mask(read(stroke, 130, 0), inner=10)[sb.OUTER, sb.OUTER])  # the colour brush would
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
