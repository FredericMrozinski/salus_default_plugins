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


class DiskStroke(sb.Stroke):
    """A smart stroke whose dabs paint their inner circle (cut by `keep_seeds`): the stroke machinery without a smart
    brush (Border growth and the model brushes are tested in their own files)."""

    def _dab(self, read_region, pu, pv, keep_seeds=None):
        read_region(self, pu, pv)
        o = sb.OUTER
        yy, xx = np.mgrid[0:N, 0:N]
        mask = (xx - o) ** 2 + (yy - o) ** 2 <= self.inner ** 2
        if keep_seeds is not None:
            rows, cols = np.nonzero(mask)
            mask[rows[~keep_seeds(rows, cols)], cols[~keep_seeds(rows, cols)]] = False
        u0, v0 = pu - o, pv - o
        self.dabs.append((u0, v0, mask))
        return sb.dab_reply(self, u0, v0, mask)


def grey(stroke, u, v):
    return np.full((R, R, 3), 128, np.uint8)


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


class StrokeTest(unittest.TestCase):
    def test_inner_radius_sets_the_spacing(self):
        self.assertEqual(sb.Stroke("a.svs", "paint", 1.0, 0, 0, inner=40).spacing, 40)
        self.assertEqual(sb.Stroke("a.svs", "paint", 1.0, 0, 0, inner=1).spacing, sb.MIN_SPACING)
        self.assertEqual(sb.check_inner(None), sb.INNER)
        self.assertEqual(sb.check_inner(10_000), sb.MAX_INNER)

    def test_a_plain_stroke_has_no_dab_of_its_own(self):
        with self.assertRaises(NotImplementedError):
            sb.Stroke("a.svs", "paint", 1.0, 0, 0).to_dab(grey, 0, 0)


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
        # At scale 0.5 (one screen pixel = two level-0 pixels) a dab's inner circle of 16 screen px is 32 level-0 px.
        stroke = DiskStroke("a.svs", "paint", 0.5, 1000.0, 2000.0, inner=16)
        dabs = stroke.to_dab(grey, 1000.0, 2000.0)
        self.assertEqual(len(dabs), 1)
        g = stroke.geometry()
        x0, y0, x1, y1 = g.bounds
        # The reply is cut to the painted pixels: it covers exactly the stroke's area.
        self.assertEqual((dabs[0]["x"], dabs[0]["y"]), (x0, y0))
        self.assertAlmostEqual(dabs[0]["w"], x1 - x0, delta=1)
        self.assertAlmostEqual(dabs[0]["h"], y1 - y0, delta=1)
        self.assertLess(dabs[0]["w"], N / 0.5)
        for low, high, centre in ((x0, x1, 1000), (y0, y1, 2000)):
            self.assertAlmostEqual(low, centre - 32, delta=2)
            self.assertAlmostEqual(high, centre + 34, delta=2)  # the centre pixel covers [centre, centre + 2)

    def test_smart_polygon_dabs_run_along_the_outline_seeded_inside(self):
        stroke = DiskStroke("a.svs", "paint", 1.0, 0.0, -50.0)
        square = [(0, -50), (52, -50), (52, 50), (0, 50)]
        stroke.outline(square)
        self.assertEqual(len(stroke.todo), int(np.ceil(2 * (52 + 100) / sb.SPACING)))
        while stroke.todo:
            stroke.outline_dabs(grey)
        g = stroke.geometry()
        self.assertTrue(g.buffer(1e-6).contains(sb.shapely.Polygon(square)))
        # the disk dabs paint only where they may seed (inside the polygon): the result is the polygon
        self.assertAlmostEqual(g.area, 52 * 100, delta=52 + 100)

    def test_smart_polygon_without_dabs_is_the_polygon(self):
        stroke = sb.Stroke("a.svs", "paint", 2.0, 10.0, 20.0)
        stroke.outline([(10, 20), (30, 20), (30, 40)])
        self.assertAlmostEqual(stroke.geometry().area, 200)

    def test_dabs_follow_the_pointer(self):
        stroke = DiskStroke("a.svs", "paint", 1.0, 0.0, 0.0)
        stroke.to_dab(grey, 0, 0)
        self.assertEqual(stroke.to_dab(grey, sb.SPACING - 1, 0), [])  # too close for a new dab
        self.assertEqual(len(stroke.to_dab(grey, 3 * sb.SPACING, 0)), 3)


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
        self.assertEqual(store.summary()["classes"], {str(self.cls["id"]): 1, str(other["id"]): 1})
        self.assertEqual(store.erase_area(box(5, 0, 7, 10), skip={other["id"]}), 1)  # hidden classes are left alone
        self.assertEqual(sorted(s.geometry.area for s in store.shapes), [30, 50])


if __name__ == "__main__":
    unittest.main()
