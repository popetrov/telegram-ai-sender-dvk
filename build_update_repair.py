"""Build public one-time Windows update repair; contains no configuration or credentials."""
import hashlib
from pathlib import Path
import zipfile


def build(root,output):
    root,output=Path(root),Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    launcher=('@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\n'
              'py -3.11 --version >nul 2>&1\r\nif errorlevel 1 goto FALLBACK\r\n'
              'py -3.11 repair_update_local.py\r\ngoto END\r\n:FALLBACK\r\n'
              'python repair_update_local.py\r\n:END\r\npause\r\n')
    instructions=('1. Остановите программу.\n2. Распакуйте ZIP в отдельную папку.\n'
        '3. Откройте «Исправить обновление.cmd» и выберите привычную папку со start.bat.\n'
        '4. Дождитесь результата, затем запустите прежний start.bat.\n'
        'API-настройки, сессия, база и очередь не заменяются. Исправляется только обновлятор.\n'
        'Активная обновлённая папка определяется автоматически; прежний файл сохраняется для отката.\n')
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        for name in ('repair_update_local.py','startup_update.py','file_lock.py'):archive.write(root/name,name)
        archive.writestr('Исправить обновление.cmd',launcher.encode('utf-8'))
        archive.writestr('Прочитать.txt',instructions.encode('utf-8-sig'))
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest+'  '+output.name+'\n',encoding='ascii')
    return digest
