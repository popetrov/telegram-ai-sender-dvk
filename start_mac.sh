#!/bin/bash
set -euo pipefail

# Always run from the project folder, regardless of where this script is called.
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN=""
if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.11)"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "Python 3.11 or newer is required."
    echo "Install Python 3.11 from https://www.python.org/downloads/macos/"
    echo "Then run this script again."
    exit 1
fi

if [ ! -d "venv" ]; then
    echo "Creating the Python virtual environment..."
    "$PYTHON_BIN" -m venv venv
fi

VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11 or newer is required. Detected:"
    "$PYTHON_BIN" --version
    echo "Install Python 3.11 from https://www.python.org/downloads/macos/"
    exit 1
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Could not find Python in the virtual environment: $VENV_PYTHON"
    exit 1
fi

echo "Installing/checking Python dependencies..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r requirements.txt

if [ ! -f ".env" ]; then
    echo
    echo "Configuration file .env was not found."
    echo "Copy .env.example to .env, fill in the manager's own settings,"
    echo "then run this script again."
    exit 1
fi

echo
echo "Starting Telegram AI Sender..."
exec "$VENV_PYTHON" launcher.py
