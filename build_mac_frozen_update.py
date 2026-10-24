"""Build a single-file Mac activator from an existing official release archive."""
import argparse
import base64
import hashlib
from pathlib import Path
import zipfile
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parent
NAME='Обновить ДВК.command'


def build(bridge, output):
    raw=Path(bridge).read_bytes()
    header='''#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
if [ -x "$ROOT/venv/bin/python" ]; then
  PYTHON="$ROOT/venv/bin/python"
elif command -v python3.11 >/dev/null 2>&1; then
  PYTHON="$(command -v python3.11)"
else
  PYTHON="$(command -v python3)"
fi
"$PYTHON" - "$ROOT" "$@" <<'DVK_MAC_UPDATE_PYTHON'
'''
    source=(ROOT/'mac_frozen_update.py').read_text('utf-8-sig').replace('\r\n','\n')
    source += '\nimport base64\nraise SystemExit(command_main(base64.b85decode('+repr(base64.b85encode(raw))+'), '+repr(hashlib.sha256(raw).hexdigest())+'))\n'
    script=(header+source+'DVK_MAC_UPDATE_PYTHON\n').encode('utf-8')
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    guide=('Обновление отдельной Mac-сборки 2.0.19 до 2.0.20\n\n'
           '1. Закройте программу рассылки и её окно Terminal.\n'
           '2. Перенесите «Обновить ДВК.command» в установленную папку программы, рядом со start_mac.sh.\n'
           '3. Откройте этот файл двойным щелчком. Если macOS не открывает файл, сообщите владельцу текст предупреждения.\n'
           '4. Дождитесь окончания установки. Программа запустится, владелец выдаст доступ через центр.\n\n'
           'В дальнейшем запускайте привычный start_mac.sh. Новые обновления будут проверяться автоматически.\n'
           'Сохраняются настройки, Telegram-сессия, номер менеджера, база и продукты.\n'
           'Создаётся отдельная рабочая копия и резервная копия. Не удаляйте их.\n'
           'Обновление не запускает сохранённую очередь рассылки автоматически. Нужны интернет и Python 3.11+.\n')
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        info=zipfile.ZipInfo(NAME);info.create_system=3;info.external_attr=0o100755<<16;info.compress_type=zipfile.ZIP_DEFLATED
        archive.writestr(info,script)
        archive.writestr('Прочитать.txt',guide)
    output.with_suffix('.zip.sha256').write_text(hashlib.sha256(output.read_bytes()).hexdigest()+'  '+output.name+'\n',encoding='ascii')
    return output


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--bridge',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.bridge:
        print(build(args.bridge,args.output))
    else:
        # Use exactly the published 2.0.20 bytes, never rebuild or replace stable runtime assets.
        name='telegram_ai_sender_v2.0.20.zip'
        base='https://github.com/popetrov/telegram-ai-sender-dvk/releases/download/v2.0.20/'
        def download(name):
            request=urllib.request.Request(base+name,headers={'User-Agent':'DVK-Mac-Activation'})
            with urllib.request.urlopen(request,timeout=45) as response:
                data=response.read(64*1024*1024+1)
            if len(data)>64*1024*1024: raise ValueError('Release asset too large')
            return data
        raw=download(name)
        digest=download(name+'.sha256').decode('ascii').split()[0]
        if hashlib.sha256(raw).hexdigest()!=digest: raise ValueError('Official release checksum mismatch')
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/name;path.write_bytes(raw)
            print(build(path,args.output))
