"""Build allowlisted pilot packages; never include local settings, databases or keys."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile
import tempfile
from manual_update import build_package
from transition_setup import CORE, BUNDLE_FILES

ROOT = Path(__file__).resolve().parent
CENTER_FILES = {
    'incident_monitor.py','incident_isolation.py',
    'safe_telegram.py','telegram_safety.py',
    'app_profile_server.py','app_profile_protocol.py','app_profile_broker.py','telegram_app_migrate.py','telegram_app_trust.py',
    'access_server.py', 'access_lease.py', 'access_runtime.py', 'authority_store.py',
    'sync_protocol.py', 'sync_runtime.py', 'sync_data.py', 'file_lock.py',
    'center_setup.py', 'center_backup.py', 'center.bat', 'center_mac.sh',
    'center_runtime.py', 'owner_notifications.py',
    'dialogue_center.py','dialogue_knowledge.py','dialogue_knowledge_protocol.py','dialogue_knowledge_transport.py','bot_menu.py',
    'manager_directory.py', 'sheets_directory_sync.py', 'google_roster_bridge.gs', 'GOOGLE_ROSTER.md',
    'requirements-authority.txt', 'PILOT.md', 'DATA_POLICY.md', 'PROJECT_AUDIT.md',
}


def build_center(root, output):
    root, output = Path(root), Path(output)
    payload = {}
    for name in sorted(CENTER_FILES):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing or unsafe center file: ' + name)
        data = path.read_bytes()
        if name.endswith('.py'):
            compile(data, name, 'exec')
        payload[name] = data
    manifest = dict(format=1, role='authority-pilot',
                    files={name: hashlib.sha256(data).hexdigest() for name, data in payload.items()})
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, sort_keys=True))
        for name, data in payload.items():
            archive.writestr(name, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(f'{digest}  {output.name}\n', encoding='ascii')
    return digest


def legacy_profiles(root):
    profiles = []
    for label,ref in [('windows-2.0.19','origin/release/2.0.19'),
                      ('macos-2.0.19','origin/macos-support-2.0.19'),('owner-2.0.9','1b4e2fc')]:
        def read(name):
            return subprocess.run(['git','show',ref+':'+name],cwd=root,capture_output=True,check=True).stdout.decode('utf-8-sig')
        names = set(CORE) | {'products.json'}
        prompts = subprocess.run(['git','ls-tree','-r','--name-only',ref,'prompts'],cwd=root,
                                 capture_output=True,check=True).stdout.decode('utf-8').splitlines()
        names.update(name for name in prompts if name.endswith('.md'))
        profiles.append(dict(name=label,version=read('version.txt').strip(),files={name:read(name) for name in sorted(names)}))
    # The legacy updater deliberately never distributed these files. A manager
    # installed at 2.0.10 and updated to 2.0.19 still legitimately has 2.0.10's
    # requirements/config/client. Admit only exact published historical bytes.
    windows=profiles[0]
    inherited=('config.py','telegram_client.py','requirements.txt')
    seen={tuple(windows['files'][name] for name in inherited)}
    for patch in range(10,19):
        ref='v2.0.'+str(patch)
        old={}
        for name in inherited:
            result=subprocess.run(['git','show',ref+':'+name],cwd=root,capture_output=True)
            if result.returncode:break
            old[name]=result.stdout.decode('utf-8-sig')
        if len(old)!=len(inherited):continue
        signature=tuple(old[name] for name in inherited)
        if signature in seen:continue
        seen.add(signature)
        profiles.append(dict(name='windows-2.0.19-inherited-'+ref,version=windows['version'],
                             files={**windows['files'],**old}))
    return dict(format=1,profiles=profiles)


def build_transition(root, output, manager_package):
    root,output = Path(root),Path(output)
    payload = {name:(root/name).read_bytes() for name in BUNDLE_FILES
               if name not in {'legacy-baselines.json','manager-package.zip'}}
    payload['legacy-baselines.json'] = json.dumps(legacy_profiles(root),ensure_ascii=False).encode('utf-8')
    payload['manager-package.zip'] = Path(manager_package).read_bytes()
    manifest = dict(format=1,files={name:hashlib.sha256(data).hexdigest() for name,data in payload.items()})
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('transition-manifest.json',json.dumps(manifest,sort_keys=True))
        for name,data in sorted(payload.items()):
            archive.writestr(name,data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(f'{digest}  {output.name}\n',encoding='ascii')
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    build_center(ROOT, args.output / 'dvk-center-pilot.zip')
    build_package(ROOT, args.output / 'dvk-manager-pilot.zip')
    build_transition(ROOT,args.output/'dvk-transition-pilot.zip',args.output/'dvk-manager-pilot.zip')
    from build_startup_bridge import build
    version = (ROOT/'version.txt').read_text('utf-8-sig').strip()
    build(ROOT,args.output/'dvk-transition-pilot.zip',args.output/f'telegram_ai_sender_v{version}.zip')
    from build_update_repair import build as build_repair
    build_repair(ROOT,args.output/'DVK_UPDATE_REPAIR_WINDOWS.zip')
    from build_fresh_installer import build as build_fresh
    for platform in ('windows','macos','linux'):
        build_fresh(ROOT,args.output/'dvk-manager-pilot.zip',args.output/f'DVK_INSTALL_{platform.upper()}.zip',platform)
    guide=(ROOT/'docs/install/Установка Telegram AI Sender.pdf').read_bytes()
    (args.output/'DVK_INSTALL_GUIDE_RU.pdf').write_bytes(guide)
    (args.output/'DVK_INSTALL_GUIDE_RU.pdf.sha256').write_text(hashlib.sha256(guide).hexdigest()+'  DVK_INSTALL_GUIDE_RU.pdf\n',encoding='ascii')
    print('Built center, manager and one-time transition pilot packages; no latest release changed.')


if __name__ == '__main__':
    main()
