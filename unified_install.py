"""One visible launcher, permanent user data, bounded internal code generations.

Never restore an old business database after a runtime has started.
"""
import contextlib,hashlib,json,os,re,shutil,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
from file_lock import FileLock

FORMAT=1

def linked(path):
    path=Path(path)
    try:return path.is_symlink() or bool(getattr(path.lstat(),'st_file_attributes',0)&0x400)
    except FileNotFoundError:return False

def owned_directory(root,path):
    root,path=Path(root).resolve(),Path(path)
    if not path.resolve().is_relative_to(root):raise ValueError('Directory escapes installation')
    for parent in [path,*path.parents]:
        if parent==root:break
        if linked(parent):raise ValueError('Linked installation directory')
BOOTSTRAP='''"""Stable DVK launcher. User data belongs to this directory."""
import json,os,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parent
if (root/'.dvk/migration-pending.json').exists():
    pending=json.loads((root/'.dvk/migration-pending.json').read_text('utf-8'))
    rid=pending.get('runtime','')
    if not isinstance(rid,str) or len(rid)!=32 or any(c not in '0123456789abcdef' for c in rid):raise SystemExit('Небезопасная запись восстановления.')
    staged=root/'.dvk/runtime'/rid
    if staged.is_symlink():raise SystemExit('Небезопасная папка восстановления.')
    sys.path.insert(0,str(staged))
    import unified_install as repair
    repair.verify_code(staged)
    repair.recover_prelaunch(root)
    (root/'.dvk/migration-pending.json').unlink()
    repair.cleanup_recovery(root)
    # Recovery must restart with a clean import path; restored flat code
    # must not import modules from the failed staged generation.
    raise SystemExit(subprocess.run([sys.executable,str(root/'start.py'),*sys.argv[1:]],cwd=root).returncode)
p=root/'.dvk/layout.json'
if p.is_symlink():raise SystemExit('Небезопасная запись установки.')
r=json.loads(p.read_text('utf-8'))
name=r.get('active','')
if r.get('format')!=1 or not isinstance(name,str) or len(name)!=32 or any(c not in '0123456789abcdef' for c in name):
    raise SystemExit('Повреждена запись установки. Данные сохранены.')
active=root/'.dvk/runtime'/name
if active.is_symlink() or active.resolve().parent!=(root/'.dvk/runtime').resolve():raise SystemExit('Небезопасная версия программы.')
python=root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
if not python.is_file():raise SystemExit('Среда Python недоступна. Данные сохранены; нужна проверка установки.')
raise SystemExit(subprocess.run([str(python),str(active/'start.py'),*sys.argv[1:]],cwd=root).returncode)
'''
MAIN_STUB='''"""Compatibility entry: business code is never loaded from this directory."""
if __name__=='__main__':
 import runpy
 from pathlib import Path
 runpy.run_path(str(Path(__file__).resolve().parent/'start.py'),run_name='__main__')
'''

def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise ValueError('Unsafe installation path')
    tmp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    with tmp.open('xb') as out:out.write(data);out.flush();os.fsync(out.fileno())
    os.replace(tmp,path)

def json_write(path,value):atomic(path,json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2).encode())
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def layout(root):
    root=Path(root).resolve();p=root/'.dvk/layout.json'
    if p.is_symlink():raise ValueError('Unsafe layout')
    value=json.loads(p.read_text('utf-8'))
    if value.get('format')!=FORMAT or value.get('data_root')!='.':raise ValueError('Unsupported layout')
    for key in ('active','previous'):
        v=value.get(key)
        if v is not None and (not isinstance(v,str) or not re.fullmatch('[0-9a-f]{32}',v)):raise ValueError('Unsafe runtime pointer')
    return value

def runtime(root,value=None):
    root=Path(root).resolve();value=value or layout(root);p=root/'.dvk/runtime'/value['active']
    if p.is_symlink() or p.resolve().parent!=(root/'.dvk/runtime').resolve() or not p.is_dir():raise ValueError('Active runtime unavailable')
    verify_code(p)
    return p

