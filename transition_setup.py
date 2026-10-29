"""One-time side-by-side transition from approved legacy installations."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from contextlib import nullcontext

from file_lock import FileLock
import manual_update

ROOT = Path(__file__).resolve().parent
CORE = ('ai.py','arbitration_api.py','bot.py','config.py','database.py','launcher.py',
        'main.py','telegram_client.py','updater.py','requirements.txt')
BUNDLE_FILES = {'transition_setup.py','manual_update.py','file_lock.py','transition.bat',
                'transition_mac.sh','TRANSITION.md','center-public.json','legacy-baselines.json','manager-package.zip',
                'startup_update.py','start.py'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalized(data):
    return data.replace(b'\r\n',b'\n')


def verify_bundle(root):
    root = Path(root).resolve()
    manifest = json.loads((root/'transition-manifest.json').read_text('utf-8'))
    if manifest.get('format') != 1 or set(manifest.get('files',{})) != BUNDLE_FILES:
        raise ValueError('Неполный установочный комплект. Скачайте и распакуйте ZIP целиком.')
    for name, expected in manifest['files'].items():
        path = root/name
        if path.is_symlink() or not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError('Повреждён файл установщика: '+name)
    public = json.loads((root/'center-public.json').read_text('utf-8'))
    validate_public(public)
    baselines = json.loads((root/'legacy-baselines.json').read_text('utf-8'))
    return manifest, public, baselines


def validate_public(public):
    if public.get('format') != 1:
        raise ValueError('Неподдерживаемая конфигурация центра')
    name = public.get('service_bot','')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',name) or not name.lower().endswith('bot'):
        raise ValueError('Некорректное имя технического бота')
    for field in ('access_public_key','sync_public_key'):
        if len(base64.b64decode(public[field],validate=True)) != 32:
            raise ValueError('Некорректный публичный ключ центра')


def same_legacy_runtime(name, actual, expected):
    actual,expected=normalized(actual),normalized(expected)
    if actual==expected:return True
    if name!='bot.py':return False
    # Recognize only the known one-line 10 -> 1 research setting variant.
    pattern=rb'(?m)^MAX_PARALLEL_RESEARCH = (?:1|10)$'
    if len(re.findall(pattern,actual))!=1 or len(re.findall(pattern,expected))!=1:return False
    return re.sub(pattern,b'MAX_PARALLEL_RESEARCH = 1',actual)==re.sub(pattern,b'MAX_PARALLEL_RESEARCH = 1',expected)


def inspect_source(source, baselines):
    source = Path(source).resolve()
    version = (source/'version.txt').read_text('utf-8-sig').strip()
    profiles = [p for p in baselines['profiles'] if p['version'] == version]
    if not profiles:
        raise ValueError('Переход с версии '+version+' этим установщиком не проверен')
    for name in CORE:
        path = source/name
        if not path.is_file() or path.is_symlink():
            raise ValueError('Отсутствует или небезопасен файл старой установки: '+name)
        content = normalized(path.read_bytes())
        if not any(name in p['files'] and same_legacy_runtime(name,content,p['files'][name].encode('utf-8')) for p in profiles):
            raise ValueError('В старой программе изменён '+name+'. Нужна отдельная проверка; файл не перезаписан.')
    # Prefer the matching macOS profile when its launcher differs from Windows.
    profile = max(profiles,key=lambda p:sum(
        normalized((source/name).read_bytes()) == normalized(p['files'].get(name,'').encode('utf-8')) for name in CORE))
    def baseline(root,name):
        if name in ('config.py','telegram_client.py','requirements.txt'):
            # A recognised inherited file is the local base, not a custom change.
            local=(source/name).read_bytes()
            if any(name in p['files'] and same_legacy_runtime(name,local,p['files'][name].encode('utf-8')) for p in profiles):
                return local
        if name=='bot.py':
            local=(source/name).read_bytes()
            if any(same_legacy_runtime(name,local,p['files'].get(name,'').encode('utf-8')) for p in profiles):
                # Treat the reviewed setting variant as this installation's base,
                # so obsolete code is replaced rather than preserved as a conflict.
                return local
        original = manual_update.git_baseline(root,name)
        if original is not manual_update.MISSING:
            return original
        text = profile['files'].get(name)
        return text.encode('utf-8') if text is not None else manual_update.MISSING
    return version, baseline


def prepare_transition(source, destination, bundle=ROOT, *, source_locked=False):
    source,destination = Path(source).resolve(),Path(destination).resolve()
    manifest,public,baselines = verify_bundle(bundle)
    if not (source/'.env').is_file() or (source/'.env').is_symlink():
        raise ValueError('В старой установке отсутствует локальный .env')
    with (nullcontext() if source_locked else FileLock(source/'data/sender.lock')):
        version,baseline = inspect_source(source,baselines)
        report = manual_update.prepare(source,Path(bundle)/'manager-package.zip',
                                       manifest['files']['manager-package.zip'],destination,
                                       baseline_provider=baseline)
    result = dict(format=1,source=str(source),destination=str(destination),source_version=version,
                  candidate_version=report['candidate_version'],conflicts=report['conflicts'],
                  source_unchanged=True,activated=False,dependencies_checked=False,
                  local_startup_checked=False,network_startup_checked=False,ready_for_pilot=False)
    (destination/'transition-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    _, payload = manual_update.verify_package(Path(bundle)/'manager-package.zip',manifest['files']['manager-package.zip'])
    (destination/'data').mkdir(exist_ok=True)
    (destination/'data/installed-baseline.json').write_text(json.dumps(
        {name:content.decode('utf-8-sig') for name,content in payload.items()},ensure_ascii=False),encoding='utf-8')
    return result


def finish_transition(destination, bundle=ROOT):
    destination = Path(destination).resolve()
    verify_bundle(bundle)
    report_path = destination/'transition-report.json'
    report = json.loads(report_path.read_text('utf-8'))
    if report['conflicts']:
        raise ValueError('Есть конфликты. Проверьте update_report.json; запуск новой копии остановлен.')
    child_env = {**os.environ,'PYTHONIOENCODING':'utf-8'}
    with FileLock(destination/'data/sender.lock'):
        # This imports only start.py from the already verified candidate, never clients.
        subprocess.run([sys.executable,'-c','import start; start.prepare_environment()'],
                       cwd=destination,check=True,env=child_env)
        python = destination/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        report['dependencies_checked'] = True
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    subprocess.run([str(python),'manager_setup.py','--center-config',str(Path(bundle).resolve()/'center-public.json')],
                   cwd=destination,check=True,env=child_env)
    # main.py acquires its own lock, so release ours before the local startup probe.
    subprocess.run([str(python),'main.py','--self-check'],cwd=destination,check=True,timeout=90,env=child_env)
    report['local_startup_checked'] = True
    report['ready_for_pilot'] = True
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--sender-stopped',action='store_true')
    parser.add_argument('--prepare-only',action='store_true')
    args = parser.parse_args(argv)
    source = args.source or Path(input('Полный путь к старой папке программы: ').strip().strip('"'))
    destination = args.destination or source.with_name(source.name+'_new')
    if not args.sender_stopped:
        print('Закройте старую программу и её окно запуска. Старые версии не имеют надёжной блокировки процесса.')
        if input('После остановки введите STOPPED: ').strip() != 'STOPPED':
            return 1
    report = prepare_transition(source,destination)
    print('Новая папка создана: '+str(destination))
    if report['conflicts']:
        print('Нужно проверить конфликты в update_report.json. Старая программа сохранена.')
        return 2
    if args.prepare_only:
        print('Подготовка закончена. Зависимости и запуск пока не проверены.')
        return 0
    finish_transition(destination)
    print('Локальные проверки пройдены. Для первого подключения откройте новую папку и запустите start.bat.')
    print('До подтверждения работы новой папки не удаляйте и не запускайте одновременно старую установку.')
    print('При ошибке закройте новую программу и вернитесь к старой папке. Автоматического переключения не было.')
    return 0


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='backslashreplace')
    try:
        raise SystemExit(main())
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as exc:
        print('Переход остановлен: '+str(exc))
        print('Старая установка не заменялась. Проверьте новую папку и отчёты перед повторной попыткой.')
        raise SystemExit(1)
