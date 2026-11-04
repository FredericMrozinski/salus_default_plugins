#!/bin/sh
# Entrypoint. The backend uses only Python's standard library; restic comes from setup.sh (backend/bin).
cd "$(dirname "$0")" || exit 1
exec python3 plugin.py "$@"