def verify_code(folder):
    folder=Path(folder);record=json.loads((folder/'code-manifest.json').read_text('utf-8'))
    if record.get('format')!=1 or not isinstance(record.get('files'),dict):raise ValueError('Invalid code manifest')
    for name,sha in record['files'].items():
        p=folder/name
        if (Path(name).is_absolute() or '..' in Path(name).parts or p.is_symlink()
                or not p.is_file() or digest(p)!=sha):raise ValueError('Runtime code changed: '+name)
    return record

def read_identity(root):
    root=Path(root).resolve();values={}
    p=root/'.env'
    if p.is_symlink():raise ValueError('Unsafe environment file')
    for line in p.read_text('utf-8-sig').splitlines():
        match=re.match(r'^\s*(MANAGER_ID|SESSION_NAME)\s*=\s*(.*?)\s*$',line)
        if match:
            if match[1] in values:raise ValueError('Duplicate identity setting')
            values[match[1]]=match[2].strip('"\'')
    manager=values.get('MANAGER_ID','')
    if not re.fullmatch('[A-Za-z0-9_-]{1,100}',manager):raise ValueError('Manager identity missing')
    name=values.get('SESSION_NAME','telegram_sender');p=root/(name if name.endswith('.session') else name+'.session')
    if not p.resolve().is_relative_to(root) or p.is_symlink():raise ValueError('Unsafe session path')
    key=None
    if p.is_file():
        with contextlib.closing(sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            row=db.execute('SELECT auth_key FROM sessions').fetchone()
            if row and row[0]:key=hashlib.sha256(row[0]).hexdigest()
    return manager,key

def snapshot_file(source,destination):
    source,destination=Path(source),Path(destination)
    if source.is_symlink() or destination.is_symlink():raise ValueError('Unsafe personal file')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if source.suffix.lower() in ('.sqlite','.sqlite3','.db','.session'):
        tmp=destination.with_name(destination.name+'.snapshot')
        if tmp.exists():raise ValueError('Interrupted snapshot requires recovery')
        with contextlib.closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as old,contextlib.closing(sqlite3.connect(tmp)) as new:
            old.backup(new)
            if new.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Invalid SQLite snapshot')
        os.replace(tmp,destination)
    else:atomic(destination,source.read_bytes())

def personal_paths(source):
    source=Path(source)
    for p in source.iterdir():
        if p.name=='.env' or p.name.endswith('.session'):yield p
    data=source/'data'
    if data.is_symlink():raise ValueError('Unsafe data directory')
    if data.exists():
        for directory,dirs,files in os.walk(data,followlinks=False):
            if any((Path(directory)/n).is_symlink() for n in dirs):raise ValueError('Unsafe personal subdirectory')
            if Path(directory)==data:dirs[:]=[n for n in dirs if n not in ('backups','updates','access_authority')]
            for name in files:
                if (name.endswith(('.lock','-wal','-shm','-journal')) or name in ('active-installation.json','installed-baseline.json','update.request','security-install-pending.json')):continue
                p=Path(directory)/name
                if p.is_symlink():raise ValueError('Unsafe personal file')
                yield p

def prepare_code(root,payload):
    root=Path(root).resolve();folder=root/'.dvk/runtime'/uuid.uuid4().hex
    owned_directory(root,folder)
    folder.mkdir(parents=True,exist_ok=False)
    json_write(folder/'.dvk-stage.json',dict(format=1,root=hashlib.sha256(str(root).encode()).hexdigest(),runtime=folder.name))
    if os.name=='nt':
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(root/'.dvk'),2)
    try:
        for name,data in payload.items():
            if Path(name).is_absolute() or '..' in Path(name).parts or name.startswith(('data/','.dvk/')) or name=='.env' or name.endswith('.session'):raise ValueError('Personal data in code package')
            p=folder/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        json_write(folder/'code-manifest.json',dict(format=1,files={n:hashlib.sha256(v).hexdigest() for n,v in payload.items()}))
        verify_code(folder)
        return folder
    except BaseException:
        # This exact newly-created, owned code-only stage has no personal data.
        owned_directory(root,folder)
        shutil.rmtree(folder)
        raise

