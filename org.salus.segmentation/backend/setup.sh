#!/bin/sh
# Creates the virtualenv with OpenSlide, Pillow, Shapely, libvips, PyTorch and numba (smart brushes) (needs Python 3.10+) for the segmentation plugin.
cd "$(dirname "$0")" || exit 1
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt || exit 1
# With a libvips on the system (Homebrew), installing pyvips compiles its binding against that libvips, overwriting the
# one pyvips-binary ships. Homebrew's libvips brings its own OpenMP, and two OpenMP libraries next to PyTorch's crash the
# backend (segfault in libomp) under heavy PyTorch work such as a segmentation model. Put the bundled binding back.
.venv/bin/pip install --force-reinstall --no-deps pyvips-binary
