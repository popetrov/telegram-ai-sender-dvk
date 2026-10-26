"""Offline security installation for verified 2.0.27/28. Never starts the sender."""
import base64
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

FILES = ('access_runtime.py', 'app_profile_client.py', 'bot.py', 'config.py',
         'dialogue_knowledge_transport.py', 'main.py', 'manual_update.py', 'sync_runtime.py',
         'telegram_app_migrate.py', 'telegram_client.py', 'transition_setup.py', 'version.txt',
         'safe_telegram.py', 'telegram_safety.py', 'account_guard.py')
MAINTENANCE = ("raise SystemExit('Установка защиты не завершена. Повторите файл установки защиты. "
               "Программа не запущена.')\n").encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()


def read(path, limit=8*1024*1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Отсутствует или небезопасен файл: ' + path.name)
    return path.read_bytes()


def atomic(path, raw):
    path = Path(path)
    temporary = path.with_name(path.name + '.security-tmp')
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError('Небезопасный файл установки')
    with temporary.open('wb') as output:
        output.write(raw);output.flush();os.fsync(output.fileno())
    os.replace(temporary, path)


class Lock:
    def __init__(self, path):
        self.path, self.stream = path, None

    def __enter__(self):
        self.path.parent.mkdir(exist_ok=True)
        if self.path.is_symlink():raise ValueError('Небезопасная блокировка')
        stream = self.path.open('a+b')
        try:
            if os.name=='nt':
                import msvcrt
                if not self.path.stat().st_size:stream.write(b'0');stream.flush()
                stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError('Программа ещё работает. Остановите её через Ctrl+C и повторите.') from None
        self.stream=stream
        return self

    def __exit__(self,*args):
        if self.stream:
            if os.name=='nt':
                import msvcrt
                self.stream.seek(0);msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
            self.stream.close()


def active_folder(root):
    pointer=root/'data/active-installation.json'
    if not pointer.exists():return root
    record=json.loads(read(pointer,4096));name=record.get('directory')
    if (record.get('format')!=1 or not isinstance(name,str) or Path(name).name!=name
            or '/' in name or '\\' in name or not name.startswith(root.name+'_dvk_')):
        raise ValueError('Не удалось безопасно определить рабочую папку')
    active=root.parent/name
    if active.is_symlink() or not active.is_dir() or active.resolve().parent!=root.parent:
        raise ValueError('Рабочая папка отсутствует или небезопасна')
    return active.resolve()


def user_data(root):
    # Hash only in memory, never place credentials or these digests in the report.
    result={}
    paths=[root/'.env']+list(root.glob('*.session'))
    data=root/'data'
    for folder,dirs,files in os.walk(data,followlinks=False):
        dirs[:]=[n for n in dirs if n not in ('backups','updates','security-backups')]
        for name in dirs:
            if (Path(folder)/name).is_symlink():raise ValueError('Небезопасная папка данных')
        for name in files:
            path=Path(folder)/name
            if (name=='active-installation.json' or name.endswith(('.sqlite','.sqlite3','.db','.session','-wal','-shm'))
                    or name in ('schedule.json','queue.json')):
                paths.append(path)
    for path in paths:
        if path.is_symlink():raise ValueError('Небезопасный пользовательский файл')
        if path.is_file():result[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def install(root, payload):
    root=Path(root).resolve()
    if payload.get('format')!=1 or set(payload['files'])!=set(FILES) or payload.get('version')!='2.0.29':
        raise ValueError('Неизвестный защитный пакет')
    files={name:base64.b64decode(value,validate=True) for name,value in payload['files'].items()}
    if files['version.txt'].decode('utf-8-sig').strip()!=payload['version']:
        raise ValueError('Некорректная версия пакета')
    for name,raw in files.items():
        if name.endswith('.py'):compile(raw,name,'exec')
    active=active_folder(root)
    if not (active/'.env').is_file():raise ValueError('Здесь нет настроенной программы. Положите файл рядом с привычным start.bat.')
    for folder in {root,active}:
        if (folder/'data').is_symlink():raise ValueError('Небезопасная папка данных')
    with ExitStack() as stack:
        for folder in sorted({root,active}):
            stack.enter_context(Lock(folder/'data/startup.lock'))
            stack.enter_context(Lock(folder/'data/sender.lock'))
        if active_folder(root)!=active:raise ValueError('Рабочая папка изменилась во время проверки')
        version=read(active/'version.txt',128).decode('utf-8-sig').strip()
        if version not in ('2.0.27','2.0.28','2.0.29'):
            raise ValueError('Это исправление проверено только для 2.0.27 и 2.0.28')
        pending=active/'data/security-install-pending.json'
        receipt_path=active/'data/security-patch.json'
        baseline_path=active/'data/installed-baseline.json'
        baseline=json.loads(read(baseline_path,16*1024*1024))
        if not isinstance(baseline,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in baseline.items()):
            raise ValueError('Некорректная запись установленной версии')
        old={}
        for name in FILES:
            path=active/name
            if path.exists() or path.is_symlink():
                raw=read(path)
                maintenance=name=='main.py' and raw==MAINTENANCE
                if (digest(raw)!=digest(files[name]) and not maintenance
                        and digest(raw) not in payload['accepted'].get(name,[])):
                    raise ValueError('Файл '+name+' отличается от проверенной версии. Ничего не перезаписано.')
                old[name]=raw
            elif payload['accepted'].get(name):
                raise ValueError('Отсутствует рабочий файл: '+name)
        before=user_data(active)
        if (all(old.get(name)==files[name] for name in FILES) and receipt_path.is_file() and not pending.exists()):
            return dict(status='already_installed',version=payload['version'],data_unchanged=True,sender_started=False)
        backup=active/'data/security-backups'/('before-'+str(time.time_ns()))
        if backup.parent.is_symlink():raise ValueError('Небезопасная папка резервной копии')
        backup.mkdir(parents=True)
        for name,raw in old.items():(backup/name).write_bytes(raw)
        (backup/'installed-baseline.json').write_bytes(read(baseline_path,16*1024*1024))
        # First stop entry-point execution. A crash at any later step must leave a stop, not mixed code.
        atomic(active/'main.py',MAINTENANCE)
        atomic(pending,json.dumps(dict(format=1,version=payload['version'])).encode())
        for name,raw in files.items():
            if name not in ('main.py','version.txt'):atomic(active/name,raw)
        for name,raw in files.items():baseline[name]=raw.decode('utf-8-sig')
        atomic(baseline_path,json.dumps(baseline,ensure_ascii=False).encode('utf-8'))
        atomic(active/'version.txt',files['version.txt'])
        # The new main itself also refuses startup while pending exists.
        atomic(active/'main.py',files['main.py'])
        if user_data(active)!=before:
            raise RuntimeError('Пользовательские файлы изменились во время установки. Запуск заблокирован, нужна проверка владельца.')
        receipt=dict(format=1,version=payload['version'],files={n:digest(raw) for n,raw in files.items()},
                     data_unchanged=True,sender_started=False,installed_at=int(time.time()))
        atomic(receipt_path,json.dumps(receipt,sort_keys=True).encode('utf-8'))
        pending.unlink()
        return dict(status='installed',version=payload['version'],data_unchanged=True,sender_started=False)


def main():
    root=Path(__file__).resolve().parent
    try:
        # Supplied by the single-file builder; never import or execute installed app code.
        payload=json.loads(base64.b64decode(SECURITY_PAYLOAD,validate=True))
        result=install(root,payload)
        text=('Защита установлена. Вход, настройки, база и очередь сохранены.\n'
              'Программа и рассылка не запускались. Передайте владельцу DVK_SECURITY_RESULT.txt.\n')
        (root/'DVK_SECURITY_RESULT.txt').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(text)
        return 0
    except Exception as exc:
        # Never print arbitrary exception text: filenames and fixed messages only.
        print('Установка защиты остановлена ('+type(exc).__name__+'). Программу не запускайте. Передайте владельцу это окно.')
        if isinstance(exc,(ValueError,RuntimeError)):print(str(exc))
        return 1


if __name__=='__main__':raise SystemExit(main())
