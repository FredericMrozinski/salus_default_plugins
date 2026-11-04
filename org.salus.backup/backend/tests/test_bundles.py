"""Tests against the real restic (skipped when restic is not installed: run setup.py)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import restic  # noqa: E402
from bundles import Bundles  # noqa: E402
from errors import BackupError  # noqa: E402

try:
    restic.binary()
    HAVE_RESTIC = True
except BackupError:
    HAVE_RESTIC = False

PW = "correct horse battery"


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@unittest.skipUnless(HAVE_RESTIC, "restic is not installed (run backend/setup.py)")
class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        os.environ["XDG_CACHE_HOME"] = str(self.dir / "cache")  # keep restic's cache out of the user's
        self.root = self.dir / "backups"
        self.data = self.dir / "data"
        write(self.data / "papers/a.txt", "alpha")
        write(self.data / "papers/sub/b.txt", "beta")
        write(self.data / "papers/skip.tmp", "temporary")
        write(self.data / "notes/n.md", "note")
        self.b = Bundles()
        self.bundle = self.b.create(self.root, "work", PW, [str(self.data / "papers"), str(self.data / "notes")], ["*.tmp"])

    def tearDown(self):
        self.tmp.cleanup()

    def wait(self, job):
        for t in list(__import__("threading").enumerate()):
            if t.name.startswith(job.kind + "-"):
                t.join(120)
        self.assertNotEqual(job.state, "running")
        return job

    def backup(self):
        return self.wait(self.b.backup(self.b.get(self.root, "work"), PW))

    def test_bundle_layout_and_listing(self):
        self.assertTrue((self.root / "work/repo/config").is_file())
        definition = json.loads((self.root / "work/bundle.json").read_text())
        self.assertEqual(definition["excludes"], ["*.tmp"])
        self.assertEqual([b.name for b in self.b.list(self.root)], ["work"])
        with self.assertRaises(BackupError):
            self.b.create(self.root, "work", PW, [str(self.data)], [])
        with self.assertRaises(BackupError):
            self.b.create(self.root, "../evil", PW, [str(self.data)], [])
        with self.assertRaises(BackupError):
            self.b.create(self.root, "short", "1234", [str(self.data)], [])

    def test_wrong_password(self):
        with self.assertRaises(BackupError) as e:
            self.b.unlock(self.bundle, "nope nope")
        self.assertEqual(e.exception.status, 401)
        self.b.unlock(self.bundle, PW)

    def test_backup_is_incremental_and_respects_excludes(self):
        job = self.backup()
        self.assertEqual(job.state, "done", job.message)
        self.assertEqual(job.summary.get("files_new"), 3)  # a, b, n — not skip.tmp
        write(self.data / "papers/c.txt", "gamma")
        second = self.backup()
        self.assertEqual((second.summary.get("files_new"), second.summary.get("files_unmodified")), (1, 3))
        snapshots = self.b.snapshots(self.bundle, PW)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(sorted(snapshots[0]["paths"]), sorted([str(self.data / "papers"), str(self.data / "notes")]))

    def test_browse(self):
        self.backup()
        snap = self.b.snapshots(self.bundle, PW)[0]["id"]
        papers = str(self.data / "papers")
        names = [e["name"] for e in self.b.list_dir(self.bundle, PW, snap, papers)]
        self.assertEqual(names, ["sub", "a.txt"])  # folders first; skip.tmp was excluded
        top = self.b.list_dir(self.bundle, PW, snap, "/")
        self.assertEqual(len(top), 1)  # the first folder of the absolute path

    def test_restore_elsewhere_with_and_without_the_folders_above(self):
        self.backup()
        snap = self.b.snapshots(self.bundle, PW)[0]["id"]
        target = self.dir / "restored"
        item = str(self.data / "papers/sub")
        self.wait(self.b.restore(self.bundle, PW, snap, [item], str(target), strip=True, overwrite=False))
        self.assertEqual((target / "sub/b.txt").read_text(), "beta")
        full = self.dir / "full"
        self.wait(self.b.restore(self.bundle, PW, snap, [item], str(full), strip=False, overwrite=False))
        self.assertEqual((full / str(self.data).lstrip("/") / "papers/sub/b.txt").read_text(), "beta")
        self.assertFalse((full / str(self.data).lstrip("/") / "papers/a.txt").exists())

    def test_restore_keep_or_replace_existing_files(self):
        self.backup()
        snap = self.b.snapshots(self.bundle, PW)[0]["id"]
        write(self.data / "papers/a.txt", "changed")
        (self.data / "notes/n.md").unlink()
        self.wait(self.b.restore(self.bundle, PW, snap, [str(self.data / "papers/a.txt"), str(self.data / "notes")], None, strip=False, overwrite=False))
        self.assertEqual((self.data / "papers/a.txt").read_text(), "changed")  # kept
        self.assertEqual((self.data / "notes/n.md").read_text(), "note")  # brought back
        self.wait(self.b.restore(self.bundle, PW, snap, [str(self.data / "papers/a.txt")], None, strip=False, overwrite=True))
        self.assertEqual((self.data / "papers/a.txt").read_text(), "alpha")  # replaced

    def test_cleanup_keeps_the_newest(self):
        self.backup()
        write(self.data / "papers/c.txt", "gamma")
        self.backup()
        job = self.wait(self.b.forget(self.bundle, PW, 1))
        self.assertEqual(job.state, "done", job.message)
        self.assertEqual(len(self.b.snapshots(self.bundle, PW)), 1)

    def test_check_and_missing_folders(self):
        self.backup()
        self.assertEqual(self.wait(self.b.check(self.bundle, PW)).state, "done")
        self.bundle.save_definition([str(self.data / "papers"), str(self.dir / "gone")], [])
        with self.assertRaises(BackupError):
            self.b.backup(self.b.get(self.root, "work"), PW)

    def test_stop_leaves_no_snapshot(self):
        with open(self.data / "papers/big.bin", "wb") as f:
            f.write(os.urandom(300 * 1024 * 1024))
        job = self.b.backup(self.b.get(self.root, "work"), PW)
        import time
        while not job.progress and job.state == "running":
            time.sleep(0.05)
        job.stop()
        self.wait(job)
        snapshots = self.b.snapshots(self.bundle, PW)
        # Either restic was interrupted (no snapshot) or it had finished just before (done, one snapshot).
        self.assertEqual((job.state, len(snapshots)) in (("stopped", 0), ("done", 1)), True, (job.state, len(snapshots)))
        self.b.unlock(self.bundle, PW)  # no stale lock left behind
        self.assertEqual(self.backup().state, "done")

    def test_a_copied_bundle_opens_elsewhere(self):
        self.backup()
        import shutil
        other_root = self.dir / "other-machine"
        shutil.copytree(self.root / "work", other_root / "work")
        moved = Bundles().get(other_root, "work")
        self.assertEqual(moved.folders, self.bundle.folders)
        self.assertEqual(len(Bundles().snapshots(moved, PW)), 1)

    def test_plain_restic_repository_is_a_bundle(self):
        restic.run(self.root / "plain", ["init"], PW)
        names = {b.name: b for b in self.b.list(self.root)}
        self.assertTrue(names["plain"].bare)
        self.b.unlock(names["plain"], PW)


if __name__ == "__main__":
    unittest.main()
