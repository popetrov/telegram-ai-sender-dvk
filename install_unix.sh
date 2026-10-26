#!/bin/bash
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"
PY=''
find_python() {
  local candidate
  for candidate in python3.11 python3.12 python3.13 python3.14 python3 /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 "$HOME/.local/dvk-python/bin/python3.14"; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys,struct; raise SystemExit(0 if (3,11)<=sys.version_info[:2]<(3,15) and struct.calcsize("P")==8 else 1)' 2>/dev/null; then
      PY="$("$candidate" -c 'import sys; print(sys.executable)')";return 0
    fi
  done
  return 1
}
if [[ "${1:-}" == '--check-only' ]]; then
  if find_python; then echo 'Suitable Python found'; else echo 'Python will be installed on first setup'; fi
  exit 0
fi
system="$(uname -s)"
if [[ "${DVK_FORCE_PYTHON_INSTALL:-0}" == 1 ]] || ! find_python; then
  echo 'Подходящий Python не найден. Устанавливаю Python…'
  if [[ "$system" == Darwin ]]; then
    tempdir="$(mktemp -d -t dvk-python)"
    pkg="$tempdir/python.pkg"
    curl --fail --location --connect-timeout 15 --max-time 600 --proto '=https' --tlsv1.2 'https://www.python.org/ftp/python/3.14.10/python-3.14.10-macos11.pkg' -o "$pkg"
    echo '286c7018aa2730f6747ca433dccdc2b31be45f078a9ea0ffa44d5f06368fd2d7  '"$pkg" | shasum -a 256 -c -
    /usr/sbin/pkgutil --check-signature "$pkg" | grep -F 'Python Software Foundation (BMM5U3QVKW)' >/dev/null
    echo 'Для установки Python macOS запросит пароль администратора компьютера.'
    sudo /usr/sbin/installer -pkg "$pkg" -target /
    if [[ -f '/Applications/Python 3.14/Install Certificates.command' ]]; then
      /bin/bash '/Applications/Python 3.14/Install Certificates.command'
    fi
  elif [[ "$system" == Linux ]]; then
    if command -v apt-get >/dev/null; then
      sudo apt-get update
      sudo apt-get install -y python3 python3-venv ca-certificates curl
    elif command -v dnf >/dev/null; then
      sudo dnf install -y python3 python3-pip ca-certificates curl
    elif command -v pacman >/dev/null; then
      sudo pacman -S --needed python python-pip ca-certificates curl
    else
      echo 'Этот дистрибутив не поддерживает автоматическую установку. Установите Python 3.11–3.14 с venv, затем запустите установку снова.' >&2;exit 1
    fi
    if ! find_python; then
      # Older supported distributions may provide Python <3.11. Build into the
      # user's directory, never replace the distribution's /usr/bin/python.
      if command -v apt-get >/dev/null; then
        sudo apt-get install -y build-essential libssl-dev zlib1g-dev libffi-dev libbz2-dev libreadline-dev libsqlite3-dev liblzma-dev tk-dev
      elif command -v dnf >/dev/null; then
        sudo dnf install -y gcc make openssl-devel zlib-devel libffi-devel bzip2-devel readline-devel sqlite-devel xz-devel tk-devel
      elif command -v pacman >/dev/null; then
        sudo pacman -S --needed base-devel openssl zlib libffi bzip2 readline sqlite xz
      else
        echo 'Требуется Python 3.11–3.14. Обратитесь к руководителю.' >&2;exit 1
      fi
      tempdir="$(mktemp -d -t dvk-python.XXXXXXXX)";archive="$tempdir/Python.tgz"
      curl --fail --location --connect-timeout 15 --max-time 600 --proto '=https' --tlsv1.2 'https://www.python.org/ftp/python/3.14.10/Python-3.14.10.tgz' -o "$archive"
      echo '9e4996c19ae5a3df31f10c37723661f71d3455d73bd005f79df7098e5cc13192  '"$archive" | sha256sum -c -
      tar -xzf "$archive" -C "$tempdir"
      (cd "$tempdir/Python-3.14.10" && ./configure --prefix="$HOME/.local/dvk-python" --with-ensurepip=install && make -j2 && make altinstall)
    fi
  else echo 'Поддерживаются macOS и Linux.' >&2;exit 1
  fi
  find_python || { echo 'Python не удалось установить.' >&2;exit 1; }
  if [[ "$system" == Darwin && "${DVK_FORCE_PYTHON_INSTALL:-0}" == 1 ]]; then
    PY='/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14'
  fi
fi
if ! "$PY" -c 'import venv; import ensurepip' 2>/dev/null; then
  if [[ "$system" == Linux ]] && command -v apt-get >/dev/null; then
    sudo apt-get update;sudo apt-get install -y python3-venv
  elif [[ "$system" == Linux ]] && command -v dnf >/dev/null; then
    sudo dnf install -y python3-pip
  fi
  "$PY" -c 'import venv; import ensurepip' || { echo 'Не хватает компонентов Python. Передайте руководителю название ошибки.' >&2;exit 1; }
fi
if [[ "${DVK_PYTHON_PROBE_ONLY:-0}" == 1 ]]; then
  "$PY" -c 'import sys,venv,ssl; print("Python bootstrap verified:",sys.version.split()[0])'
  exit 0
fi
mkdir -p data
printf '%s\n' "$PY" > data/python-path.txt
echo 'Python готов. Устанавливаю программу и открываю настройку…'
exec "$PY" install_manager.py
