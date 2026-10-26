#!/bin/bash
cd "$(cd "$(dirname "$0")" && pwd)"
/bin/bash start_mac.sh "$@"
result=$?
if [[ $result -ne 0 ]]; then read -r -p 'Нажмите Enter…' _;fi
exit "$result"
