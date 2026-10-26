"""Local first-install form. Secrets stay on this computer, never in logs."""
import argparse,json,os,re,subprocess,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
FIELDS=(('number','Номер менеджера от руководителя, например 010',False),
        ('TELEGRAM_API_ID','Telegram API ID — получите у руководителя',False),
        ('TELEGRAM_API_HASH','Telegram API Hash — получите у руководителя',True),
        ('BOT_TOKEN','Токен вашего бота из @BotFather',True),
        ('OPENAI_API_KEY','Ключ ИИ — получите у руководителя',True),
        ('PARSER_API_KEY','Ключ судебного поиска — для ДВК Право',True))

def validate(values):
    values={key:str(value).strip() for key,value in values.items()}
    if not re.fullmatch(r'[0-9]{1,12}',values.get('number','')):raise ValueError('Введите номер, который выдал руководитель. Ведущие нули сохраняются.')
    if not values.get('TELEGRAM_API_ID','').isdigit():raise ValueError('Telegram API ID должен состоять из цифр.')
    if not re.fullmatch(r'[a-fA-F0-9]{32}',values.get('TELEGRAM_API_HASH','')):raise ValueError('Проверьте Telegram API Hash: нужны 32 символа, без пробелов.')
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}',values.get('BOT_TOKEN','')):raise ValueError('Проверьте токен BotFather: скопируйте только токен, без текста вокруг.')
    if not values.get('OPENAI_API_KEY'):raise ValueError('Получите ключ ИИ у руководителя и вставьте его.')
    if any(c.isspace() for key in ('OPENAI_API_KEY','PARSER_API_KEY') for c in values.get(key,'')):
        raise ValueError('Ключи вводятся одной строкой, без пробелов и переносов.')
    return values

def save(root,values):
    from file_lock import FileLock
    from transition_setup import validate_public
    from dotenv import set_key
    root=Path(root).resolve();values=validate(values)
    public=json.loads((root/'center-public.json').read_text('utf-8'));validate_public(public)
    with FileLock(root/'data/startup.lock'),FileLock(root/'data/sender.lock'):
        path=root/'.env'
        if path.exists() or (root/'data/active-installation.json').exists():
            raise ValueError('Настройки уже существуют. Они не перезаписаны. Используйте обычный запуск или обратитесь к руководителю.')
        number=values.pop('number')
        values.update(MANAGER_ID='manager_'+number,SESSION_NAME='telegram_sender_manager_'+number,
                      ACCESS_BOT_USERNAME=public['service_bot'],ACCESS_PUBLIC_KEY=public['access_public_key'],SYNC_PUBLIC_KEY=public['sync_public_key'])
        handle,temp=tempfile.mkstemp(prefix='.setup-',dir=root);os.close(handle)
        temporary=Path(temp)
        try:
            for key,value in values.items():set_key(str(temporary),key,value)
            os.chmod(temporary,0o600)
            # An exclusive final creation protects another concurrent installer.
            with path.open('xb') as output:output.write(temporary.read_bytes())
            os.chmod(path,0o600)
        finally:temporary.unlink(missing_ok=True)

def setup_html(token,*,demo=False):
    import html
    rows=[]
    examples={'number':'010','TELEGRAM_API_ID':'12345678'} if demo else {}
    for key,label,secret in FIELDS:
        value=examples.get(key,'пример для инструкции' if demo and secret else '')
        rows.append('<label>'+html.escape(label)+'<input name="'+key+'" type="'+('password' if secret else 'text')+
                    '" value="'+html.escape(value,quote=True)+'" autocomplete="off" '+('required' if key!='PARSER_API_KEY' else '')+'></label>')
    return ('''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ДВК — первая настройка</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f4f9;color:#172333;font:16px Arial,sans-serif}
main{max-width:760px;margin:32px auto;padding:32px 42px;background:white;border-radius:16px;box-shadow:0 12px 40px #18315312}
.brand{font-size:13px;letter-spacing:2px;color:#536880;font-weight:bold}h1{font-size:29px;margin:13px 0}p{line-height:1.5;color:#526171}
label{display:block;margin-top:17px;font-size:15px;font-weight:bold}input{display:block;margin-top:7px;width:100%;padding:11px 13px;font-size:18px;border:1px solid #b5c1d0;border-radius:7px;background:#fcfdff;color:#15273a}
button{width:100%;border:0;border-radius:8px;background:#235fc7;color:white;padding:15px;font-size:18px;font-weight:bold;margin-top:22px;cursor:pointer}
.note{font-size:14px;margin-bottom:0}.step{float:right;color:#536880;font-size:14px}
</style><main><span class="step">Первый запуск</span><div class="brand">ДВК · TELEGRAM AI SENDER</div>
<h1>Настройка программы</h1><p>Заполните один раз. Дальше запускайте программу привычным файлом запуска.</p>
<form method="post" autocomplete="off"><input type="hidden" name="csrf" value="'''+token+'">'+''.join(rows)+'''
<p class="note">Токен создайте в @BotFather. Ключ ИИ получите у руководителя.<br>Ключ судебного поиска обязателен для ДВК Право. Данные сохраняются на этом компьютере.</p>
<button type="submit">Сохранить и запустить</button></form></main></html>''').encode('utf-8')

