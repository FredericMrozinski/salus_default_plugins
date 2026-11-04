"""The Swin-UNet edge model loads Lightning checkpoints of the training code (here: one made from random weights)."""
import sys
import tempfile
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import ViewerError  # noqa: E402
from models import swin_edges as se  # noqa: E402


class SwinEdgesTest(unittest.TestCase):
    def test_loads_a_lightning_checkpoint_and_predicts_probabilities(self):
        torch.manual_seed(0)
        net = se.build()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model.ckpt"
            # What Lightning saves: the module's state_dict (with the loss's pos_weight buffer) and more.
            torch.save({"state_dict": {**net.state_dict(), "pos_weight": torch.tensor(20.0)},
                        "hyper_parameters": {"lr": 1e-4}, "epoch": 49}, path)
            loaded = se.load(path, device="cpu")
        x = torch.rand(1, 3, 64, 64)
        with torch.inference_mode():
            self.assertTrue(torch.allclose(loaded(x), net.eval()(x)))
            p = torch.sigmoid(loaded(x))
        self.assertEqual(tuple(p.shape), (1, 1, 64, 64))
        self.assertTrue(float(p.min()) >= 0 and float(p.max()) <= 1)

    def test_a_missing_checkpoint_says_where_it_belongs(self):
        with self.assertRaises(ViewerError) as e:
            se.load(Path("/nonexistent/swin_edges.ckpt"), device="cpu")
        self.assertIn("/nonexistent/swin_edges.ckpt", str(e.exception))

    def test_is_registered_as_an_edge_mode(self):
        import modelbrush
        model = modelbrush.SEGMENTERS["swin_edges"]
        self.assertEqual((model.output, model.view), ("edges", 1024))


if __name__ == "__main__":
    unittest.main()
