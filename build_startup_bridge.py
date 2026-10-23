"""Build a legacy-compatible bridge. Does not publish or change latest.

The old updater requires its exact allowlist. Its pre-update backup supplies
the real local code/catalog again before migration; placeholders are never run.
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

from startup_update import LEGACY_FILES


def build(root, transition_zip, output):
    root, output = Path(root), Path(output)
    raw = Path(transition_zip).read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
        with zipfile.ZipFile(io.BytesIO(bundle.read('manager-package.zip'))) as manager:
            version = json.loads(manager.read('manifest.json'))['version']
    # Standalone stdlib entry point: old updaters cannot deliver new module names.
    # Extract only known flat bootstrap modules; the full bundle is checked by run.
    script = '''"""Legacy startup bridge; generated from the verified transition bundle."""
import base64, hashlib, io, pathlib, subprocess, sys, tempfile, zipfile
PAYLOAD = %r
SHA256 = %r
def main():
    raw = base64.b85decode(PAYLOAD)
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError('Damaged startup bridge')
    root = pathlib.Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='dvk-bootstrap-') as directory:
        target = pathlib.Path(directory)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for name in ('startup_update.py','transition_setup.py','manual_update.py','file_lock.py','start.py'):
                (target/name).write_bytes(archive.read(name))
        sys.path.insert(0, str(target))
        from startup_update import run
        if sys.argv[1:] == ['--self-check']:
            def probe(active):
                if not (active/'data/installed-baseline.json').is_file():
                    return 1
                return subprocess.run([sys.executable,str(active/'main.py'),'--self-check'],cwd=active).returncode
            return run(root, initial_bundle=raw, launch=probe)
        if sys.argv[1:]:
            raise ValueError('Unsupported bridge arguments')
        return run(root, initial_bundle=raw)
if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Переход остановлен. Данные сохранены. Нужна проверка владельца: '+type(exc).__name__)
        raise SystemExit(1)
''' % (base64.b85encode(raw), hashlib.sha256(raw).hexdigest())
    payload = {}
    for name in LEGACY_FILES:
        data = subprocess.run(['git','show','origin/release/2.0.19:'+name], cwd=root,
                              capture_output=True, check=True).stdout
        payload[name] = data
    payload['launcher.py'] = payload['main.py'] = script.encode('utf-8')
    payload['version.txt'] = (version+'\n').encode('ascii')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            archive.writestr(name,data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest+'  '+output.name+'\n',encoding='ascii')
    return digest