def wizard(root=ROOT,*,demo=False,open_browser=True,ready=None):
    import hmac,secrets,webbrowser
    from http.server import HTTPServer,BaseHTTPRequestHandler
    from socketserver import TCPServer
    from urllib.parse import parse_qs
    token=secrets.token_urlsafe(32);route='/setup/'+token;state={'saved':False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,body,status=200):
            self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('X-Frame-Options','DENY')
            self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if self.path!=route:self.respond(b'Not found',404);return
            self.respond(setup_html(token,demo=demo))
        def do_POST(self):
            expected='http://127.0.0.1:'+str(self.server.server_port)
            if self.path!=route or self.headers.get('Origin')!=expected:
                self.respond(b'Request rejected',403);return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<16384:raise ValueError('invalid')
                fields=parse_qs(self.rfile.read(length).decode('utf-8'),keep_blank_values=True)
                if not hmac.compare_digest(fields.pop('csrf',[''])[0],token):raise ValueError('invalid')
                values={key:fields.get(key,[''])[0] for key,_,_ in FIELDS}
                if not demo:save(root,values)
                self.respond('<html lang="ru"><meta charset="utf-8"><title>ДВК — готово</title><body style="font:20px Arial;max-width:720px;margin:100px auto;line-height:1.6"><h1>Настройка завершена</h1><p>Вернитесь в окно программы. Сейчас появится QR-код для входа в Telegram.</p><p>На телефоне: Настройки → Устройства → Подключить устройство.</p><p>После входа откройте своего бота и дождитесь разрешения руководителя.</p></body></html>'.encode('utf-8'))
                state['saved']=True
            except ValueError as exc:
                import html
                self.respond(('<html lang="ru"><meta charset="utf-8"><h2>Проверьте поля</h2><p>'+html.escape(str(exc))+'</p><p>Нажмите кнопку браузера «Назад», исправьте поле и сохраните снова.</p></html>').encode('utf-8'),400)
            except Exception as exc:
                self.respond(('Ошибка '+type(exc).__name__+'. Настройка не завершена. Обратитесь к руководителю.').encode('utf-8'),500)
    class LocalServer(HTTPServer):
        def server_bind(self):
            # HTTPServer's reverse-DNS lookup can stall localhost startup on
            # Macs. This loopback-only wizard never needs a DNS server name.
            TCPServer.server_bind(self)
            self.server_name='127.0.0.1'
            self.server_port=self.server_address[1]
    with LocalServer(('127.0.0.1',0),Handler) as server:
        server.timeout=1;url='http://127.0.0.1:'+str(server.server_port)+route
        if ready:ready(url)
        if open_browser:webbrowser.open(url)
        print('Откройте страницу настройки: '+url,flush=True)
        import time
        deadline=time.monotonic()+1800
        while not state['saved'] and time.monotonic()<deadline:server.handle_request()
    return state['saved'] and not demo

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--demo',action='store_true');parser.add_argument('--launch',action='store_true');args=parser.parse_args()
    if wizard(demo=args.demo) and args.launch:
        return subprocess.run([sys.executable,'-u','start.py'],cwd=ROOT).returncode
    return 0

if __name__=='__main__':raise SystemExit(main())
