"""Optional owner-local daily backups with SQLite snapshots and bounded retention."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile


def backup(state, destination, keep=30):
    state, destination = Path(state).resolve(), Path(destination).resolve()
    if destination.is_relative_to(state) or state.is_relative_to(destination) or not 1 <= keep <= 3650:
        raise ValueError('Резервные копии должны быть в отдельной папке; хранение 1–3650 дней')
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / ('dvk-center-' + datetime.now(timezone.utc).strftime('%Y-%m-%d') + '.zip')
    if target.exists():
        return target
    with tempfile.TemporaryDirectory(dir=destination, prefix='center-backup-') as staging:
        staging = Path(staging)
        with closing(sqlite3.connect(state / 'authority.sqlite')) as source:
            with closing(sqlite3.connect(staging / 'authority.sqlite')) as snapshot:
                source.backup(snapshot)
                if snapshot.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity check failed')
        for name in ('signing.key', 'encryption.key', 'public_key.txt', 'sync_public_key.txt'):
            shutil.copy2(state / name, staging / name)
        knowledge=state/'knowledge.json'
        if knowledge.is_symlink():raise ValueError('Unsafe knowledge backup source')
        if knowledge.is_file():shutil.copy2(knowledge,staging/knowledge.name)
        temporary = staging / 'backup.zip'
        with zipfile.ZipFile(temporary, 'x', zipfile.ZIP_DEFLATED) as archive:
            for path in staging.iterdir():
                if path != temporary:
                    archive.write(path, path.name)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError('Backup archive integrity check failed')
        temporary.rename(target)
    # Only remove files belonging to this backup scheme after a successful backup.
    today = datetime.now(timezone.utc).date()
    for path in destination.glob('dvk-center-????-??-??.zip'):
        try:
            date = datetime.strptime(path.stem.removeprefix('dvk-center-'), '%Y-%m-%d').date()
        except ValueError:
            continue
        if (today - date).days >= keep and path != target:
            path.unlink()
    return target


async def loop(state, settings):
    while True:
        try:
            config = json.loads(Path(settings).read_text('utf-8'))
            await asyncio.to_thread(backup, state, config['directory'], config.get('keep_days', 30))
        except Exception as exc:
            logging.getLogger(__name__).error('Center backup failed (%s); check local destination', type(exc).__name__)
        await asyncio.sleep(3600)
