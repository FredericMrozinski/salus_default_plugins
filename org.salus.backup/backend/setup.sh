#!/bin/sh
# Downloads restic for this machine into backend/bin (see setup.py).
cd "$(dirname "$0")" || exit 1
exec python3 setup.py
