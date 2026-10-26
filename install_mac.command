#!/bin/bash
cd "$(cd "$(dirname "$0")" && pwd)"
/bin/bash install_unix.sh
result=$?
if [[ $result -ne 0 ]]; then echo 'Установка остановлена. Сфотографируйте ошибку и передайте руководителю.';read -r -p 'Нажмите Enter…' _;fi
exit "$result"
