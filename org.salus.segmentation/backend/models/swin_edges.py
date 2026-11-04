"""Swin-UNet edge model: a Swin-B encoder with a convolutional decoder (skip connections and a full-resolution CNN stem)
that predicts edge logits for a 1024 x 1024 view. A model brush with output="edges" (see modelbrush.py).

The network is rebuilt here from the training code (SwinUNetEdgeModel in edge_detection_model/model/edge_detection.py)
without Lightning, and the weights come from the Lightning checkpoint's state_dict. The input is RGB without
normalisation, as 0..1 (ToTensor()), as the model was trained.

The checkpoint goes to models/weights/swin_edges.ckpt (not in git), or set SEGMENTATION_SWIN_EDGES to its path. Runs on
CUDA, else MPS (Apple GPU), else the CPU; SEGMENTATION_DEVICE (cuda / mps / cpu) overrides that.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np

from errors import ViewerError
from modelbrush import register

WEIGHTS = Path(__file__).resolve().parent / "weights"
CHECKPOINT = Path(os.environ.get("SEGMENTATION_SWIN_EDGES") or WEIGHTS / "swin_edges.ckpt")  # trained on 0..1
VIEW = 1024  # the size the model was trained on (a multiple of 32)


def pick_device() -> str:
    import torch
    wanted = os.environ.get("SEGMENTATION_DEVICE")
    if wanted:
        return wanted
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def build():
    """The network of SwinUNetEdgeModel (training code), as a plain nn.Module."""
    import torch
    from torch import nn
    from torchvision import models

    def conv_block(c_in: int, c_out: int) -> nn.Sequential:
        return nn.Sequential(
            nn.Conv2d(c_in, c_out, 3, padding=1, bias=False), nn.GroupNorm(8, c_out), nn.GELU(),
            nn.Conv2d(c_out, c_out, 3, padding=1, bias=False), nn.GroupNorm(8, c_out), nn.GELU(),
        )

    class UpBlock(nn.Module):
        def __init__(self, c_in: int, c_skip: int, c_out: int):
            super().__init__()
            self.up = nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False)
            self.conv = conv_block(c_in + c_skip, c_out)

        def forward(self, x, skip):
            return self.conv(torch.cat([self.up(x), skip], dim=1))

    class SwinUNetEdges(nn.Module):
        STAGE_IDX = (1, 3, 5, 7)  # the swin.features layers that end a stage: 1/4, 1/8, 1/16, 1/32

        def __init__(self):
            super().__init__()
            self.encoder = models.swin_b(weights=None).features  # the weights come from the checkpoint
            self.stem1 = conv_block(3, 32)
            self.stem2 = nn.Sequential(nn.Conv2d(32, 64, 3, stride=2, padding=1, bias=False), nn.GroupNorm(8, 64),
                                       nn.GELU(), conv_block(64, 64))
            self.up16 = UpBlock(1024, 512, 256)
            self.up8 = UpBlock(256, 256, 128)
            self.up4 = UpBlock(128, 128, 64)
            self.up2 = UpBlock(64, 64, 32)
            self.up1 = UpBlock(32, 32, 32)
            self.out_conv = nn.Conv2d(32, 1, kernel_size=1)

        def forward(self, img):
            feats, x = [], img
            for i, layer in enumerate(self.encoder):
                x = layer(x)
                if i in self.STAGE_IDX:
                    feats.append(x.permute(0, 3, 1, 2).contiguous())
            f4, f8, f16, f32 = feats
            s1 = self.stem1(img)
            s2 = self.stem2(s1)
            x = self.up16(f32, f16)
            x = self.up8(x, f8)
            x = self.up4(x, f4)
            x = self.up2(x, s2)
            x = self.up1(x, s1)
            return self.out_conv(x)

    return SwinUNetEdges()


def load(checkpoint: Path = CHECKPOINT, device: str | None = None):
    import torch
    if not checkpoint.is_file():
        raise ViewerError(f"The Swin-UNet edge model's checkpoint is missing: put it at {checkpoint}.", 503)
    ckpt = torch.load(checkpoint, map_location="cpu", weights_only=False)  # a Lightning checkpoint (hyper-parameters)
    state = ckpt.get("state_dict", ckpt)
    state = {k: v for k, v in state.items() if k != "pos_weight"}  # a buffer of the training loss
    net = build()
    net.load_state_dict(state)
    return net.eval().to(device or pick_device())


def edge_model(checkpoint: Path, divisor: float):
    """Edge probabilities (VIEW x VIEW, 0..1) of an RGB view; the network gets RGB / divisor. Loaded on first use."""
    lock = threading.Lock()
    loaded = {}

    def edges(rgb: np.ndarray) -> np.ndarray:
        import torch
        with lock:
            if not loaded:
                loaded["device"] = pick_device()
                loaded["net"] = load(checkpoint, loaded["device"])
            x = torch.from_numpy(np.array(rgb)).permute(2, 0, 1)[None].float().div_(divisor).to(loaded["device"])  # a writable copy
            with torch.inference_mode():
                return torch.sigmoid(loaded["net"](x))[0, 0].float().cpu().numpy()

    return edges


ABOUT = ("Experimental: brush, eraser and polygon fill, inside their circle, what the centre reaches without crossing an "
         "edge found by the Swin-UNet edge model (the view around the brush is segmented first)")

edges = edge_model(CHECKPOINT, 255.0)
register("swin_edges", "Swin Edges", edges, output="edges", view=VIEW, about=ABOUT)