def merge_configuration(root,source,payload,baseline):
    from manual_update import merge_json,registry_products,MISSING
    root,source=Path(root),Path(source);conflicts=[];writes={}
    for name,data in payload.items():
        if name!='products.json' and not name.startswith('prompts/'):continue
        p=source/name;local=p.read_bytes() if p.exists() else None;base=baseline.get(name)
        if local is None:writes[name]=data;continue
        if name=='products.json':
            merged=merge_json(registry_products(json.loads(base)) if base is not None else MISSING,
                              registry_products(json.loads(local)),registry_products(json.loads(data)),conflicts)
            writes[name]=(json.dumps(merged,ensure_ascii=False,indent=2)+'\n').encode()
        elif base is None or local.replace(b'\r\n',b'\n')!=base.encode().replace(b'\r\n',b'\n'):
            writes[name]=local
        else:writes[name]=data
    if conflicts:raise ValueError('Custom product configuration conflict')
    # Preserve custom prompt files that are absent from the release.
    if (source/'prompts').exists():
        for p in (source/'prompts').rglob('*'):
            if p.is_symlink():raise ValueError('Unsafe custom prompt')
            if p.is_file():writes.setdefault(p.relative_to(source).as_posix(),p.read_bytes())
    return writes

def verify_environment(root,code,python=None):
    # Setup helpers only. --self-check explicitly never connects or sends.
    python=Path(python or sys.executable)
    script='import sys;sys.path.insert(0,sys.argv[1]);import start;start.prepare_environment(__import__("pathlib").Path(sys.argv[2]))'
    subprocess.run([str(python),'-c',script,str(code),str(root)],cwd=root,check=True)
    target=Path(root)/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    subprocess.run([str(target),str(code/'main.py'),'--self-check'],cwd=root,check=True,timeout=90)

