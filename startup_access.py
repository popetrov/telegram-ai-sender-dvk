"""Fresh signed center assignment before discovery; unchanged offline fallback."""
import asyncio,base64,json,os,sqlite3,subprocess,sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

class StartupIdentityError(RuntimeError):pass
class StartupNotEnrolled(RuntimeError):pass

def identity(root,manager):
 from unified_install import owned_directory
 try:owned_directory(root,root/'data')
 except ValueError as exc:raise StartupIdentityError('Unsafe data directory') from exc
 from telegram_app_migrate import env_values
 if (root/'.env').is_symlink():raise StartupIdentityError('Unsafe environment file')
 values=env_values((root/'.env').read_text('utf-8-sig'))
 path=root/'data/local.sqlite'
 if path.is_symlink():raise StartupIdentityError('Unsafe identity database')
 with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
  saved=dict(db.execute("SELECT key,value FROM local_settings WHERE key IN ('manager_id','account_id')"))
 if values.get('MANAGER_ID')!=manager or (saved.get('manager_id') and saved['manager_id']!=manager):
  raise StartupIdentityError('Manager binding mismatch')
 if not saved.get('account_id'):raise StartupNotEnrolled('Initial Telegram enrollment is not complete')
 try:account=int(saved.get('account_id','0'))
 except (ValueError,TypeError):raise StartupIdentityError('Invalid account identity') from None
 if account<=0 or saved.get('manager_id')!=manager or values.get('MANAGER_ID')!=manager:
  raise StartupIdentityError('Manager/account binding mismatch')
 return values,account

def prepare(root,manager):
 from app_profile_client import public_config
 from access_lease import LeaseStore
 values,account=identity(root,manager);public=public_config(root)
 for path in (root/'data/access_lease.json',root/'data/access_lease.json.tmp'):
  if path.is_symlink():raise StartupIdentityError('Unsafe permission cache')
 store=LeaseStore(root/'data/access_lease.json',base64.b64decode(public['access_public_key'],validate=True),f'{manager}:{account}')
 return values,account,public,store

def cached(store,*,error_type=None):
 try:
  payload=store.require_access()
  return dict(format=1,state='cached',expires_at=payload['expires_at'],error_type=error_type)
 except Exception:
  # Distinguish a signed revocation from a missing/expired permission, never erase either.
  try:
   p=store._verify(json.loads(store.path.read_text('utf-8')))
   if p['allowed'] is False:return dict(format=1,state='revoked',error_type=error_type)
  except Exception:pass
  return dict(format=1,state='unavailable',error_type=error_type)

async def refresh(root,manager,*,client_factory=None):
 root=Path(root).resolve()
 from app_profile_client import memory_session,check_account
 from access_runtime import refresh as refresh_lease
 from account_guard import claim_account,release_account
 values,account,public,store=prepare(root,manager)
 client=None;claimed=False
 try:
  try:claim_account(account);claimed=True
  except RuntimeError as exc:raise StartupIdentityError('Account already running') from exc
  if client_factory is None:
   from safe_telegram import ProtectedTelegramClient
   client_factory=ProtectedTelegramClient
  client=client_factory(memory_session(root,values),int(values['TELEGRAM_API_ID']),values['TELEGRAM_API_HASH'],connection_retries=0,request_retries=0,flood_sleep_threshold=0)
  async with asyncio.timeout(35):
   await client.connect()
   if not await client.is_user_authorized():raise RuntimeError('Existing session is not authorized')
   me=await client.get_me()
   if me.id!=account:raise StartupIdentityError('Telegram account mismatch')
   try:check_account(root,me.id)
   except Exception as exc:raise StartupIdentityError('Approved account binding mismatch') from exc
   bot=await client.get_entity(public['service_bot'])
   if not getattr(bot,'bot',False) or (getattr(bot,'username','') or '').casefold()!=public['service_bot'].lstrip('@').casefold():
    raise StartupIdentityError('Service bot mismatch')
   # Only the existing /lease protocol. No business bot, clients, sync queue or login flow.
   await refresh_lease(client,bot,store)
   payload=store._verify(json.loads(store.path.read_text('utf-8')))
   return dict(format=1,state='fresh' if payload['allowed'] else 'revoked',expires_at=payload['expires_at'])
 except StartupIdentityError:raise
 except Exception as exc:return cached(store,error_type=type(exc).__name__)
 finally:
  if client is not None:
   try:
    async with asyncio.timeout(4):await client.disconnect()
   except Exception:pass
  if claimed:release_account()

def before_updates(root,active,manager):
 """Isolated bounded process prevents temporary Telegram state leaking into main()."""
 from file_lock import FileLock
 from telegram_app_migrate import say
 root,active=Path(root),Path(active)
 say('Проверяю свежее назначение центра…','Checking fresh center assignment...')
 python=root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
 with FileLock(root/'data/sender.lock'):
  try:
   result=subprocess.run([str(python),str(active/'startup_access.py'),str(root),manager],cwd=root,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=45,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
   data=json.loads(result.stdout.strip().splitlines()[-1])
   if data.get('format')!=1 or data.get('state') not in {'fresh','cached','revoked','unavailable','identity_error','unenrolled'}:raise ValueError('Invalid startup result')
  except (OSError,subprocess.TimeoutExpired,ValueError,IndexError):
   # Child may have saved a response just before interruption. The actual signed file
   # remains the only authority; the normal application verifies it before any work.
   try:data=cached(prepare(root,manager)[3],error_type='StartupTransportUnavailable')
   except StartupIdentityError:raise
   except Exception:data=dict(format=1,state='unavailable')
 if data['state']=='identity_error':raise StartupIdentityError('Аккаунт или менеджер не совпадает с установленной программой. Данные сохранены.')
 if data['state']=='fresh':say('Назначение центра получено. Проверяю обновления…','Center assignment received. Checking updates...')
 elif data['state']=='cached':
  expires=datetime.fromtimestamp(data['expires_at'],ZoneInfo('Europe/Saratov')).strftime('%d.%m.%Y %H:%M')
  say('Центр сейчас недоступен. Проверяю обновления по сохранённому назначению. Доступ действует до '+expires+' по Саратову; срок не продлён.','Center unavailable. Using cached assignment until '+expires+' Saratov time; expiry unchanged.')
 elif data['state']=='revoked':say('Центральный доступ отозван. Работа с клиентами запрещена.','Central access revoked. Client work is prohibited.')
 elif data['state']=='unenrolled':say('Первый вход: проверка центра продолжится после обычной авторизации Telegram.','Initial setup: center check continues after normal Telegram sign-in.')
 else:say('Свежее назначение центра не получено. Использую сохранённое только при действующей подписи и сроке; без разрешения работа с клиентами запрещена.','Fresh center assignment unavailable. Only a valid signed, unexpired permission permits client work.')
 return data

if __name__=='__main__':
 try:result=asyncio.run(refresh(Path(sys.argv[1]),sys.argv[2]))
 except StartupNotEnrolled:result=dict(format=1,state='unenrolled')
 except StartupIdentityError:result=dict(format=1,state='identity_error')
 except Exception as exc:result=dict(format=1,state='unavailable',error_type=type(exc).__name__)
 print(json.dumps(result,separators=(',',':')),flush=True)
