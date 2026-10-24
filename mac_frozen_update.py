"""One-time activation for the frozen macOS 2.0.19 customer build.

Distributed inside one executable .command with a pinned official bridge.
The old updater makes its normal backup; the bridge stages a separate copy.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def running_senders(root):
    """Old Mac builds have no process lock. Check same-user Python working dirs."""
    commands = subprocess.run(['ps','-axo','pid=,command='],check=True,capture_output=True,text=True).stdout
    python_pids = {int(line.strip().split(None,1)[0]) for line in commands.splitlines()
                   if line.strip() and 'python' in line.casefold()}
    result = subprocess.run(['lsof','-n','-a','-u',str(os.getuid()),'-d','cwd','-Fpn'],
                            capture_output=True,text=True)
    if result.returncode not in (0,1):
        raise RuntimeError('Не удалось проверить остановку старой программы.')
    pid = None
    for line in result.stdout.splitlines():
        if line.startswith('p'):
            pid = int(line[1:])
        elif line.startswith('n') and pid in python_pids and pid != os.getpid():
            cwd = Path(line[1:])
            if cwd == root or (cwd.parent == root.parent and cwd.name.startswith(root.name+'_dvk_')):
                return True
    return False


def unpack_verified_bridge(raw, expected, folder):
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Повреждён файл обновления.')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        script = archive.read('launcher.py')
        assert script == archive.read('main.py')
        # Read constants without executing downloaded code.
        import ast
        import base64
        constants = {n.targets[0].id:ast.literal_eval(n.value) for n in ast.parse(script).body
                     if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)}
        bundle = base64.b85decode(constants['PAYLOAD'])
        if hashlib.sha256(bundle).hexdigest() != constants['SHA256']:
            raise ValueError('Повреждён переходный комплект.')
    # Extract only flat bootstrap modules before importing the verified bundle reader.
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        for name in ('startup_update.py','transition_setup.py','manual_update.py','file_lock.py','start.py'):
            (folder/name).write_bytes(archive.read(name))
    sys.path.insert(0,str(folder))
    from startup_update import unpack_bundle
    full = folder/'bundle'; full.mkdir()
    unpack_bundle(bundle,full)
    return script,full


def install(root, raw, expected, *, check_only=False, running=running_senders):
    root = Path(root).resolve()
    if running(root):
        raise RuntimeError('Сначала закройте работающую рассылку и её окно Terminal. Затем повторите запуск обновления.')
    for name in ('launcher.py','main.py','version.txt','.env','start_mac.sh'):
        if not (root/name).is_file() or (root/name).is_symlink():
            raise ValueError('Перенесите файл обновления в установленную папку программы, рядом со start_mac.sh.')
    with tempfile.TemporaryDirectory(prefix='dvk-mac-update-') as temporary:
        script,bundle = unpack_verified_bridge(raw,expected,Path(temporary))
        from file_lock import FileLock
        from transition_setup import inspect_source
        with FileLock(root/'data/startup.lock'), FileLock(root/'data/sender.lock'):
            current = (root/'version.txt').read_text('utf-8-sig').strip()
            if current == '2.0.19':
                baselines = json.loads((bundle/'legacy-baselines.json').read_text('utf-8'))
                baselines['profiles'] = [p for p in baselines['profiles'] if p['name']=='macos-2.0.19']
                inspect_source(root,baselines)  # Never execute unknown local launcher code.
                spec = importlib.util.spec_from_file_location('dvk_frozen_legacy_launcher',root/'launcher.py')
                legacy = importlib.util.module_from_spec(spec); spec.loader.exec_module(legacy)
                package = Path(temporary)/'official-bridge.zip'; package.write_bytes(raw)
                if not legacy.apply_update(package,'2.0.20'):
                    raise RuntimeError('Старый обновлятор остановил переход. См. сообщение выше.')
            elif (current != '2.0.20' or (root/'launcher.py').read_bytes() != script
                  or (root/'main.py').read_bytes() != script):
                raise ValueError('Неизвестная версия. Автоматическая замена отменена.')
        # Release the locks: the bridge obtains them for its own entire transition.
        environment = {**os.environ,'PYTHONIOENCODING':'utf-8'}
        subprocess.run([sys.executable,str(root/'launcher.py'),'--self-check'],cwd=root,
                       env=environment,check=True)
        from startup_update import active_installation
        active = active_installation(root)
        if active == root or not (active/'data/installed-baseline.json').is_file():
            raise RuntimeError('Новая установка ещё не готова. Данные сохранены; повторите обновление.')
        if (active/'version.txt').read_text('utf-8-sig').strip() != '2.0.20':
            raise RuntimeError('Неожиданная версия после перехода.')
        print('Обновление до 2.0.20 завершено. Привычный start_mac.sh теперь запускает новую версию.',flush=True)
        if not check_only:
            return subprocess.run([sys.executable,str(root/'launcher.py')],cwd=root,env=environment).returncode
    return 0


def command_main(raw, expected):
    if sys.platform != 'darwin':
        raise SystemExit('Этот файл предназначен только для macOS.')
    if sys.version_info < (3,11):
        raise SystemExit('Нужен Python 3.11 или новее.')
    if sys.argv[2:] not in ([],['--check-only']):
        raise SystemExit('Неизвестный параметр запуска.')
    try:
        return install(sys.argv[1],raw,expected,check_only=sys.argv[2:]==['--check-only'])
    except Exception as exc:
        print('Обновление остановлено: '+str(exc),flush=True)
        print('Не удаляйте папку программы и резервные копии. Передайте сообщение владельцу.',flush=True)
        return 1