def activate(root,code,source,*,check=verify_environment,private=False):
    root,code,source=Path(root).resolve(),Path(code).resolve(),Path(source).resolve()
    owned_directory(root,root/'.dvk');owned_directory(root,root/'data')
    manager,session=read_identity(source)
    if root!=source and (root/'.env').exists():
        own_manager,own_session=read_identity(root)
        if own_manager!=manager or (own_session and session and own_session!=session):raise ValueError('Refuse to merge different Telegram identities')
    previous=layout(root) if (root/'.dvk/layout.json').exists() else None
    journal=root/'.dvk/migration-pending.json'
    if journal.exists():
        stale=json.loads(journal.read_text('utf-8'))
        if stale.get('phase')!='rolled_back':raise ValueError('Interrupted migration requires recovery; no stale sender started')
        cleanup_recovery(root);journal.unlink()
    manifest=verify_code(code);payload={n:(code/n).read_bytes() for n in manifest['files']}
    baseline_path=source/'data/installed-baseline.json'
    baseline=json.loads(baseline_path.read_text('utf-8')) if baseline_path.is_file() else {}
    configs=merge_configuration(root,source,payload,baseline)
    # Reserve one pre-activation snapshot; do not overwrite unresolved recovery.
    backup=root/'.dvk/recovery'
    if backup.exists():raise ValueError('Unresolved previous recovery snapshot')
    backup.mkdir(parents=True)
    prior={};written=[]
    files=list(personal_paths(source)) if root!=source else []
    existing_state=list(personal_paths(root)) if (root/'.env').exists() else []
    for p in existing_state:
        relative=p.relative_to(root).as_posix()
        snapshot_file(p,backup/relative);prior[relative]=True
    for p in files:
        relative=p.relative_to(source).as_posix();target=root/relative
        if relative in prior:continue
        if target.exists():snapshot_file(target,backup/relative);prior[relative]=True
        else:prior[relative]=False
    for name in list(configs)+['requirements.txt','center-public.json','version.txt','start.py','main.py','launcher.py','start_mac.sh','start.bat','data/installed-baseline.json']:
        p=root/name
        if name in prior:continue
        if p.exists():snapshot_file(p,backup/name);prior[name]=True
        else:prior[name]=False
    journal_value=dict(format=1,phase='prepared',source=str(source),runtime=code.name,prior=prior,previous=previous)
    json_write(journal,journal_value)
    try:
        # A power cut must not let the previous bootstrap start partially
        # migrated state. Both entry points stop while the marker exists.
        atomic(root/'start.py',BOOTSTRAP.encode());written.append('start.py')
        atomic(root/'main.py',MAIN_STUB.encode());written.append('main.py')
        atomic(root/'launcher.py',MAIN_STUB.encode());written.append('launcher.py')
        if root!=source:
            incoming={p.relative_to(source).as_posix() for p in files}
            for p in existing_state:
                relative=p.relative_to(root).as_posix()
                if relative not in incoming:
                    # A dormant launcher may have an obsolete schedule/settings.
                    # Preserve separately, never mix it into the current state.
                    snapshot_file(p,root/'.dvk/recovered-launcher-state'/code.name/relative)
                    p.unlink();written.append(relative)
        for p in files:
            relative=p.relative_to(source).as_posix();snapshot_file(p,root/relative);written.append(relative)
        for name,data in configs.items():atomic(root/name,data);written.append(name)
        for name in ('requirements.txt','center-public.json','version.txt','start_mac.sh','start.bat'):atomic(root/name,payload[name]);written.append(name)
        # Code-only layout selection makes self-check use the permanent data root.
        value=dict(format=FORMAT,data_root='.',active=code.name,previous=previous['active'] if previous else None,
                   manager_id=manager,private_work=bool(private),schema=1,activated_at=int(time.time()),started=False)
        json_write(root/'.dvk/layout.json',value)
        # Offline checks may apply additive schema changes. Before business
        # starts, failure must restore the saved state even on in-place updates.
        written.extend(p.relative_to(root).as_posix() for p in existing_state)
        check(root,code)
        atomic(root/'start.py',BOOTSTRAP.encode());written.append('start.py')
        atomic(root/'main.py',MAIN_STUB.encode());written.append('main.py')
        atomic(root/'launcher.py',MAIN_STUB.encode());written.append('launcher.py')
        json_write(root/'data/installed-baseline.json',{n:d.decode('utf-8-sig') for n,d in payload.items()})
        written.append('data/installed-baseline.json')
        json_write(root/'.dvk/last-transition.json',dict(journal_value,phase='activated',old_folder=str(source),safe_to_delete=False))
        journal.unlink()
        # Snapshot is code/state before FIRST business launch. No later data rollback.
        try:cleanup_recovery(root)
        except OSError:json_write(root/'.dvk/cleanup-pending.json',dict(recovery=True))
        return code
    except BaseException:
        for name in reversed(list(dict.fromkeys(written))):
            p=root/name
            if prior.get(name):snapshot_file(backup/name,p)
            elif p.is_file():p.unlink()
        if previous:json_write(root/'.dvk/layout.json',previous)
        elif (root/'.dvk/layout.json').exists():(root/'.dvk/layout.json').unlink()
        json_write(journal,dict(journal_value,phase='rolled_back'))
        # Visible stop remains until recovery acknowledges the failure.
        raise

def cleanup_recovery(root):
    root=Path(root).resolve();target=root/'.dvk/recovery'
    if target.exists():
        if target.is_symlink() or target.resolve().parent!=root/'.dvk':raise ValueError('Unsafe recovery cleanup')
        shutil.rmtree(target)

