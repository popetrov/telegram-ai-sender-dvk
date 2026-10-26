#!/bin/bash
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"
if [[ -f data/python-path.txt ]]; then
    IFS= read -r DVK_PYTHON < data/python-path.txt || true
    if [[ -n "${DVK_PYTHON:-}" && -x "$DVK_PYTHON" ]]; then
        exec "$DVK_PYTHON" start.py "$@"
    fi
fi
if command -v python3.11 >/dev/null 2>&1; then
    exec python3.11 start.py "$@"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 start.py "$@"
else
    echo "Python 3.11 or newer is required." >&2
    exit 1
fi
