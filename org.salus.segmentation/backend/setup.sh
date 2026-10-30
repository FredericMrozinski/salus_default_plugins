#!/bin/sh
# Creates the virtualenv with OpenSlide, Pillow, Shapely, libvips and PyTorch (smart brush) (needs Python 3.10+) for the segmentation plugin.
cd "$(dirname "$0")" || exit 1
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
