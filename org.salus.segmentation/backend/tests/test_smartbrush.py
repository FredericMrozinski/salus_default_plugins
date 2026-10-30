import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import smartbrush as sb  # noqa: E402
from tests.test_segmentation import Base  # noqa: E402
from annotations import Annotations  # noqa: E402

N = 2 * sb.OUTER + 1                   # dab window
R = 2 * (sb.OUTER + sb.MARGIN) + 1     # region read for a dab


def fill_rows_reference(mask, gap):
    """Straightforward scan: fill runs of up to `gap` unset pixels between set pixels in each row."""
    out = mask.copy()
    for row in out:
        idx = np.flatnonzero(row)
        for a, b in zip(idx[:-1], idx[1:]):
            if 1 < b - a <= gap + 1:
                row[a + 1:b] = True
    return out


def fill_reference(mask, gap, all4):
    h, v = fill_rows_reference(mask, gap), fill_rows_reference(mask.T, gap).T
    return (h & v) if all4 else (h | v)


def two_regions(split):
    """A region image: red left of column `split`, blue right of it."""
    img = np.zeros((R, R, 3), np.uint8)
    img[:, :split] = (200, 60, 60)
    img[:, split:] = (40, 40, 190)
    return img


class GapTest(unittest.TestCase):
    def test_matches_the_scan_reference(self):
        rng = np.random.default_rng(0)
        for _ in range(100):
            h, w = rng.integers(1, 60, 2)
            mask = rng.random((h, w)) < rng.uniform(0.1, 0.95)
            gap = int(rng.integers(1, 45))
            for all4 in (False, True):
                np.testing.assert_array_equal(sb.fill_gaps(mask, gap, all4), fill_reference(mask, gap, all4))

    def test_open_notch_is_only_filled_without_all4(self):
        mask = np.zeros((7, 7), bool)
        mask[1:6, 1] = mask[1:6, 5] = mask[1, 1:6] = True  # a U open at the bottom
        self.assertTrue(sb.fill_gaps(mask, 5, False)[3, 3])
        self.assertFalse(sb.fill_gaps(mask, 5, True)[3, 3])
        mask[5, 1:6] = True
        self.assertTrue(sb.fill_gaps(mask, 5, True)[3, 3])


class DabTest(unittest.TestCase):
    def test_dab_stops_at_a_colour_edge(self):
        split = sb.MARGIN + sb.OUTER + 60  # 60 px right of the centre
        mask = sb.dab_mask(two_regions(split))
        self.assertEqual(mask.shape, (N, N))
        edge = split - sb.MARGIN  # the edge in window coordinates
        self.assertTrue(mask[sb.OUTER, sb.OUTER])
        self.assertFalse(mask[:, edge:].any())  # nothing beyond the edge
        # The blur mixes the colours near a strong edge, so the exact check stops about 1.5 sigma before it.
        self.assertTrue(mask[sb.OUTER, edge - int(2 * sb.BLUR_SIGMA)])

    def test_dab_stays_inside_the_outer_circle(self):
        mask = sb.dab_mask(np.full((R, R, 3), 128, np.uint8))
        yy, xx = np.mgrid[0:N, 0:N]
        self.assertFalse(mask[(xx - sb.OUTER) ** 2 + (yy - sb.OUTER) ** 2 > (sb.OUTER + 1) ** 2].any())
        self.assertGreater(mask.mean(), 0.7)  # a uniform image: (almost) the whole circle


