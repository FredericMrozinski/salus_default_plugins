#!/bin/sh
# Creates the virtualenv with psutil (needs Python 3.10+) for the system info plugin.
cd "$(dirname "$0")" || exit 1
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
