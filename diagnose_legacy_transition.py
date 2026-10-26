"""Read-only transition checks. Never opens credentials, sessions or business databases."""
import ast,base64,hashlib,io,json,re,sys,zipfile
from pathlib import Path
LEGACY={'ai.py','arbitration_api.py','bot.py','database.py','launcher.py','main.py','products.json','updater.py','version.txt',
        'prompts/bg.md','prompts/credit.md','prompts/pravo_ip.md','prompts/pravo_ooo.md','prompts/zakupki_konsalt.md'}
CORE=('ai.py','arbitration_api.py','bot.py','config.py','database.py','launcher.py','main.py','telegram_client.py','updater.py','requirements.txt')
def read(path,limit=32*1024*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:raise ValueError('unsafe_or_missing_file')
    return path.read_bytes()
def normalized(data):return data.replace(b'\r\n',b'\n')
def version(path):
    try:
        value=read(path,100).decode('utf-8-sig').strip()
        return value if re.fullmatch('v?[0-9]+\\.[0-9]+\\.[0-9]+',value) else 'необычный формат'
    except Exception:return 'не найден'
def bundle_profiles(root,lines):
    for name in ('main.py','launcher.py'):
        try:
            tree=ast.parse(read(root/name).decode('utf-8-sig'));constants={}
            for node in tree.body:
                if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
                    if node.targets[0].id in {'PAYLOAD','SHA256'}:constants[node.targets[0].id]=ast.literal_eval(node.value)
            if 'PAYLOAD' not in constants:continue
            data=base64.b85decode(constants['PAYLOAD'])
            if len(data)>64*1024*1024 or hashlib.sha256(data).hexdigest()!=constants.get('SHA256'):
                lines.append('ПРИЧИНА: повреждён встроенный пакет перехода.');return None
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(i.file_size for i in archive.infolist())>64*1024*1024:raise ValueError('oversized_archive')
                manifest=json.loads(archive.read('transition-manifest.json'))
                failed=[n for n,h in manifest['files'].items() if hashlib.sha256(archive.read(n)).hexdigest()!=h]
                if failed:
                    lines.append('ПРИЧИНА: контрольная сумма встроенного комплекта не совпадает.');return None
                lines.append('Встроенный пакет перехода: контрольные суммы подтверждены.')
                return json.loads(archive.read('legacy-baselines.json'))
        except Exception as exc:lines.append('Проверка переходного файла '+name+': '+type(exc).__name__)
    lines.append('Встроенный переходный пакет в этой папке не найден.')
    return None
def inspect(root):
    root=Path(root).resolve();lines=['ДВК — проверка перехода','Папка: '+root.name,'Python: '+sys.version.split()[0],
        'Проверка не запускает программу и не изменяет её данные.','Токены, ключи, сессии и клиентская база не читаются.',
        'Версия в выбранной папке: '+version(root/'version.txt')]
    profiles=bundle_profiles(root,lines)
    pointer=root/'data/active-installation.json'
    if pointer.exists():
        try:
            record=json.loads(read(pointer,16384).decode('utf-8-sig'));name=record.get('directory','')
            if record.get('format')!=1 or not isinstance(name,str) or not re.fullmatch(re.escape(root.name)+r'_dvk_[0-9a-f]{32}',name):
                lines.append('ПРИЧИНА: запись активной версии не соответствует имени этой папки (возможное переименование или копирование).');return lines
            active=root.parent/name
            if active.is_symlink() or not active.is_dir() or active.resolve().parent!=root.parent:
                lines.append('ПРИЧИНА: папка активной версии отсутствует рядом с исходной папкой.');return lines
            lines.append('Активная папка найдена. Её версия: '+version(active/'version.txt'))
            baseline=active/'data/installed-baseline.json'
            if baseline.exists():
                saved=json.loads(read(baseline).decode('utf-8-sig'));changed=[]
                for filename in CORE:
                    if filename in saved:
                        try:matches=normalized(read(active/filename))==normalized(saved[filename].encode('utf-8'))
                        except Exception:matches=False
                        if not matches:changed.append(filename)
                lines.append('Изменённые основные файлы активной версии: '+(', '.join(changed) or 'нет'))
                if changed:lines.append('ПРИЧИНА: файлы активной программы отличаются от установленной версии.')
        except Exception as exc:lines.append('ПРИЧИНА: запись активной версии не читается ('+type(exc).__name__+').')
        return lines
    backups=sorted((root/'data/backups').glob('*/manifest.json'),reverse=True)
    lines.append('Резервных копий старого обновлятора: '+str(len(backups)))
    if not backups:
        lines.append('ПРИЧИНА: отсутствует резервная копия, необходимая для перехода с 2.0.19.');return lines
    try:
        backup=backups[0].parent;record=json.loads(read(backups[0],1024*1024).decode('utf-8-sig'))
        existing=record.get('existing_files',[])
        if not isinstance(existing,list) or any(not isinstance(x,str) for x in existing):raise ValueError('invalid_manifest')
        missing=sorted(LEGACY-set(existing));extra=set(existing)-LEGACY
        lines.append('Восстанавливаемая версия: '+version(backup/'version.txt'))
        if missing or extra:
            lines.append('ПРИЧИНА: состав последней резервной копии не подходит переходному обновлятору.')
            lines.append('Отсутствуют в перечне: '+(', '.join(missing) or 'нет'))
            lines.append('Лишних записей в перечне: '+str(len(extra)))
        missing_files=[];changed=[];old_version=version(backup/'version.txt')
        supported=[p for p in (profiles or {}).get('profiles',[]) if p['version']==old_version]
        for filename in CORE:
            origin=(backup if filename in LEGACY else root)/filename
            try:data=normalized(read(origin))
            except Exception:missing_files.append(filename);continue
            if supported and not any(normalized(p['files'].get(filename,'').encode('utf-8'))==data for p in supported):changed.append(filename)
        if missing_files:lines.append('ПРИЧИНА: отсутствуют файлы старой установки: '+', '.join(missing_files))
        if changed:lines.append('ПРИЧИНА: основные файлы старой установки отличаются от проверенных: '+', '.join(changed))
        if profiles is not None and not supported:lines.append('ПРИЧИНА: версия резервной копии не входит в проверенные варианты перехода.')
        if not (missing or extra or missing_files or changed):lines.append('Структурные проверки старой установки пройдены.')
    except Exception as exc:lines.append('Проверка резервной копии: '+type(exc).__name__)
    return lines
def main(root=None):
    root=Path(root or Path(__file__).resolve().parent)
    try:lines=inspect(root)
    except Exception as exc:lines=['Проверка не завершена: '+type(exc).__name__]
    report=root/'DVK_UPDATE_DIAGNOSTIC.txt';report.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('\n'.join(lines));print('\nОтправьте владельцу файл DVK_UPDATE_DIAGNOSTIC.txt из этой папки.')
if __name__=='__main__':main()
