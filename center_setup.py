"""Local center setup; secrets are entered only in the owner's console."""
import argparse
import json
from getpass import getpass
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def configure(root=ROOT):
    path = Path(root) / '.env.authority'
    if path.exists():
        print('Настройки уже существуют: сохранены без изменений.')
        return
    token = getpass('Токен @asdvk2026_bot (ввод скрыт): ').strip()
    admin = input('Ваш числовой Telegram ID: ').strip()
    if ':' not in token or any(c.isspace() for c in token) or not admin.isdigit() or int(admin) <= 0:
        raise ValueError('Некорректный токен или Telegram ID')
    with path.open('x', encoding='utf-8') as stream:
        stream.write(f'ACCESS_AUTHORITY_BOT_TOKEN={token}\nACCESS_AUTHORITY_ADMIN_ID={admin}\n')
    if os.name != 'nt':
        path.chmod(0o600)
    print('Настройки сохранены локально. Не пересылайте .env.authority.')


def install(root=ROOT):
    root = Path(root)
    directory = root / 'venv-center'
    python = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not directory.exists():
        venv.EnvBuilder(with_pip=True).create(directory)
    if not python.is_file():
        raise RuntimeError('Окружение центра неполное. Проверьте установку Python.')
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(root / 'requirements-authority.txt')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
    return python


def configure_service(root=ROOT):
    from dotenv import dotenv_values, set_key
    import re
    path = Path(root) / '.env.authority'
    if not path.is_file():
        raise RuntimeError('Сначала настройте административного бота.')
    current = dotenv_values(path)
    username = input('Имя технического бота без @ [dvk_sync2026_bot]: ').strip().lstrip('@') or 'dvk_sync2026_bot'
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username) or not username.lower().endswith('bot'):
        raise ValueError('Некорректное имя технического бота.')
    token = getpass('Токен технического бота (ввод скрыт): ').strip()
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise ValueError('Некорректный токен технического бота.')
    if token == current.get('ACCESS_AUTHORITY_BOT_TOKEN'):
        raise ValueError('Технический бот должен отличаться от административного.')
    from file_lock import FileLock
    import uuid
    with FileLock(Path(root) / 'data/access_authority/center.lock'):
        backup = path.with_name('.env.authority.before-service-' + uuid.uuid4().hex)
        with backup.open('xb') as stream:
            stream.write(path.read_bytes())
        if os.name != 'nt':
            backup.chmod(0o600)
        try:
            set_key(str(path), 'ACCESS_SERVICE_BOT_TOKEN', token)
            set_key(str(path), 'ACCESS_SERVICE_BOT_USERNAME', username)
        except BaseException:
            path.write_bytes(backup.read_bytes())
            raise
    print('Технический бот настроен локально. Токен не отправляйте в чат. Центр пока не запускайте.')


def startup(enable):
    if os.name != 'nt':
        raise RuntimeError('Автозапуск этой командой доступен только в Windows')
    target = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup/DVK Center.cmd'
    content = '@echo off\nchcp 65001 >nul\ncall "' + str(ROOT / 'center.bat') + '" run\n'
    if target.exists() and target.read_text('utf-8') != content:
        raise RuntimeError('Автозапуск указывает на другую установку. Сначала отключите его там.')
    if enable:
        target.write_text(content, encoding='utf-8', newline='\r\n')
    elif target.exists():
        target.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'configure', 'run', 'check', 'autostart-on', 'autostart-off', 'backup-config', 'service-config', 'sheets-config'])
    parser.add_argument('--directory')
    parser.add_argument('--keep-days', type=int, default=30)
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == 'check':
        for name in ('access_server.py', 'authority_store.py', 'sync_protocol.py', 'requirements-authority.txt'):
            if not (ROOT / name).is_file():
                raise RuntimeError('Отсутствует ' + name)
        print('Файлы центра на месте. Сеть этим не проверена.')
    elif args.command == 'install':
        install()
        configure()
    elif args.command == 'configure':
        configure()
    elif args.command == 'service-config':
        python = ROOT / 'venv-center' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        return subprocess.call([str(python), '-c', 'import center_setup; center_setup.configure_service()'], cwd=ROOT)
    elif args.command == 'sheets-config':
        from sheets_directory_sync import configure as configure_sheets
        configure_sheets(ROOT / 'data/access_authority')
    elif args.command == 'backup-config':
        if not args.directory or not 1 <= args.keep_days <= 3650:
            raise ValueError('Укажите --directory и срок 1–3650 дней')
        directory = Path(args.directory).resolve()
        state = ROOT / 'data/access_authority'
        if directory.is_relative_to(state) or state.is_relative_to(directory):
            raise ValueError('Укажите отдельную папку для копий')
        state.mkdir(parents=True, exist_ok=True)
        (state / 'backup.json').write_text(json.dumps(dict(directory=str(directory), keep_days=args.keep_days)), encoding='utf-8')
        print('Ежедневное копирование включено; начнётся при следующем запуске центра.')
    elif args.command.startswith('autostart-'):
        startup(args.command.endswith('-on'))
    else:
        python = ROOT / 'venv-center' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file() or not (ROOT / '.env.authority').is_file():
            raise RuntimeError('Сначала выполните center.bat install')
        return subprocess.call([str(python), str(ROOT / 'access_server.py')], cwd=ROOT)
    return 0


if __name__ == '__main__':
    # Windows redirected output can default to cp1252 even for Russian users.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print('Ошибка настройки центра: ' + str(exc))
        raise SystemExit(1)
