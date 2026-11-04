"""Model brushes, with a stand-in model: the view is split into a left and a right region."""
import sys
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import modelbrush as mb  # noqa: E402
import smartbrush as sb  # noqa: E402

VIEW = 256


def halves(rgb):
    labels = np.ones(rgb.shape[:2], np.int32)
    labels[:, rgb.shape[1] // 2:] = 2
    return labels


class ModelBrushTest(unittest.TestCase):
    def setUp(self):
        self._read = sb.read_view
        sb.read_view = lambda slide, x, y, scale, size: np.zeros((size, size, 3), np.uint8)
        self.model = mb.register("halves", "Halves", halves, view=VIEW)
        mb._views.clear()
        mb._jobs.clear()

    def tearDown(self):
        sb.read_view = self._read
        mb.SEGMENTERS.pop("halves", None)

    def stroke(self, inner=20):
        st = mb.ModelStroke(self.model, "a.svs", "paint", 1.0, 0.0, 0.0, inner=inner)
        st.slide = object()
        return st

    def dab(self, st, x, y):
        for _ in range(200):
            dabs = st.to_dab(None, x, y)
            if not st.waiting:
                return dabs
            time.sleep(0.01)
        self.fail("the model never finished")

    def test_waits_then_paints_the_region_under_the_centre_inside_the_circle(self):
        first = self.stroke()
        self.assertEqual(first.to_dab(None, 0.0, 0.0), [])  # the view (centred here) is being segmented
        self.assertTrue(first.waiting)
        self.dab(first, 0.0, 0.0)  # the regions' border runs through this point
        st = self.stroke()
        self.dab(st, -5.0, 0.0)  # 5 px left of the border: same view, no wait
        mask = st.dabs[-1][2]
        o = sb.OUTER
        self.assertTrue(mask[o, o])
        self.assertFalse(mask[o, o + 10])  # right of the border: the other region
        self.assertFalse(mask[o, o - 25])  # outside the circle
        self.assertTrue(mask[o + 19, o])
        self.assertEqual(len(mb._views), 1)

    def test_a_failing_model_reports_its_error(self):
        mb.register("broken", "Broken", lambda rgb: np.zeros((3, 3)), view=VIEW)  # wrong shape
        try:
            st = mb.ModelStroke(mb.SEGMENTERS["broken"], "a.svs", "paint", 1.0, 0.0, 0.0, inner=10)
            st.slide = object()
            with self.assertRaises(sb.ViewerError):
                for _ in range(200):
                    st.to_dab(None, 0.0, 0.0)
                    time.sleep(0.01)
        finally:
            mb.SEGMENTERS.pop("broken", None)

    def test_edge_model_fills_up_to_the_edges(self):
        def ring(rgb):  # the outline of a square around the view's centre, with a 2 px gap in its top side
            e = np.zeros(rgb.shape[:2], np.uint8)
            c = rgb.shape[0] // 2
            e[c - 10, c - 10:c + 11] = e[c + 10, c - 10:c + 11] = 1
            e[c - 10:c + 11, c - 10] = e[c - 10:c + 11, c + 10] = 1
            e[c - 10, c:c + 2] = 0
            return e
        model = mb.register("ring", "Ring", ring, view=VIEW, output="edges")
        try:
            # the view is centred on the first dab (grid pixel 0, centre at 0.5): the ring's centre is at 0.5, 0.5
            st = mb.ModelStroke(model, "a.svs", "paint", 1.0, 0.0, 0.0, inner=30)
            st.slide = object()
            self.dab(st, 0.0, 0.0)
            mask = st.dabs[-1][2]
            o = sb.OUTER
            inside = mask[o - 9:o + 10, o - 9:o + 10]
            self.assertTrue(inside.all())  # the square's inside
            self.assertTrue(mask[o - 10, o - 5] and mask[o + 10, o])  # the edge line itself
            self.assertFalse(mask[o - 12, o])  # beyond the gap: closed (2 px <= 2 * close)
            self.assertFalse(mask[o, o + 15])  # outside the square
            self.assertEqual(mask.sum(), 21 * 21)  # the square with its outline, the closed gap included
        finally:
            mb.SEGMENTERS.pop("ring", None)

    def test_rays_through_a_hole_are_brought_back_to_the_edge(self):
        edges = np.zeros((41, 41), bool)
        edges[5, :] = True
        edges[5, 20:22] = False  # a 2 px gap, 15 px above the centre: about 7 degrees of rays get through
        disk = np.ones((41, 41), bool)
        self.assertFalse(mb.fill_between_edges(edges, disk, close=0)[2, 20])  # no closing needed: the rays are repaired
        self.assertTrue(mb.fill_between_edges(edges, disk, close=0, neighbours=2)[2, 20])  # too few neighbours: it leaks
        fill = mb.fill_between_edges(edges, disk, close=0)
        self.assertTrue(fill[5, 10] and fill[6, 20] and fill[30, 20])  # the edge line and everything below it
        self.assertFalse(fill[:5].any())
        wide = edges.copy()
        wide[5, 5:36] = False  # a hole wider than the neighbourhood: a real opening
        self.assertTrue(mb.fill_between_edges(wide, disk, close=1)[2, 20])

    def test_outlier_rays_are_interpolated_and_steps_kept(self):
        lengths = np.full(360, 20.0)
        lengths[100:108] = 80  # a hole, 8 degrees wide
        lengths[200:300] = 50  # a step: the edge ends there
        lengths[250] = 30  # a speck
        fixed = mb.repair_outliers(lengths, 10)
        self.assertTrue(np.allclose(fixed[95:115], 20) and fixed[250] == 50)
        self.assertTrue(np.allclose(fixed[205:295], 50) and fixed[150] == 20)

    def test_a_centre_on_an_edge_starts_next_to_it(self):
        edges = np.zeros((41, 41), bool)
        edges[10, :] = True
        edges[18:23, 18:23] = True  # the centre on an edge
        fill = mb.fill_between_edges(edges, np.ones((41, 41), bool), close=1)
        self.assertTrue(fill[14, 20])  # the nearest free side, between the line and the blob
        self.assertFalse(fill[5, 20])

    def test_rays_are_fast(self):
        rng = np.random.default_rng(0)
        edges = rng.random((201, 201)) > 0.995  # specks
        yy, xx = np.mgrid[-100:101, -100:101]
        disk = xx * xx + yy * yy <= 100 * 100
        mb.fill_between_edges(edges, disk, close=1)
        t = time.perf_counter()
        for _ in range(10):
            mb.fill_between_edges(edges, disk, close=1)
        self.assertLess((time.perf_counter() - t) / 10, 0.05)  # well under the time between dabs

    def test_ids_of_the_built_in_modes_are_refused(self):
        with self.assertRaises(ValueError):
            mb.register("color", "Colour", halves)


if __name__ == "__main__":
    unittest.main()