def recover_prelaunch(root):
    """Recover an interrupted *pre-launch* transaction, never sent business state."""
    root=Path(root).resolve();journal=root/'.dvk/migration-pending.json'
    if not journal.is_file() or journal.is_symlink():raise ValueError('No safe recovery marker')
    record=json.loads(journal.read_text('utf-8'))
    if record.get('format')!=1 or record.get('phase') not in ('prepared','rolled_back'):raise ValueError('Unsafe migration phase')
    current=layout(root) if (root/'.dvk/layout.json').is_file() else None
    if current and current.get('active')==record.get('runtime') and current.get('started'):raise ValueError('Business runtime already started; no data rollback')
    previous=record.get('previous');backup=root/'.dvk/recovery'
    for name,existed in record['prior'].items():
        p=root/name
        if Path(name).is_absolute() or '..' in Path(name).parts or not p.resolve().is_relative_to(root):raise ValueError('Unsafe recovery target')
        if existed and not (backup/name).is_file():raise ValueError('Incomplete recovery snapshot')
    with FileLock(root/'data/startup.lock'),FileLock(root/'data/sender.lock'):
        for name,existed in record['prior'].items():
            p=root/name
            if existed:snapshot_file(backup/name,p)
            elif p.is_file():p.unlink()
        if previous:json_write(root/'.dvk/layout.json',previous)
        elif (root/'.dvk/layout.json').is_file():(root/'.dvk/layout.json').unlink()
        json_write(journal,dict(record,phase='rolled_back'))
    return 'restored_before_business_launch'

def prune(root):
    root=Path(root).resolve();record=layout(root);base=root/'.dvk/runtime';keep={record['active'],record.get('previous')}
    for p in base.iterdir():
        if p.name in keep:continue
        if p.is_symlink() or not re.fullmatch('[0-9a-f]{32}',p.name) or p.resolve().parent!=base:raise ValueError('Unsafe old runtime')
        marker=p/'.dvk-stage.json'
        if marker.is_file():
            expected=dict(format=1,root=hashlib.sha256(str(root).encode()).hexdigest(),runtime=p.name)
            if json.loads(marker.read_text('utf-8'))!=expected:raise ValueError('Unowned runtime stage')
        else:verify_code(p)
        for directory,dirs,files in os.walk(p,followlinks=False):
            if any(linked(Path(directory)/n) for n in dirs+files):raise ValueError('Linked old runtime file')
        shutil.rmtree(p)

def launch(root,code):
    root=Path(root).resolve();record=layout(root);record['started']=True;json_write(root/'.dvk/layout.json',record)
    python=root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    env={**os.environ,'DVK_LAUNCHER_ROOT':str(root),'PYTHONIOENCODING':'utf-8'}
    return subprocess.run([str(python),str(code/'main.py')],cwd=root,env=env).returncode

