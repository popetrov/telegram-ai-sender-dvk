"""Owner-requested interactive startup; diagnostic file contains only stages/error types."""
import asyncio,datetime,functools,json,os,runpy,sys,urllib.error,urllib.request
from pathlib import Path
REPORT=None
def record(stage,exc=None,**fields):
    entry=dict(time=datetime.datetime.now().astimezone().isoformat(timespec='seconds'),stage=stage,**fields)
    if exc is not None:
        entry['error_type']=type(exc).__name__
        if isinstance(exc,urllib.error.HTTPError):entry['http_status']=exc.code
        reason=getattr(exc,'reason',None)
        if reason is not None:entry['reason_type']=type(reason).__name__
        code=getattr(reason,'verify_code',None)
        if type(code) is int:entry['certificate_code']=code
        number=getattr(reason,'errno',None)
        if type(number) is int:entry['network_errno']=number
    if REPORT is not None:
        with REPORT.open('a',encoding='utf-8') as output:output.write(json.dumps(entry,ensure_ascii=False)+'\n')
def wrap(module,name,stage):
    original=getattr(module,name)
    if asyncio.iscoroutinefunction(original):
        @functools.wraps(original)
        async def call(*args,**kwargs):
            record(stage+'.begin')
            try:result=await original(*args,**kwargs)
            except Exception as exc:record(stage+'.failed',exc);raise
            record(stage+'.ok');return result
    else:
        @functools.wraps(original)
        def call(*args,**kwargs):
            record(stage+'.begin')
            try:result=original(*args,**kwargs)
            except Exception as exc:record(stage+'.failed',exc);raise
            record(stage+'.ok');return result
    setattr(module,name,call)
def network_check():
    endpoints=[('github',urllib.request.Request('https://api.github.com/repos/popetrov/telegram-ai-sender-dvk/releases/latest',headers={'User-Agent':'DVK-startup-check'}))]
    try:
        public=json.loads(Path('center-public.json').read_text('utf-8'))
        from app_profile_broker import validate_url
        url=validate_url(public['bootstrap_url'])
        endpoints.append(('settings_service',urllib.request.Request(url,data=json.dumps(dict(action='app_poll',request_id='0'*32)).encode(),headers={'Content-Type':'application/json'})))
    except Exception as exc:record('settings_service.config',exc)
    for name,request in endpoints:
        try:
            with urllib.request.urlopen(request,timeout=10) as response:response.read(256)
            record(name+'.reachable')
        except Exception as exc:record(name+'.unavailable',exc)
def child(active):
    active=Path(active).resolve();os.chdir(active);sys.path.insert(0,str(active))
    record('interactive_start',python=sys.version.split()[0])
    network_check()
    import app_profile_client as profiles
    import app_profile_broker as broker
    from telethon import TelegramClient
    for name,stage in [('fresh','first_setup'),('accept','verify_settings'),('probe_application_pair','verify_telegram_application'),('migrate','save_application_settings')]:wrap(profiles,name,stage)
    for name in ('register','poll'):wrap(broker,name,'settings_'+name)
    wrap(TelegramClient,'connect','telegram_connection')
    wrap(TelegramClient,'__call__','telegram_request')
    sys.argv=[str(active/'main.py')]
    try:runpy.run_path(str(active/'main.py'),run_name='__main__')
    except KeyboardInterrupt:record('stopped_by_user')
    except BaseException as exc:
        record('startup_stopped',exc)
        print('Запуск остановлен. Передайте владельцу файл DVK_START_REPORT.txt.',flush=True)
        return 1
    return 0
def main():
    global REPORT
    here=Path(__file__).resolve().parent
    REPORT=here/'DVK_START_REPORT.txt'
    if len(sys.argv)>1 and sys.argv[1]=='--dvk-child':return child(sys.argv[2])
    os.chdir(here);sys.path.insert(0,str(here))
    record('launcher.begin')
    try:
        import startup_update
        def launch(active):
            import subprocess
            python=Path(active)/'venv/Scripts/python.exe'
            if not python.is_file():python=Path(sys.executable)
            loader="import os; from pathlib import Path; f=Path(os.environ['DVK_DIAG_SELF']); s=f.read_text(encoding='utf-8-sig'); c=s.split(chr(10)+'# DVK_DIAG_PYTHON'+chr(10),1)[1]; exec(compile(c,str(f),'exec'),{'__name__':'__main__','__file__':str(f)})"
            result=subprocess.run([str(python),'-u','-c',loader,'--dvk-child',str(active)],cwd=active,env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUNBUFFERED':'1'})
            record('sender_exit',exit_code=result.returncode);return result.returncode
        # Use the installed package. An unavailable GitHub must not block this check.
        return startup_update.run(here,updates=False,launch=launch)
    except BaseException as exc:
        record('launcher.failed',exc)
        print('Запуск не завершён. Передайте владельцу DVK_START_REPORT.txt.',flush=True)
        return 1
if __name__=='__main__':raise SystemExit(main())
