#!/bin/sh
# Entrypoint: runs the backend with the plugin's own virtualenv (see setup.sh).
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "File viewer backend: virtualenv missing. Run backend/setup.sh first." >&2
    exit 1
fi
exec .venv/bin/python plugin.py "$@"
