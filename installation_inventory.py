"""Only validated sibling installations; never scan arbitrary home folders."""
import hashlib,json,os,re,subprocess
from pathlib import Path

def processes():
    try:
        if os.name=='nt':
            raw=subprocess.check_output(['powershell','-NoProfile','-Command',
                "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.Name -match '^python(w)?[.]exe$' } | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress"],
                encoding='utf-8',errors='replace',stderr=subprocess.DEVNULL,timeout=15)
            rows=json.loads(raw) if raw.strip() else [];rows=rows or []
            return (rows if isinstance(rows,list) else [rows]),True
        raw=subprocess.check_output(['ps','-eo','pid=,args='],text=True,timeout=10)
        return [dict(ProcessId=int(line.split(None,1)[0]),CommandLine=line.split(None,1)[1]) for line in raw.splitlines() if len(line.split(None,1))==2],True
    except (OSError,ValueError,subprocess.SubprocessError):return [],False

def known(root,source=None):
    from unified_install import read_identity
    root=Path(root).resolve();manager,key=read_identity(root);paths=[]
    if source and Path(source).resolve()!=root:paths.append(Path(source).resolve())
    pattern=re.compile(re.escape(root.name)+r'(?:_dvk_[0-9a-f]{32})+')
    for p in root.parent.iterdir():
        if len(paths)>=100:break
        if p.is_dir() and not p.is_symlink() and pattern.fullmatch(p.name) and p.resolve()!=root:paths.append(p.resolve())
    rows,coverage=processes();result=[];skip={os.getpid(),os.getppid()}
    # A relative `python main.py` has no trustworthy folder in WMI/ps.
    # Such a process is an explicit coverage gap, never proof of an idle copy.
    for row in rows:
        command=row.get('CommandLine') or ''
        if row['ProcessId'] in skip:continue
        if any(n in command for n in ('main.py','start.py','launcher.py')):
            if not any(str(p).casefold() in command.casefold() for p in [root,*paths]):coverage=False
    for p in dict.fromkeys(paths):
        try:
            own_manager,own_key=read_identity(p)
            if own_manager!=manager or not key or own_key!=key:continue
            running=[]
            for row in rows:
                command=row.get('CommandLine') or ''
                if row['ProcessId'] not in skip and str(p).casefold() in command.casefold() and any(n in command for n in ('main.py','start.py','launcher.py')):running.append(row['ProcessId'])
            text=(p/'telegram_client.py').read_text('utf-8-sig') if (p/'telegram_client.py').is_file() else ''
            result.append(dict(folder=str(p),folder_id=hashlib.sha256(str(p).encode()).hexdigest(),
                same_account_key=True,running_pids=running,process_check_complete=coverage,
                guarded='ProtectedTelegramClient' in text,has_internal_layout=(p/'.dvk/layout.json').exists()))
        except (OSError,ValueError,KeyError):continue
    return result

def retire_known(root,source=None):
    from unified_install import retire,json_write
    root=Path(root).resolve();results=known(root,source)
    for row in results:
        if row['running_pids'] or not row['process_check_complete'] or row['has_internal_layout']:
            if not row['has_internal_layout']:retire(row['folder'],root,preserve=False)
            row['safe_to_delete']=False;row['reason']='running_or_unverified';continue
        retire(row['folder'],root)
        info=json.loads((Path(row['folder'])/'DVK_FOLDER_MOVED.json').read_text('utf-8'))
        row['safe_to_delete']=info['safe_to_delete'];row['reason']='redirected_and_preserved'
    json_write(root/'.dvk/legacy-inventory.json',dict(format=1,folders=results))
    return results
