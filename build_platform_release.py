"""Build allowlisted pilot packages; never include local settings, databases or keys."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile
from manual_update import build_package

ROOT = Path(__file__).resolve().parent
CENTER_FILES = {
    'access_server.py', 'access_lease.py', 'access_runtime.py', 'authority_store.py',
    'sync_protocol.py', 'sync_runtime.py', 'sync_data.py', 'file_lock.py',
    'center_setup.py', 'center_backup.py', 'center.bat', 'center_mac.sh',
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    build_center(ROOT, args.output / 'dvk-center-pilot.zip')
    build_package(ROOT, args.output / 'dvk-manager-pilot.zip')
    print('Built separate center and manager pilot packages; no latest release changed.')


if __name__ == '__main__':
    main()
