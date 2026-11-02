import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import texts  # noqa: E402
from errors import ViewerError  # noqa: E402


class TextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name, data: bytes) -> str:
        path = self.dir / name
        path.write_bytes(data)
        return str(path)

    def test_encodings(self):
        self.assertEqual(texts.read(self.file("a.geojson", '{"name": "Größe"}'.encode()))["text"], '{"name": "Größe"}')
        bom = texts.read(self.file("b.txt", b"\xef\xbb\xbfhi"))
        self.assertEqual((bom["text"], bom["encoding"]), ("hi", "UTF-8 (BOM)"))
        self.assertEqual(texts.read(self.file("c.txt", "hé".encode("utf-16")))["text"], "hé")
        old = texts.read(self.file("d.txt", "Größe".encode("cp1252")))
        self.assertEqual((old["text"], old["encoding"]), ("Größe", "Windows-1252"))

    def test_binary_and_missing_files_are_refused(self):
        with self.assertRaises(ViewerError):
            texts.read(self.file("e.txt", b"PK\x03\x04\x00\x00"))
        with self.assertRaises(ViewerError):
            texts.read(str(self.dir / "nope.txt"))

    def test_large_files_are_cut_off_between_characters(self):
        old, texts.MAX_BYTES = texts.MAX_BYTES, 5
        try:
            result = texts.read(self.file("f.txt", "abcdé and more".encode()))  # é is 2 bytes, at bytes 4..5
        finally:
            texts.MAX_BYTES = old
        self.assertEqual((result["text"], result["truncated"], result["encoding"]), ("abcd", True, "UTF-8"))


if __name__ == "__main__":
    unittest.main()
