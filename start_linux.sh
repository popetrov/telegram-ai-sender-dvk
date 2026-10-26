#!/bin/bash
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"
exec /bin/bash start_mac.sh "$@"
