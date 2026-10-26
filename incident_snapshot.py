"""Offline, read-only incident snapshot. Never starts/imports the sender or reads credentials into the report."""
import ast
import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

FILES = ('version.txt', 'main.py', 'bot.py', 'config.py', 'start.py', 'startup_update.py',
         'manual_update.py', 'transition_setup.py', 'launcher.py', 'telegram_client.py', 'access_runtime.py',
         'app_profile_client.py', 'sync_runtime.py', 'dialogue_runtime.py', 'safe_telegram.py', 'telegram_safety.py')


def read(path, limit=4*1024*1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('unsafe_or_missing')
    return path.read_bytes()


def settings(root):
    # Read locally for path/identity selection; never include the raw file or other values.
    values = {}
    for line in read(root/'.env', 128*1024).decode('utf-8-sig').splitlines():
        match = re.match(r'^\s*(MANAGER_ID|DATABASE_PATH|SESSION_NAME)\s*=\s*(.*?)\s*$', line)
        if match:
            values[match[1]] = match[2].strip('"\'')
    return values


def snapshot(root):
    root = Path(root).resolve()
    report = dict(format=1, recorded_at=datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
                  mode='offline_read_only_no_telegram_no_sender', installations=[])
    roots = [('launcher', root)]
    pointer = root/'data/active-installation.json'
    if pointer.exists():
        try:
            record = json.loads(read(pointer, 4096))
            name = record['directory']
            if not isinstance(name, str) or Path(name).name != name or name in ('.','..') or '/' in name or '\\' in name:
                raise ValueError('unsafe_pointer')
            active = root.parent/name
            if active.is_symlink() or not active.is_dir() or active.resolve().parent != root.parent:
                raise ValueError('missing_or_unsafe_active')
            roots.append(('active', active))
        except Exception as exc:
            report['pointer_error_type'] = type(exc).__name__
    for role, folder in roots:
        item = dict(role=role, files={}, database_counts={})
        report['installations'].append(item)
        try:
            values = settings(folder)
            manager = values.get('MANAGER_ID','')
            item['manager_id'] = manager if re.fullmatch('[A-Za-z0-9_-]{1,100}',manager) else 'invalid_or_missing'
            item['session_file_count'] = len([p for p in folder.glob('*.session') if p.is_file() and not p.is_symlink()])
        except Exception as exc:
            values = {}
            item['configuration_error_type'] = type(exc).__name__
        for name in FILES:
            path = folder/name
            if not path.exists():
                continue
            try:
                raw = read(path)
                entry = dict(sha256=hashlib.sha256(raw).hexdigest(), modified_at=datetime.datetime.fromtimestamp(
                    path.stat().st_mtime).astimezone().isoformat(timespec='seconds'))
                if name == 'version.txt':
                    text = raw.decode('utf-8-sig').strip()
                    entry['version'] = text if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',text) else 'unknown'
                else:
                    tree = ast.parse(raw.decode('utf-8-sig'))
                    calls = []
                    for node in ast.walk(tree):
                        if not isinstance(node,ast.Call):continue
                        f = node.func
                        label = f.attr if isinstance(f,ast.Attribute) else f.id if isinstance(f,ast.Name) else ''
                        if re.fullmatch('[A-Za-z0-9_]{1,100}',label) and (
                                label.startswith(('Delete','Clear','Destroy','Discard'))
                                or label in ('delete_messages','delete_dialog','cleanup_service_messages','clean_service_history',
                                             'SetHistoryTTLRequest','UpdateDialogFilterRequest','EditPeerFoldersRequest')):
                            calls.append(dict(operation=label,line=node.lineno))
                    entry['history_related_calls'] = calls
                item['files'][name] = entry
            except Exception as exc:
                item['files'][name] = dict(error_type=type(exc).__name__)
        binding = folder/'data/telegram-app-binding.private.json'
        if binding.exists():
            try:
                payload = json.loads(read(binding,32768))['payload']
                account = payload.get('account_id')
                item['declared_bound_account_id'] = account if type(account) is int else None
                item['binding_manager_matches_env'] = payload.get('manager_id') == values.get('MANAGER_ID')
                item['binding_note'] = 'reported_only_not_cryptographically_verified_by_this_offline_tool'
            except Exception as exc:
                item['binding_error_type'] = type(exc).__name__
        if values:
            database = (folder/values.get('DATABASE_PATH','data/local.sqlite')).resolve()
            if database.is_relative_to(folder) and database.is_file() and not database.is_symlink():
                try:
                    db = sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=3)
                    try:
                        db.execute('PRAGMA query_only=ON')
                        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        for table in ('clients','messages','prepared_messages','research_runs','dialogue_messages'):
                            if table in tables:
                                item['database_counts'][table] = db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                    finally:db.close()
                except Exception as exc:item['database_error_type'] = type(exc).__name__
    report['limits'] = ('Shows installed code, declared binding and local record counts. Does not reconstruct past '
                        'Telegram deletion requests, restore chats, or prove the cause of disappearance.')
    return report


def main():
    root = Path(__file__).resolve().parent
    output = root/'DVK_INCIDENT_REPORT.txt'
    output.write_text(json.dumps(snapshot(root),ensure_ascii=False,indent=2),encoding='utf-8')
    print('Проверка завершена без запуска программы и без подключения к Telegram.\n'
          'Передайте владельцу файл DVK_INCIDENT_REPORT.txt. Настройки, база и очередь не изменены.')


if __name__=='__main__':main()