class GeometryTest(unittest.TestCase):
    def test_mask_to_geometry_keeps_area_and_holes(self):
        mask = np.zeros((50, 60), bool)
        mask[5:45, 5:55] = True
        mask[20:30, 20:30] = False
        g = sb.mask_to_geometry(mask)
        self.assertEqual(g.area, mask.sum())
        self.assertEqual(len(g.interiors), 1)
        self.assertEqual(g.bounds, (5, 5, 55, 45))

    def test_stroke_geometry_is_in_level0_pixels(self):
        # A region whose left part (up to 60 screen px right of the first point) is red: at scale 0.5 (one screen
        # pixel = two level-0 pixels) the painted area must end about 120 level-0 pixels right of the point, less
        # the 1.5 sigma the blur costs at the edge.
        stroke = sb.Stroke("a.svs", "paint", 0.5, 1000.0, 2000.0)
        reader = lambda st, u, v: two_regions(sb.MARGIN + sb.OUTER + 60 - u)  # noqa: E731
        dabs = stroke.to_dab(reader, 1000.0, 2000.0)
        self.assertEqual(len(dabs), 1)
        self.assertAlmostEqual(dabs[0]["w"], N / 0.5)
        g = stroke.geometry()
        x0, y0, x1, y1 = g.bounds
        self.assertAlmostEqual(x0, 1000 - 2 * sb.OUTER, delta=2)
        self.assertAlmostEqual(x1, 1000 + 2 * (60 - 1.5 * sb.BLUR_SIGMA), delta=6)
        self.assertAlmostEqual(y1 - y0, 2 * N, delta=2)

    def test_smart_polygon_grows_from_inside_only(self):
        # Red left of grid column 60, blue right of it. The polygon's right side is 8 px from the blue, so dabs centred
        # on it have inner circles reaching into the blue; only seeds inside the polygon count, so no blue is painted.
        stroke = sb.Stroke("a.svs", "paint", 1.0, 0.0, -50.0)
        reader = lambda st, u, v: two_regions(sb.MARGIN + sb.OUTER + 60 - u)  # noqa: E731
        square = [(0, -50), (52, -50), (52, 50), (0, 50)]
        stroke.outline(square)
        self.assertEqual(len(stroke.todo), int(np.ceil(2 * (52 + 100) / sb.SPACING)))
        while stroke.todo:
            stroke.outline_dabs(reader)
        g = stroke.geometry()
        self.assertTrue(g.buffer(1e-6).contains(sb.shapely.Polygon(square)))
        x0, y0, x1, y1 = g.bounds
        self.assertLessEqual(x1, 60)
        self.assertLess(x0, -100)  # grown out over the red
        self.assertLess(y0, -150)

    def test_smart_polygon_without_dabs_is_the_polygon(self):
        stroke = sb.Stroke("a.svs", "paint", 2.0, 10.0, 20.0)
        stroke.outline([(10, 20), (30, 20), (30, 40)])
        self.assertAlmostEqual(stroke.geometry().area, 200)

    def test_dabs_follow_the_pointer(self):
        stroke = sb.Stroke("a.svs", "paint", 1.0, 0.0, 0.0)
        reader = lambda st, u, v: np.full((R, R, 3), 128, np.uint8)  # noqa: E731
        stroke.to_dab(reader, 0, 0)
        self.assertEqual(stroke.to_dab(reader, sb.SPACING - 1, 0), [])  # too close for a new dab
        self.assertEqual(len(stroke.to_dab(reader, 3 * sb.SPACING, 0)), 3)


class BrushAnnotationsTest(Base):
    def test_painted_areas_merge_with_touching_shapes_of_their_class(self):
        from shapely.geometry import box
        store = Annotations(self.project, "a.svs")
        other = self.project.add_class("Stroma", "#00ff00")
        first = store.add_area(box(0, 0, 10, 10), self.cls["id"])
        store.add_area(box(20, 0, 30, 10), self.cls["id"])           # apart: a shape of its own
        store.add_area(box(5, 0, 15, 10), other["id"])               # other class: not merged
        merged = store.add_area(box(9, 0, 21, 10), self.cls["id"])   # bridges the first two
        self.assertEqual(merged, first)
        tumor = [s for s in store.shapes if s.class_id == self.cls["id"]]
        self.assertEqual(len(tumor), 1)
        self.assertEqual(tumor[0].geometry.area, 300)
        self.assertEqual(len(store.shapes), 2)
        self.assertTrue(store.undo())
        self.assertEqual(len(store.shapes), 3)

    def test_erase_area_cuts_every_class(self):
        from shapely.geometry import box
        store = Annotations(self.project, "a.svs")
        other = self.project.add_class("Stroma", "#00ff00")
        store.add_area(box(0, 0, 10, 10), self.cls["id"])
        store.add_area(box(0, 0, 10, 10), other["id"])
        self.assertEqual(store.erase_area(box(0, 0, 5, 10)), 2)
        self.assertEqual([s.geometry.area for s in store.shapes], [50, 50])


if __name__ == "__main__":
    unittest.main()
