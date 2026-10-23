#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "$#" -eq 0 ]; then set -- run; fi
exec python3 center_setup.py "$@"
