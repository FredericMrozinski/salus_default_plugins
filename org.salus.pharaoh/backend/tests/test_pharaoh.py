import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pyvips  # noqa: E402

from errors import PharaohError  # noqa: E402
from pharaoh import Pharaoh  # noqa: E402


def make_slide(path: Path, size=(3000, 2000), pyramid=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    noise = [pyvips.Image.gaussnoise(*size, sigma=30, mean=m) for m in (150, 120, 110)]
    image = noise[0].bandjoin(noise[1:]).copy(interpretation="srgb").cast("uchar")
    image.tiffsave(str(path), tile=True, tile_width=256, tile_height=256, compression="none", pyramid=pyramid, bigtiff=True)


def wait_idle(p: Pharaoh, timeout=60):
    end = time.time() + timeout
    while p.run_state == "running" and time.time() < end:
        time.sleep(0.05)
    assert p.run_state != "running"


def wait_inspected(p: Pharaoh):
    p.pool.shutdown(wait=True)


class PharaohTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        make_slide(self.dir / "cohort/a.tif")
        make_slide(self.dir / "cohort/deep/b.tiff", pyramid=True)
        (self.dir / "cohort/notes.txt").write_text("x")
        self.p = Pharaoh()

    def tearDown(self):
        self.tmp.cleanup()

    def add_all(self):
        self.p.add(str(self.dir / "cohort"))
        wait_inspected(self.p)

    def test_folder_is_indexed_recursively_and_inspected(self):
        self.assertEqual(self.p.add(str(self.dir / "cohort"))["added"], 2)
        self.assertEqual(self.p.add(str(self.dir / "cohort"))["added"], 0)
        wait_inspected(self.p)
        levels = {i["name"]: i["info"]["levels"] for i in self.p.state()["items"]}
        self.assertEqual(levels["a.tif"], 1)
        self.assertGreater(levels["b.tiff"], 1)

    def test_single_file_and_missing_path(self):
        self.p.add(str(self.dir / "cohort/notes.txt"))
        wait_inspected(self.p)
        self.assertIn("cannot be read", self.p.state()["items"][0]["error"])
        with self.assertRaises(PharaohError):
            self.p.add(str(self.dir / "nope"))
        with self.assertRaises(PharaohError):
            self.p.add("relative/path")

    def test_remove(self):
        self.add_all()
        first = self.p.state()["items"][0]["id"]
        self.p.remove([first])
        self.assertEqual(len(self.p.items), 1)

    def test_beside_needs_a_prefix_or_suffix(self):
        with self.assertRaises(PharaohError):
            self.p.set_plan(None, {"mode": "beside", "prefix": "", "suffix": ""})
        with self.assertRaises(PharaohError):
            self.p.set_plan(None, {"mode": "beside", "suffix": "a/b"})
        with self.assertRaises(PharaohError):
            self.p.set_plan({"quality": 0}, None)
        with self.assertRaises(PharaohError):
            self.p.set_plan(None, {"mode": "folder", "folder": "relative"})

    def test_run_beside_writes_new_files_with_a_pyramid(self):
        self.add_all()
        self.p.set_plan({"compression": "jpeg", "quality": 50}, {"mode": "beside", "suffix": "_small"})
        self.p.start()
        wait_idle(self.p)
        self.assertEqual(self.p.run_state, "finished")
        out = self.dir / "cohort/a_small.tif"
        self.assertTrue(out.exists())
        self.assertLess(out.stat().st_size, (self.dir / "cohort/a.tif").stat().st_size)
        self.assertGreater(pyvips.Image.new_from_file(str(out)).get("n-pages"), 1)
        self.assertTrue((self.dir / "cohort/deep/b_small.tiff").exists())
        self.assertEqual({i["status"] for i in self.p.state()["items"]}, {"done"})
        self.assertFalse(list(self.dir.rglob("*.pharaoh-part")))

    def test_overwrite_replaces_the_file_and_keeps_larger_results_out(self):
        self.add_all()
        original = (self.dir / "cohort/a.tif").stat().st_size
        self.p.set_plan({"compression": "jpeg", "quality": 40}, {"mode": "overwrite"})
        self.p.start()
        wait_idle(self.p)
        self.assertLess((self.dir / "cohort/a.tif").stat().st_size, original)
        # Uncompressed output is larger than the JPEG file now: the original stays.
        self.p.reset()
        size = (self.dir / "cohort/a.tif").stat().st_size
        self.p.set_plan({"compression": "none"}, {"mode": "overwrite", "keep_if_larger": True})
        self.p.start()
        wait_idle(self.p)
        self.assertEqual((self.dir / "cohort/a.tif").stat().st_size, size)
        self.assertIn("original was kept", next(i for i in self.p.state()["items"] if i["name"] == "a.tif")["message"])

    def test_folder_mode_keeps_the_structure(self):
        self.add_all()
        out = self.dir / "out"
        self.p.set_plan({"pyramid": False}, {"mode": "folder", "folder": str(out)})
        self.p.start()
        wait_idle(self.p)
        self.assertTrue((out / "a.tif").exists())
        self.assertTrue((out / "deep/b.tiff").exists())

    def test_existing_targets_are_skipped_unless_replacing(self):
        self.add_all()
        (self.dir / "cohort/a_x.tif").write_text("old")
        self.p.set_plan(None, {"mode": "beside", "suffix": "_x"})
        rows = {r["name"]: r for r in self.p.preview()}
        self.assertEqual(rows["a.tif"]["action"], "skip")
        self.assertEqual(rows["b.tiff"]["action"], "write")
        self.p.set_plan(None, {"mode": "beside", "suffix": "_x", "replace_existing": True})
        self.assertEqual({r["name"]: r for r in self.p.preview()}["a.tif"]["action"], "write")

    def test_skip_pyramidal(self):
        self.add_all()
        self.p.set_plan({"skip_pyramidal": True}, {"mode": "beside", "suffix": "_y"})
        rows = {r["name"]: r for r in self.p.preview()}
        self.assertEqual(rows["b.tiff"]["action"], "skip")
        self.assertEqual(rows["a.tif"]["action"], "write")

    def test_abort_stops_and_leaves_no_partial_files(self):
        make_slide(self.dir / "big/c.tif", size=(16000, 12000))
        self.p.add(str(self.dir / "big"))
        self.add_all()
        wait_inspected(self.p)
        self.p.set_plan({"compression": "deflate"}, {"mode": "beside", "suffix": "_z"})
        self.p.start()
        end = time.time() + 30
        while not self.p.current and time.time() < end:
            time.sleep(0.01)
        self.p.abort()
        wait_idle(self.p)
        self.assertEqual(self.p.run_state, "aborted")
        self.assertFalse(list(self.dir.rglob("*.pharaoh-part")))
        self.assertFalse(list(self.dir.rglob("*_z.*")))
        self.assertTrue(all(i["status"] in ("aborted", "done") for i in self.p.state()["items"]))

    def test_cannot_change_things_during_a_run(self):
        make_slide(self.dir / "big/c.tif", size=(16000, 12000))
        self.p.add(str(self.dir / "big"))
        wait_inspected(self.p)
        self.p.set_plan(None, {"mode": "beside", "suffix": "_w"})
        self.p.start()
        with self.assertRaises(PharaohError):
            self.p.clear()
        self.p.abort()
        wait_idle(self.p)


if __name__ == "__main__":
    unittest.main()