def run(root,*,updates=True,discover=None,check=verify_environment,launch_fn=launch):
    root=Path(root).resolve()
    if (root/'.dvk/migration-pending.json').exists():raise ValueError('Migration incomplete; refusing stale sender')
    with FileLock(root/'data/startup.lock'):
        record=layout(root);active=runtime(root,record)
        prune(root)
        if updates and not record.get('private_work'):
            from startup_update import latest_bundle,unpack_bundle
            import tempfile
            try:
                release=(discover or latest_bundle)((active/'version.txt').read_text('utf-8-sig').strip())
                if release:
                    tag,raw=release
                    with FileLock(root/'data/sender.lock'),tempfile.TemporaryDirectory(prefix='dvk-unified-') as tmp:
                        bundle=Path(tmp);unpack_bundle(raw,bundle)
                        # The incoming validator owns its current file/schema registry.
                        # Never reject future modules using this release's old allowlist.
                        script=('import sys;from pathlib import Path;import startup_update as u;'
                                'u.prepare_unified_update(Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3])')
                        subprocess.run([sys.executable,'-c',script,str(root),str(bundle),tag],
                                       cwd=bundle,check=True,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
                        active=runtime(root)
                    prune(root)
            except Exception as exc:
                from startup_update import update_failure_report
                update_failure_report(root,active,exc)
                if (root/'.dvk/migration-pending.json').exists():raise
                print('Обновление отложено; данные сохранены ('+type(exc).__name__+').',flush=True)
        try:
            from installation_inventory import retire_known
            retire_known(root)
        except Exception as exc:
            # Never present a stale "safe to remove" list after an incomplete scan.
            json_write(root/'.dvk/legacy-inventory.json',dict(format=1,folders=[],
                       inspection_complete=False,error_type=type(exc).__name__))
        return launch_fn(root,active)

def retire(source,root,*,preserve=True):
    """Preserve a transparent link; never mix or delete old personal files."""
    source,root=Path(source).resolve(),Path(root).resolve()
    if source==root:return
    if read_identity(source)!=read_identity(root):raise ValueError('Old copy identity mismatch')
    alias=('"""This installation has moved; no old business code is loaded."""\n'
           'import runpy\nfrom pathlib import Path\n'
           'root=Path('+repr(str(root))+')\n'
           'if not (root/".dvk/layout.json").is_file():raise SystemExit("Действующая установка недоступна; старая копия не запущена.")\n'
           'runpy.run_path(str(root/"start.py"),run_name="__main__")\n')
    for name in ('main.py','start.py','launcher.py'):
        old=source/name
        if old.is_file():
            backup=root/'.dvk/retired-code'/hashlib.sha256(str(source).encode()).hexdigest()/name
            if not backup.exists():atomic(backup,old.read_bytes())
        atomic(old,alias.encode())
    if not preserve:
        json_write(source/'DVK_FOLDER_MOVED.json',dict(format=1,destination=str(root),ordinary_start_redirected=True,
                   data_copied=False,safe_to_delete=False,note='Old process or unverified process scan; keep this folder.'))
        return
    # No obsolete interpreter is used by the new root. Preserve non-package
    # files, including earlier evidence/backups, before authorising removal.
    baseline=source/'data/installed-baseline.json'
    baseline_files=json.loads(baseline.read_text('utf-8')) if baseline.is_file() else {}
    names=set(baseline_files)
    transition=root/'.dvk/last-transition.json'
    authoritative=transition.is_file() and json.loads(transition.read_text('utf-8')).get('old_folder')==str(source)
    copied={p.relative_to(source).as_posix() for p in personal_paths(source)} if authoritative else set()
    archive=root/'.dvk/recovered-files'/hashlib.sha256(str(source).encode()).hexdigest()
    recovered=[]
    for directory,dirs,files in os.walk(source,followlinks=False):
        if any((Path(directory)/n).is_symlink() for n in dirs):raise ValueError('Old folder contains linked personal data')
        if Path(directory)==source:dirs[:]=[n for n in dirs if n not in ('venv','.venv','__pycache__','.git','.dvk')]
        for filename in files:
            p=Path(directory)/filename;name=p.relative_to(source).as_posix()
            if p.is_symlink():raise ValueError('Old folder contains linked personal file')
            unchanged_code=name in names and p.read_bytes().replace(b'\r\n',b'\n').decode('utf-8-sig',errors='replace')==baseline_files[name].replace('\r\n','\n')
            if (unchanged_code or name in copied or name in ('main.py','start.py','launcher.py','DVK_FOLDER_MOVED.json')
                    or filename.endswith(('.lock','-wal','-shm','-journal','.pyc'))):continue
            snapshot_file(p,archive/name)
            # SQLite backup rewrites pages: compare its logical integrity above;
            # byte equality is required for ordinary unknown user files.
            if p.suffix.lower() not in ('.sqlite','.sqlite3','.db','.session') and digest(p)!=digest(archive/name):raise ValueError('Old personal file copy mismatch')
            recovered.append(name)
    safe=not (source/'.git').exists() and not (source/'.dvk').exists()
    json_write(source/'DVK_FOLDER_MOVED.json',dict(format=1,destination=str(root),ordinary_start_redirected=True,
               data_copied=True,safe_to_delete=safe,recovered_personal_files=recovered,
               note='All application state moved; unknown user files preserved internally.' if safe else 'Extra repository/internal installation requires review.'))
    json_write(root/'.dvk/legacy-folders'/(hashlib.sha256(str(source).encode()).hexdigest()+'.json'),
               dict(folder=str(source),safe_to_delete=safe,recovered_personal_files=recovered))

def latest_legacy_state(source):
    """Follow verified pointer ancestry to the real current data, never a stale hop."""
    source=Path(source).resolve();identity=read_identity(source);seen=set()
    for _ in range(1000):
        if source in seen:raise ValueError('Cycle in legacy installation pointers')
        seen.add(source)
        if (source/'.dvk/layout.json').is_file():return source
        pointer=source/'data/active-installation.json'
        if not pointer.exists():return source
        if linked(pointer):raise ValueError('Linked legacy pointer')
        record=json.loads(pointer.read_text('utf-8'));name=record.get('directory','')
        if record.get('format')!=1 or not isinstance(name,str) or not re.fullmatch(re.escape(source.name)+'_dvk_[0-9a-f]{32}',name):raise ValueError('Invalid legacy pointer chain')
        target=source.parent/name
        if linked(target) or not target.is_dir() or read_identity(target)!=identity:raise ValueError('Legacy pointer changes account identity')
        # Additional hops were not attested by the old parent updater.
        baseline=target/'data/installed-baseline.json'
        if (target/'.dvk/layout.json').is_file():
            from startup_update import load_unified
            load_unified(target).runtime(target)
        elif baseline.is_file():
            import manual_update
            for item,text in json.loads(baseline.read_text('utf-8')).items():
                if item not in manual_update.RUNTIME or item in ('products.json','.env.example') or item.startswith('prompts/'):continue
                file=target/item
                if linked(file) or not file.is_file() or not manual_update.same_text(file.read_bytes(),text.encode('utf-8')):raise ValueError('Changed nested runtime: '+item)
        else:
            import transition_setup
            _,_,profiles=transition_setup.verify_bundle(Path(transition_setup.__file__).parent)
            transition_setup.inspect_source(target,profiles)
        source=target.resolve()
    raise ValueError('Legacy pointer chain too long')


def compatibility_caller(source,candidate):
    """Derive the old launcher from its requested destination, prove ownership.

    Frozen old bootstraps do not provide DVK_LAUNCHER_ROOT. Environment text
    cannot authorise an installation; existing paths, identity and pointer
    ancestry must agree. Resolve the existing stem separately for Windows8.3.
    """
    source,candidate=Path(source).resolve(),Path(candidate).resolve()
    match=re.fullmatch(r'(.+)_dvk_([0-9a-f]{32})',candidate.name)
    if not match:raise ValueError('Unsafe compatibility destination')
    stem=candidate.parent/match[1]
    if linked(stem) or not stem.is_dir():raise ValueError('Compatibility launcher missing')
    caller=stem.resolve()
    if caller.parent!=candidate.parent or read_identity(caller)!=read_identity(source):raise ValueError('Compatibility launcher identity mismatch')
    current=caller;seen=set()
    for _ in range(1000):
        if current==source:return caller
        if current in seen:raise ValueError('Cycle in compatibility ownership')
        seen.add(current)
        pointer=current/'data/active-installation.json'
        if linked(pointer) or not pointer.is_file():break
        record=json.loads(pointer.read_text('utf-8'));name=record.get('directory','')
        child=re.fullmatch(r'(.+)_dvk_([0-9a-f]{32})',name) if isinstance(name,str) else None
        if (record.get('format')!=1 or child is None or Path(name).name!=name
                or (current.parent/child[1]).resolve()!=current):break
        target=current.parent/name
        if linked(target) or not target.is_dir() or read_identity(target)!=read_identity(source):break
        current=target.resolve()
    raise ValueError('Launcher does not own source installation')


def prepare_legacy(source,candidate,payload,*,check=verify_environment):
    """Adapter called by the OLD updater while it still holds its locks.

    Only a hidden, data-free compatibility stub is created at its requested
    sibling path; it removes itself after the old parent finishes launching.
    """
    source,candidate=Path(source).resolve(),Path(candidate).resolve()
    caller=compatibility_caller(source,candidate)
    requested_source=source
    source=latest_legacy_state(source)
    root=canonical_root(caller,source)
    if candidate.exists():raise ValueError('Compatibility destination already exists')
    with contextlib.ExitStack() as locks:
        if source!=requested_source:locks.enter_context(FileLock(source/'data/sender.lock'))
        if root!=caller:locks.enter_context(FileLock(root/'data/startup.lock'))
        if root!=source:locks.enter_context(FileLock(root/'data/sender.lock'))
        code=prepare_code(root,payload)
        activate(root,code,source,check=check)
        retire(source,root)
    # Source is held idle by the calling legacy updater. Known alternatives
    # are retired only when an actual process check confirms they are idle.
    if requested_source not in (source,root):retire(requested_source,root)
    if caller!=root and caller!=source:retire(caller,root)
    from installation_inventory import retire_known
    retire_known(root,source)
    candidate.mkdir()
    if os.name=='nt':
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(candidate),2)
    marker=dict(format=1,root=str(root),caller=str(caller),runtime=code.name,data_free=True)
    json_write(candidate/'DVK_TRANSITION_STUB.json',marker)
    stub=('import json,os,shutil,subprocess\nfrom pathlib import Path\n'
          'here=Path(__file__).resolve().parent\n'
          'r=json.loads((here/"DVK_TRANSITION_STUB.json").read_text("utf-8"))\n'
          'root=Path(r["root"]).resolve(); code=root/".dvk/runtime"/r["runtime"]\n'
          'caller=Path(r["caller"]).resolve()\n'
          'assert r["data_free"] and here.parent==caller.parent and caller.parent==root.parent and not here.is_symlink()\n'
          'python=root/"venv"/("Scripts/python.exe" if os.name=="nt" else "bin/python")\n'
          'p=caller/"data/active-installation.json"\n'
          'if p.exists() and json.loads(p.read_text("utf-8")).get("directory")==here.name:p.unlink()\n'
          'if set(x.name for x in here.iterdir())<={"main.py","DVK_TRANSITION_STUB.json"}:shutil.rmtree(here)\n'
          'raise SystemExit(subprocess.run([str(python),str(code/"main.py")],cwd=root).returncode)\n')
    atomic(candidate/'main.py',stub.encode())
    return code

