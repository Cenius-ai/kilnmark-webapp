#!/usr/bin/env bash
# Kilnmark install: project-level dependencies only, then verify and EXIT.
# Never touches system packages and never starts a long-running process.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON was not found on PATH (Python 3.9+ is required)" >&2
    exit 1
fi

echo "python: $("$PYTHON" --version 2>&1)"

if "$PYTHON" -c 'import pytest' >/dev/null 2>&1; then
    echo "test runner: pytest $("$PYTHON" -c 'import pytest; print(pytest.__version__)') (already available)"
else
    echo "installing project dependencies from requirements.txt"
    "$PYTHON" -m pip install --upgrade pip setuptools wheel
    "$PYTHON" -m pip install -r requirements.txt
fi

echo "checking that kilnmark imports"
"$PYTHON" -c 'import kilnmark; print("kilnmark", kilnmark.VERSION, "imports cleanly")'
"$PYTHON" kilnmark.py --version

echo
echo "Setup complete. Kilnmark needs no database, no server and no network."
echo "  run:   python3 kilnmark.py --help"
echo "  demo:  sh demo.sh"
echo "  test:  python3 -m pytest"
