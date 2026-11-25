#!/bin/sh
# Run from source using the PySide6 venv in ~/.venvs/xlttool (dev only).
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${XLTTOOL_PYTHON:-${PSPCONV_PYTHON:-$HOME/.venvs/xlttool/bin/python}}"
PYTHONPATH="$HERE:$HERE/dev" exec "$PY" -m xlttool "$@"