def canonical_root(caller,source):
    """Recover original launcher through verified pointer ancestry, never date/name alone."""
    caller,source=Path(caller).resolve(),Path(source).resolve()
    if (source/'.dvk/layout.json').is_file():return source
    identity=read_identity(source)
    base=re.sub(r'(?:_dvk_[0-9a-f]{32})+$','',caller.name)
    possible=caller.parent/base
    if possible==caller or not possible.is_dir() or linked(possible):return caller
    if read_identity(possible)!=identity:raise ValueError('Original launcher belongs to another identity')
    current=possible;seen=set()
    for _ in range(1000):
        if current==source:return possible
        if current in seen:raise ValueError('Cycle in legacy launcher pointers')
        seen.add(current);p=current/'data/active-installation.json'
        if not p.is_file() or linked(p):break
        r=json.loads(p.read_text('utf-8'));name=r.get('directory','')
        if r.get('format')!=1 or not isinstance(name,str) or not re.fullmatch(re.escape(current.name)+'_dvk_[0-9a-f]{32}',name):break
        current=current.parent/name
        if linked(current) or not current.is_dir():break
    return caller

def adopt_flat(root,*,check=verify_environment):
    root=Path(root).resolve()
    from manual_update import package_files
    payload={n:(root/n).read_bytes() for n in package_files(root)}
    with FileLock(root/'data/startup.lock'),FileLock(root/'data/sender.lock'):
        code=prepare_code(root,payload);activate(root,code,root,check=check)
    return code
