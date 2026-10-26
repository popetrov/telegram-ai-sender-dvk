"""One-time Windows updater repair. Never starts the sender or changes credentials."""
from contextlib import ExitStack
import hashlib
import ast
import json
from pathlib import Path
import os
import sys
import uuid
from file_lock import FileLock
import startup_update

HERE=Path(__file__).resolve().parent
# Exact normalized source shared by the published 2.0.20 and 2.0.21.
OLD_HASH='315391821d1518e45939e5e572b1eadc1378a0a7af4eb2d16db836bb73319e88'
BRIDGE_AST='09485b3c30ab442d15fd07fb7f68a65b10608a44ee31d968cddd4a6340bc62ed'
BRIDGE_REPAIRED='''"""DVK repaired bridge: use the active updater, never a frozen embedded copy."""
import json,pathlib,re,subprocess,sys
def main():
    root=pathlib.Path(__file__).resolve().parent
    record=json.loads((root/'data/active-installation.json').read_text('utf-8'))
    name=record.get('directory','')
    if record.get('format')!=1 or not isinstance(name,str) or not re.fullmatch(re.escape(root.name)+r'_dvk_[0-9a-f]{32}',name):
        raise ValueError('Invalid active installation')
    active=root.parent/name
    if active.is_symlink() or not active.is_dir() or active.resolve().parent!=root.parent:
        raise ValueError('Active installation unavailable')
    sys.path.insert(0,str(active))
    from startup_update import run
    if sys.argv[1:]==['--self-check']:
        def check(folder):
            python=folder/'venv'/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
            return subprocess.run([str(python),str(folder/'main.py'),'--self-check'],cwd=folder).returncode
        return run(root,updates=False,launch=check)
    if sys.argv[1:]:raise ValueError('Unsupported startup arguments')
    return run(root)
if __name__=='__main__':
    raise SystemExit(main())
'''.encode('utf-8')


def digest(data):return hashlib.sha256(data.replace(b'\r\n',b'\n')).hexdigest()


def replace(path,data):
    temp=path.with_name('.'+path.name+'.repair.tmp');temp.write_bytes(data);os.replace(temp,path)


def known_bridge(data):
    if data==BRIDGE_REPAIRED:return True
    try:
        tree=ast.parse(data.decode('utf-8-sig'))
        for node in tree.body:
            if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
                if node.targets[0].id=='PAYLOAD':node.value=ast.Constant(b'placeholder')
                if node.targets[0].id=='SHA256':node.value=ast.Constant('placeholder')
        return hashlib.sha256(ast.dump(tree,include_attributes=False).encode()).hexdigest()==BRIDGE_AST
    except (ValueError,SyntaxError,UnicodeError):return False


def repair(root):
    root=Path(root).resolve();active=startup_update.active_installation(root)
    targets=list(dict.fromkeys([root,active]))
    new=(HERE/'startup_update.py').read_bytes();compile(new,'startup_update.py','exec')
    originals={};replacements={};marker='updater-repair-'+uuid.uuid4().hex
    with ExitStack() as locks:
        for target in targets:
            if target.is_symlink() or (target/'data').is_symlink() or (target/'.code-backups').is_symlink():raise ValueError('Небезопасная папка программы.')
            locks.enter_context(FileLock(target/'data/startup.lock'))
            locks.enter_context(FileLock(target/'data/sender.lock'))
        for target in targets:
            path=target/'startup_update.py'
            if path.is_symlink():raise ValueError('Небезопасный путь обновлятора.')
            if path.is_file():
                old=path.read_bytes()
                if digest(old) not in {OLD_HASH,digest(new)}:raise ValueError('Обновлятор изменён вручную. Автоматическая замена отложена.')
                originals[path]=old;replacements[path]=new
            elif target!=root or active==root:raise ValueError('В выбранной папке не найден обновлятор.')
            if target==root and active!=root:
                bridge=root/'launcher.py'
                if bridge.is_file() and known_bridge(bridge.read_bytes()):
                    for name in ('launcher.py','main.py'):
                        entry=root/name
                        if entry.is_symlink() or not entry.is_file() or not known_bridge(entry.read_bytes()):
                            raise ValueError('Не подтверждён исходный переходный запуск.')
                        originals[entry]=entry.read_bytes();replacements[entry]=BRIDGE_REPAIRED
                elif not path.exists():raise ValueError('Не найден привычный переходный запуск.')
            baseline=target/'data/installed-baseline.json'
            if baseline.is_symlink():raise ValueError('Небезопасный файл версии.')
            if baseline.exists():originals[baseline]=baseline.read_bytes()
        for target in targets:
            backup=target/'.code-backups'/marker;backup.mkdir(parents=True)
            for path,data in originals.items():
                if path.is_relative_to(target):
                    out=backup/path.relative_to(target);out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(data)
        try:
            for path,data in replacements.items():replace(path,data)
            for target in targets:
                baseline=target/'data/installed-baseline.json'
                if baseline in originals:
                    values=json.loads(originals[baseline]);values.update({path.name:data.decode('utf-8-sig')
                        for path,data in replacements.items() if path.parent==target})
                    replace(baseline,json.dumps(values,ensure_ascii=False).encode('utf-8'))
        except BaseException:
            for path,data in originals.items():replace(path,data)
            raise
    return active


def main():
    from tkinter import Tk,filedialog
    ui=Tk();ui.withdraw();name=filedialog.askdirectory(title='Выберите привычную папку программы, где лежит start.bat');ui.destroy()
    if not name:print('Отменено. Настройки не изменены.');return
    print('Исправляю получение обновлений. База, API-настройки и сессия не меняются.',flush=True)
    active=repair(Path(name))
    print('Исправление установлено. Проверяю загрузку официального обновления…',flush=True)
    try:
        found=startup_update.latest_bundle((active/'version.txt').read_text('utf-8-sig').strip())
        print('Архив обновления '+found[0]+' получен и проверен.' if found else 'Установлена актуальная версия.',flush=True)
    except Exception as exc:
        import urllib.error
        message=('HTTP '+str(exc.code)) if isinstance(exc,urllib.error.HTTPError) else type(exc).__name__
        print('Сеть пока не дала скачать архив: '+message+'. Исправление сохранено, данные не менялись.',flush=True)
    print('Теперь запустите привычный start.bat. Программа сама этой проверкой не запускалась.',flush=True)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Исправление не установлено: '+type(exc).__name__+'.',flush=True)
        if isinstance(exc,(ValueError,RuntimeError)):print(str(exc),flush=True)
        raise SystemExit(1)
