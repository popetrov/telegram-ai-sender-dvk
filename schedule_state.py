"""Persist schedule metadata, never automatically repeat an interrupted send."""
import json
import os
from pathlib import Path

PATH = Path('data/schedule.json')


def save(queue, path=PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {key: queue.get(key) for key in ('owner_user_id', 'chat_id', 'state', 'file_name')}
    metadata['send_at'] = queue['send_at'].isoformat() if queue.get('send_at') else None
    metadata['prepared_ids'] = [i.get('prepared_id') for i in queue.get('items', [])]
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(metadata, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def clear(path=PATH):
    Path(path).unlink(missing_ok=True)


def recover(path=PATH):
    path = Path(path)
    if not path.exists():
        return None
    metadata = json.loads(path.read_text('utf-8'))
    if not isinstance(metadata, dict):
        raise ValueError('Повреждено сохранённое расписание. Проверьте data/schedule.json.')
    metadata['state'] = 'interrupted'
    metadata['items'] = []
    return metadata
