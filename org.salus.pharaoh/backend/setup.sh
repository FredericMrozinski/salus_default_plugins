#!/bin/sh
# Creates the virtualenv with pyvips (and the libvips it bundles; needs Python 3.9+) for Pharaoh.
cd "$(dirname "$0")" || exit 1
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
