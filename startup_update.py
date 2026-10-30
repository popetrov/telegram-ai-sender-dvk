"""Update before startup; retain the previous installation until local checks pass.

The original shortcut remains the entry point. An atomic pointer selects the
active sibling installation. Never roll back data after starting the sender.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import uuid
import zipfile

from file_lock import FileLock

REPOSITORY = 'popetrov/telegram-ai-sender-dvk'
API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
LATEST_PAGE = f'https://github.com/{REPOSITORY}/releases/latest'
MAX_BYTES = 64 * 1024 * 1024
_network_stage = 'release_metadata'
_transition_stage = 'bootstrap'
_transition_source = None


def report_transition_failure(root,exc):
    """Safe startup report even before the account or telemetry client can start."""
    import time
    import traceback
    from transition_setup import CORE
    reason='transition_failed'
    filename=None
    message=str(exc) if isinstance(exc,ValueError) else ''
    if message.startswith('В старой программе изменён '):
        reason='legacy_runtime_changed'
        filename=next((name for name in CORE if message.startswith('В старой программе изменён '+name+'.')),None)
    elif 'резервная копия' in message:reason='backup_missing_or_incomplete'
    elif 'активн' in message:reason='active_installation_unavailable'
    elif 'конфликт' in message.lower():reason='customization_conflict'
    elif isinstance(exc,subprocess.CalledProcessError):reason='child_process_failed'
    report=dict(protocol='DVK_START_FAILURE_V1',recorded_at=int(time.time()),stage=_transition_stage,
                error_type=type(exc).__name__,reason_code=reason,filename=filename,
                call_stack=[dict(file=Path(frame.filename).name,line=frame.lineno,function=frame.name)
                            for frame in traceback.extract_tb(exc.__traceback__,limit=16)],core_hashes={})
    if _transition_source is not None:
        for name in CORE:
            path=Path(_transition_source)/name
            if path.is_file() and not path.is_symlink():report['core_hashes'][name]=hashlib.sha256(path.read_bytes()).hexdigest()
    path=Path(root)/'DVK_START_REPORT.txt'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def update_failure_report(root, active, exc):
    """Safe structured diagnostics: no URLs with credentials or exception prose."""
    import time
    reason=getattr(exc,'reason',None)
    report=dict(recorded_at=int(time.time()),stage=_network_stage,error_type=type(exc).__name__,
                reason_type=type(reason).__name__ if reason is not None else None,
                errno=getattr(reason,'errno',None),http_status=getattr(exc,'code',None),
                folder_id=hashlib.sha256(str(active).encode()).hexdigest(),
                version=(active/'version.txt').read_text('utf-8-sig').strip())
    path=root/'data/update-network-report.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return path
LEGACY_FILES = {'ai.py', 'arbitration_api.py', 'bot.py', 'database.py', 'launcher.py',
                'main.py', 'products.json', 'updater.py', 'version.txt',
                'prompts/bg.md', 'prompts/credit.md', 'prompts/pravo_ip.md',
                'prompts/pravo_ooo.md', 'prompts/zakupki_konsalt.md'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise ValueError('Invalid release version')
    return tuple(map(int, value.lstrip('v').split('.')))


def fetch(url):
    global _network_stage
    _network_stage='release_metadata' if url==API else 'release_asset'
    # Only official repository release assets/API. TLS validation stays enabled.
    prefixes = (API, f'https://github.com/{REPOSITORY}/releases/download/')
    if not any(url.startswith(prefix) for prefix in prefixes):
        raise ValueError('Unexpected update URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'DVK-Startup'})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Update exceeds size limit')
    return data


def stable_release_url():
    request=urllib.request.Request(LATEST_PAGE,headers={'User-Agent':'DVK-Startup'})
    try:
        with urllib.request.urlopen(request,timeout=15) as response:return response.geturl()
    except (urllib.error.URLError,TimeoutError):
        curl=shutil.which('curl.exe') or shutil.which('curl')
        if not curl:raise
        result=subprocess.run([curl,'--fail','--location','--silent','--show-error',
            '--proto','=https','--proto-redir','=https','--output',os.devnull,
            '--write-out','%{url_effective}','--connect-timeout','10','--max-time','20',LATEST_PAGE],
            capture_output=True,timeout=25,check=False)
        if result.returncode:raise urllib.error.URLError('Stable release endpoint unavailable')
        return result.stdout.decode('utf-8').strip()


def release_metadata():
    # Prefer the stable permalink: shared VPN IPs frequently exhaust API quotas.
    try:
        global _network_stage
        _network_stage='release_latest_redirect'
        # GitHub's stable-latest permalink excludes prereleases. Do not use the
        # Atom feed: a newer pilot there must never become a production update.
        final=stable_release_url()
        match=re.fullmatch(re.escape(f'https://github.com/{REPOSITORY}/releases/tag/')+r'(v\d+\.\d+\.\d+)',final)
        if not match:raise ValueError('Unexpected stable release redirect')
        tag=match[1]
        names=('dvk-transition-pilot.zip','dvk-transition-pilot.zip.sha256')
        return dict(tag_name=tag,draft=False,prerelease=False,assets=[
            dict(name=name,browser_download_url=f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}') for name in names])
    except (urllib.error.URLError,TimeoutError):
        return json.loads(fetch(API))


def latest_bundle(current):
    release = release_metadata()
    if release.get('draft') or release.get('prerelease'):
        return None
    tag = release['tag_name']
    if version(tag) <= version(current):
        return None
    names = ('dvk-transition-pilot.zip', 'dvk-transition-pilot.zip.sha256')
    assets = {}
    for name in names:
        matches = [a for a in release.get('assets', []) if a.get('name') == name]
        if len(matches) != 1:
            return None  # A legacy-only release is not a full platform update.
        url = matches[0]['browser_download_url']
        expected = f'https://github.com/{REPOSITORY}/releases/download/{tag}/{name}'
        if url != expected:
            raise ValueError('Unexpected release asset URL')
        assets[name] = url
    raw = fetch(assets[names[0]])
    checksum = fetch(assets[names[1]]).decode('ascii').strip()
    match = re.fullmatch(r'([a-fA-F0-9]{64})(?:\s+\*?\S+)?', checksum)
    if not match or hashlib.sha256(raw).hexdigest() != match[1].lower():
        raise ValueError('Update checksum mismatch')
    return tag, raw


def unpack_bundle(raw, destination):
    from transition_setup import BUNDLE_FILES, verify_bundle
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        names = [i.filename for i in entries]
        if (len(names) != len(set(n.casefold() for n in names))
                or set(names) != BUNDLE_FILES | {'transition-manifest.json'}
                or sum(i.file_size for i in entries) > MAX_BYTES):
            raise ValueError('Invalid transition archive')
        for info in entries:
            if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('Unsafe transition archive')
        for name in names:
            (destination / name).write_bytes(archive.read(name))
    verify_bundle(destination)


def load_unified(root):
    import importlib.util
    root=Path(root).resolve();record=json.loads((root/'.dvk/layout.json').read_text('utf-8'))
    rid=record.get('active','')
    if record.get('format')!=1 or not isinstance(rid,str) or not re.fullmatch('[0-9a-f]{32}',rid):raise ValueError('Invalid unified layout')
    code=root/'.dvk/runtime'/rid
    if code.is_symlink() or not code.resolve().is_relative_to(root):raise ValueError('Unsafe unified runtime')
    manifest=json.loads((code/'code-manifest.json').read_text('utf-8'))
    module=code/'unified_install.py'
    if hashlib.sha256(module.read_bytes()).hexdigest()!=manifest['files']['unified_install.py']:raise ValueError('Unified updater changed')
    spec=importlib.util.spec_from_file_location('unified_install',module)
    loaded=importlib.util.module_from_spec(spec);sys.modules['unified_install']=loaded;spec.loader.exec_module(loaded)
    return loaded


def active_installation(root):
    root = Path(root).resolve()
    if (root/'.dvk/layout.json').is_file():return load_unified(root).runtime(root)
    pointer = root / 'data/active-installation.json'
    if not pointer.exists():
        return root
    record = json.loads(pointer.read_text('utf-8'))
    name = record.get('directory', '')
    if (record.get('format') != 1 or not isinstance(name, str)
            or not re.fullmatch(re.escape(root.name) + r'_dvk_[0-9a-f]{32}', name)):
        raise ValueError('Повреждена запись активной версии. Нужна проверка владельца.')
    active = root.parent / name
    if active.is_symlink() or not active.is_dir() or active.resolve().parent != root.parent:
        raise ValueError('Активная версия недоступна. Нужна проверка владельца.')
    return active


def activate(root, candidate):
    root, candidate = Path(root).resolve(), Path(candidate).resolve()
    if candidate.parent != root.parent:
        raise ValueError('Active installation must be a sibling')
    path = root / 'data/active-installation.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(format=1, directory=candidate.name)), encoding='utf-8')
    os.replace(temporary, path)


def prepare_update(source, candidate, bundle, tag):
    # Never validate a newer package with the installed release's old allowlist.
    # The bundle has already passed its exact file list and checksum checks.
    from transition_setup import verify_bundle
    verify_bundle(bundle)
    script = ('import sys; from pathlib import Path; import startup_update as u; '
              'prepare=getattr(u,"_prepare_verified_update",u.prepare_update); '
              'prepare(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4])')
    subprocess.run([sys.executable,'-c',script,str(source.resolve()),str(candidate.resolve()),
                    str(bundle.resolve()),tag],cwd=bundle,check=True,
                   env={**os.environ,'PYTHONIOENCODING':'utf-8'})


def prepare_unified_update(root,bundle,tag):
    """Incoming full-package updater. Caller holds permanent startup/sender locks."""
    import importlib.util
    from transition_setup import verify_bundle
    import manual_update
    root,bundle=Path(root).resolve(),Path(bundle).resolve()
    manifest,_,_=verify_bundle(bundle)
    meta,payload=manual_update.verify_package(bundle/'manager-package.zip',manifest['files']['manager-package.zip'])
    if version(meta['version'])!=version(tag):raise ValueError('Update version mismatch')
    current=load_unified(root)
    current.runtime(root)  # attest old code, not permanent launcher stubs
    for helper in ('installation_inventory.py',):
        (bundle/helper).write_bytes(payload[helper])
    module=bundle/'unified_install.py';module.write_bytes(payload['unified_install.py'])
    spec=importlib.util.spec_from_file_location('unified_install',module)
    adapter=importlib.util.module_from_spec(spec);sys.modules['unified_install']=adapter;spec.loader.exec_module(adapter)
    code=adapter.prepare_code(root,payload)
    adapter.activate(root,code,root)
    return code


def _prepare_verified_update(source, candidate, bundle, tag):
    import manual_update
    from transition_setup import prepare_transition, finish_transition, verify_bundle
    manifest, _, _ = verify_bundle(bundle)
    package = bundle / 'manager-package.zip'
    metadata, payload = manual_update.verify_package(package, manifest['files']['manager-package.zip'])
    if version(metadata['version']) != version(tag):
        raise ValueError('Release and package versions differ')
    # Old clients extract only the fixed transition file list. Load this adapter
    # from the already verified manager payload without changing that wire format.
    if 'unified_install.py' in payload:
        old_baseline=source/'data/installed-baseline.json'
        if (source/'.dvk/layout.json').is_file():
            load_unified(source).runtime(source)
        elif old_baseline.is_file():
            reviewed=json.loads(old_baseline.read_text('utf-8'))
            for name,text in reviewed.items():
                if name not in manual_update.RUNTIME or name in ('products.json','.env.example') or name.startswith('prompts/'):continue
                p=source/name
                if not p.is_file() or p.is_symlink() or not manual_update.same_text(p.read_bytes(),text.encode('utf-8')):
                    raise ValueError('Locally modified runtime: '+name)
        else:
            _,_,profiles=verify_bundle(bundle)
            from transition_setup import inspect_source
            inspect_source(source,profiles)
        import importlib.util
        for helper in ('installation_inventory.py',):
            (bundle/helper).write_bytes(payload[helper])
        module_path=bundle/'unified_install.py'
        module_path.write_bytes(payload['unified_install.py'])
        spec=importlib.util.spec_from_file_location('unified_install',module_path)
        adapter=importlib.util.module_from_spec(spec);sys.modules['unified_install']=adapter;spec.loader.exec_module(adapter)
        return adapter.prepare_legacy(source,candidate,payload)

    baseline_path = source / 'data/installed-baseline.json'
    if baseline_path.is_file():
        baseline = json.loads(baseline_path.read_text('utf-8'))
        def provider(root, name):
            text = baseline.get(name)
            return text.encode('utf-8') if text is not None else manual_update.MISSING
        # Reject core modifications rather than silently carrying obsolete code.
        for name in manual_update.RUNTIME - {'products.json', '.env.example'}:
            if name.startswith('prompts/') or name not in baseline:
                continue
            path = source / name
            if not path.is_file() or not manual_update.same_text(path.read_bytes(), baseline[name].encode('utf-8')):
                raise ValueError('Locally modified runtime: ' + name)
        report = manual_update.prepare(source, package, manifest['files']['manager-package.zip'], candidate,
                                       baseline_provider=provider)
        (candidate / 'transition-report.json').write_text(json.dumps(dict(
            conflicts=report['conflicts'], activated=False)), encoding='utf-8')
    else:
        prepare_transition(source, candidate, bundle, source_locked=True)
    finish_transition(candidate, bundle)
    # Store the distributed baseline, not merged customer customizations.
    (candidate / 'data/installed-baseline.json').write_text(json.dumps(
        {name: content.decode('utf-8-sig') for name, content in payload.items()},
        ensure_ascii=False), encoding='utf-8')


def recover_legacy(root, bundle):
    """Recover pre-bridge files from the backup made by the old updater.

    Never use the bridge's placeholder product catalog or the backup database:
    snapshot current local data, then restore only the backed-up code/products.
    """
    import manual_update
    global _transition_stage,_transition_source
    _transition_stage='legacy_backup_select'
    from transition_setup import CORE, inspect_source, verify_bundle
    _, _, baselines = verify_bundle(bundle)
    backups = sorted((root / 'data/backups').glob('*/manifest.json'), reverse=True)
    if not backups:
        raise ValueError('Не найдена резервная копия старого обновления')
    backup = backups[0].parent
    record = json.loads(backups[0].read_text('utf-8'))
    if set(record.get('existing_files', [])) != LEGACY_FILES:
        raise ValueError('Неполная резервная копия старой версии')
    source = root.with_name(root.name + '_dvk_' + uuid.uuid4().hex)
    source.mkdir()
    _transition_source=source
    for name in set(CORE) | {'version.txt', 'products.json'}:
        origin = backup / name if name in LEGACY_FILES else root / name
        if origin.is_symlink() or not origin.is_file():
            raise ValueError('Неполная старая установка')
        shutil.copy2(origin, source / name)
    prompts = root / 'prompts'
    if prompts.exists():
        if prompts.is_symlink() or any(p.is_symlink() for p in prompts.rglob('*')):
            raise ValueError('Unsafe local prompts')
        shutil.copytree(prompts, source / 'prompts')
    for name in LEGACY_FILES:
        if name.startswith('prompts/'):
            if (backup / name).is_symlink() or not (backup / name).is_file():
                raise ValueError('Unsafe backup prompt')
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / name, target)
    _transition_stage='legacy_source_verify'
    inspect_source(source, baselines)
    _transition_stage='legacy_data_snapshot'
    manual_update.snapshot_local(root, source)
    activate(root, source)  # Durable fallback before installing anything new.
    return source


def run(root, *, updates=True, discover=latest_bundle, prepare=prepare_update, launch=None,
        initial_bundle=None):
    root = Path(root).resolve()
    if (root/'.dvk/layout.json').is_file():
        adapter=load_unified(root)
        return adapter.run(root,updates=updates,discover=discover,launch_fn=(lambda r,c:launch(c)) if launch is not None else adapter.launch)
    global _transition_stage
    _transition_stage='active_installation_check'
    os.environ['DVK_LAUNCHER_ROOT']=str(root)
    from start import prepare_environment
    if launch is None:
        def launch(active):
            if active != root and not (active / 'data/installed-baseline.json').exists():
                # The restored legacy sender uses the interpreter that already
                # ran its old launcher. Falling back must not need the internet.
                python = Path(sys.executable)
            else:
                python = prepare_environment(active)
            return subprocess.run([str(python), str(active / 'main.py')], cwd=active).returncode
    # Held for the complete sender lifetime, including staging and launch.
    with FileLock(root / 'data/startup.lock'):
        active = active_installation(root)  # Corrupt pointer must never start a stale database.
        if initial_bundle is not None and active == root:
            with tempfile.TemporaryDirectory(prefix='dvk-bridge-') as temporary:
                bundle = Path(temporary)
                unpack_bundle(initial_bundle, bundle)
                active = recover_legacy(root, bundle)
        if initial_bundle is not None:
            # Embedded bytes were delivered and verified by the old updater.
            # Do not need a second download to complete the first transition.
            def discover(current):
                with zipfile.ZipFile(io.BytesIO(initial_bundle)) as archive:
                    with zipfile.ZipFile(io.BytesIO(archive.read('manager-package.zip'))) as manager:
                        tag = json.loads(manager.read('manifest.json'))['version']
                return (tag, initial_bundle) if version(tag) > version(current) else latest_bundle(current)
        if updates:
            try:
                print('Проверяю обновления…', flush=True)
                release = discover((active / 'version.txt').read_text('utf-8-sig').strip())
                if release:
                    tag, raw = release
                    with FileLock(active / 'data/sender.lock'):
                        candidate = root.with_name(root.name + '_dvk_' + uuid.uuid4().hex)
                        with tempfile.TemporaryDirectory(prefix='dvk-update-') as temporary:
                            bundle = Path(temporary)
                            unpack_bundle(raw, bundle)
                            print('Устанавливаю обновление. Сохраняю прежнюю версию и данные…', flush=True)
                            _transition_stage='candidate_prepare'
                            prepare(active, candidate, bundle, tag)
                        activate(root, candidate)
                        active = candidate
                    print('Обновление готово. Запускаю программу.', flush=True)
            except Exception as exc:
                try:
                    update_failure_report(root,active,exc)
                    print('Отчёт: data/update-network-report.json. Настройки сети и папку не меняйте без проверки отчёта.',flush=True)
                except Exception:
                    pass
                # Log only type: subprocess/HTTP errors can include local settings.
                print('Обновление отложено. Запускаю прежнюю версию. Причина: ' + type(exc).__name__, flush=True)
        # No rollback after launch: the new sender may already have sent messages.
        return launch(active)
