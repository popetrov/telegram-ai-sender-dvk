"""Materialize actual legacy source with synthetic settings; never real credentials."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def create_fixture(baselines,destination,profile_name='windows-2.0.19'):
    profile = next(p for p in baselines['profiles'] if p['name']==profile_name)
    destination = Path(destination)
    destination.mkdir()
    for name,text in profile['files'].items():
        path = destination/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text,encoding='utf-8')
    (destination/'version.txt').write_text(profile['version']+'\n',encoding='utf-8')
    settings = dict(TELEGRAM_API_ID='12345',TELEGRAM_API_HASH='fixture-api-hash',
                    BOT_TOKEN='123:test-only-token',OPENAI_API_KEY='test-only-openai',
                    MANAGER_ID='fixture_manager',SESSION_NAME='fixture_session',ADMIN_TELEGRAM_ID='42')
    (destination/'.env').write_text(''.join(k+'='+v+'\n' for k,v in settings.items()),encoding='utf-8')
    subprocess.run([sys.executable,'-c','import database; database.init_database()'],cwd=destination,
                   env={**os.environ,**settings},check=True,capture_output=True)
    db = sqlite3.connect(destination/'fixture_session.session')
    try:
        db.execute('CREATE TABLE fixture_data(value TEXT)')
        db.execute("INSERT INTO fixture_data VALUES ('synthetic session marker')")
        db.commit()
    finally:
        db.close()
    return destination


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--profile',default='windows-2.0.19',choices=['windows-2.0.19','macos-2.0.19','owner-2.0.9'])
    args = parser.parse_args()
    create_fixture(json.loads((args.bundle/'legacy-baselines.json').read_text('utf-8')),args.destination,args.profile)
    print('Legacy 2.0.19 source materialized with synthetic local settings and database.')
