"""Fresh installation packages with public center config and no owner secrets."""
import hashlib,json,stat,zipfile
from pathlib import Path
from manual_update import verify_package

COMMON={'install_manager.py','setup_wizard.py','center-public.json','python-downloads.json','INSTALLATION.md'}
GUIDE='Инструкция по установке.pdf'
PLATFORM={
    'windows':{'install.bat','install_windows.ps1'},
    'macos':{'install_unix.sh','install_mac.command','start_mac.command'},
    'linux':{'install_unix.sh','install_linux.sh','start_linux.sh'},
}

def build(root,package,output,platform):
    root,package,output=Path(root),Path(package),Path(output)
    _,payload=verify_package(package,hashlib.sha256(package.read_bytes()).hexdigest())
    payload.update({name:(root/name).read_bytes() for name in COMMON|PLATFORM[platform]})
    payload[GUIDE]=(root/'docs/install/Установка Telegram AI Sender.pdf').read_bytes()
    payload['data/installed-baseline.json']=json.dumps({name:data.decode('utf-8-sig')
        for name,data in verify_package(package,hashlib.sha256(package.read_bytes()).hexdigest())[1].items()},ensure_ascii=False).encode('utf-8')
    if platform=='windows':payload['install_windows.ps1']=b'\xef\xbb\xbf'+payload['install_windows.ps1'].removeprefix(b'\xef\xbb\xbf')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        for name,data in sorted(payload.items()):
            info=zipfile.ZipInfo(name);info.compress_type=zipfile.ZIP_DEFLATED;info.create_system=3
            info.external_attr=(stat.S_IFREG|(0o755 if name.endswith(('.sh','.command')) else 0o644))<<16
            archive.writestr(info,data)
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest+'  '+output.name+'\n',encoding='ascii')
    return digest
