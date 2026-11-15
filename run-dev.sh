#!/bin/sh
# Run from source using the PySide6 venv from ~/PSvid (dev only).
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${XLTTOOL_PYTHON:-${PSPCONV_PYTHON:-$HOME/PSvid/.venv/bin/python}}"
PYTHONPATH="$HERE:$HERE/dev" exec "$PY" -m xlttool "$@"
