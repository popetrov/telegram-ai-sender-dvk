"""Build one-file offline safety patch. No personal data is packaged."""
import base64
import json
from pathlib import Path
import subprocess
import zipfile
from install_security_fix import FILES,digest


def payload(root):
    root=Path(root)
    files={name:base64.b64encode((root/name).read_bytes()).decode() for name in FILES}
    accepted={}
    for name in FILES:
        accepted[name]=[]
        for tag in ('v2.0.27','v2.0.28','v2.0.29'):
            result=subprocess.run(['git','show',tag+':'+name],cwd=root,capture_output=True)
            if result.returncode==0:accepted[name].append(digest(result.stdout))
    return dict(format=1,version='2.0.30',files=files,accepted=accepted)


def build(root,output):
    root,output=Path(root),Path(output)
    encoded=base64.b64encode(json.dumps(payload(root),sort_keys=True).encode()).decode()
    source='SECURITY_PAYLOAD = '+repr(encoded)+'\n'+(root/'install_security_fix.py').read_text('utf-8')
    wrapper='''@echo off
chcp 65001 >nul
setlocal
set "PYTHONIOENCODING=utf-8"
set "DVK_SECURITY_SELF=%~f0"
set "DVK_SECURITY_PY=python"
if exist "%~dp0venv\\Scripts\\python.exe" set "DVK_SECURITY_PY=%~dp0venv\\Scripts\\python.exe"
if exist "%~dp0.venv\\Scripts\\python.exe" set "DVK_SECURITY_PY=%~dp0.venv\\Scripts\\python.exe"
"%DVK_SECURITY_PY%" -c "import os; from pathlib import Path; f=Path(os.environ['DVK_SECURITY_SELF']); s=f.read_text(encoding='utf-8-sig'); c=s.split(chr(10)+'# DVK_SECURITY_PYTHON'+chr(10),1)[1]; exec(compile(c,str(f),'exec'),{'__name__':'__main__','__file__':str(f)})"
echo.
pause
exit /b
# DVK_SECURITY_PYTHON
'''
    raw=(wrapper+source).replace('\r\n','\n').replace('\n','\r\n').encode('utf-8')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('DVK_INSTALL_PROTECTION.bat',raw)
    return raw


if __name__=='__main__':
    import sys
    build(Path(__file__).resolve().parent,Path(sys.argv[1]))
