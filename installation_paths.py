"""Resolve one permanent data root without trusting an arbitrary environment path."""
import json
from pathlib import Path

def state_root(code=None):
    code=Path(code or __file__).resolve()
    folder=code if code.is_dir() else code.parent
    # Unified runtimes have an exact structural relationship to the data root.
    if folder.parent.name=='runtime' and folder.parent.parent.name=='.dvk':
        root=folder.parent.parent.parent
        marker=root/'.dvk/layout.json'
        if marker.is_symlink() or not marker.is_file():raise ValueError('Unified installation marker missing')
        record=json.loads(marker.read_text('utf-8'))
        if record.get('format')!=1 or record.get('data_root')!='.':raise ValueError('Invalid data root')
        return root
    return folder

def code_root():return Path(__file__).resolve().parent
