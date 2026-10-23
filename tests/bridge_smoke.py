"""Exercise the *installed old updater*, then the standalone startup bridge.

Synthetic data only; the new sender runs --self-check, never Telegram or AI.
Dependency installation needs package-index access.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from startup_update import active_installation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--bridge',type=Path)
    parser.add_argument('--packages',type=Path)
    parser.add_argument('--verify-only',action='store_true')
    args = parser.parse_args()
    source = args.source.resolve()
    if args.verify_only:
        bridge = None
    elif args.bridge:
        bridge = args.bridge.resolve()
    elif args.packages:
        matches = list(args.packages.glob('telegram_ai_sender_v*.zip'))
        if len(matches) != 1:
            raise ValueError('Expected exactly one legacy bridge')
        bridge = matches[0].resolve()
    else:
        parser.error('--bridge or --packages is required')
    original_env = (source/'.env').read_bytes()
    if args.verify_only:
        backup = sorted((source/'data/backups').glob('*/manifest.json'),reverse=True)[0].parent
        original_products = (backup/'products.json').read_bytes()
    else:
        original_products = (source/'products.json').read_bytes()
    with sqlite3.connect(source/'fixture_session.session') as db:
        original_session = db.execute('SELECT value FROM fixture_data').fetchall()
    db.close()
    child_env = {**os.environ,'PYTHONIOENCODING':'utf-8'}
    # Import the actual OLD launcher from the fixture, not this repository.
    if not args.verify_only:
        with zipfile.ZipFile(bridge) as archive:
            version = archive.read('version.txt').decode('ascii').strip()
        subprocess.run([sys.executable,'-c',
                        'import launcher,sys; raise SystemExit(0 if launcher.apply_update(sys.argv[1],sys.argv[2]) else 1)',
                        str(bridge),version],cwd=source,env=child_env,check=True)
        subprocess.run([sys.executable,str(source/'launcher.py'),'--self-check'],
                       cwd=source,env=child_env,check=True)
    active = active_installation(source)
    assert active != source
    # Telethon can add its schema during import; existing session rows must survive.
    with sqlite3.connect(active/'fixture_session.session') as db:
        assert db.execute('SELECT value FROM fixture_data').fetchall() == original_session
    db.close()
    assert json.loads((active/'transition-report.json').read_text('utf-8'))['ready_for_pilot']
    # .env gains public center settings but all manager-owned values persist.
    from dotenv import dotenv_values
    old = dotenv_values(stream=__import__('io').StringIO(original_env.decode('utf-8')))
    new = dotenv_values(active/'.env')
    assert all(new[k] == v for k,v in old.items())
    old_catalog = json.loads(original_products)
    new_catalog = json.loads((active/'products.json').read_text('utf-8'))
    old_catalog = old_catalog.get('products',old_catalog)
    new_catalog = new_catalog.get('products',new_catalog)
    assert all(all(new_catalog[k][field] == value for field,value in product.items())
               for k,product in old_catalog.items())
    print('PASS: old updater -> bridge -> automatic transition -> local startup; credentials, session, products preserved; no client sent.')


if __name__=='__main__':
    main()
