"""Company/account-bound data-only dialogue packages; independent of core releases."""
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import urllib.request
import zipfile

from access_lease import AccessDenied
from dialogue_policy import DialoguePolicy

LIMIT = 256 * 1024
ASSET_ROOT = 'https://github.com/popetrov/telegram-ai-sender-dvk/releases/download/'


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',value):
        raise ValueError('Некорректный идентификатор компании или бизнеса')
    return value


def version(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{1,6}\.\d{1,6}\.\d{1,6}',value):
        raise ValueError('Версия пакета: например 1.0.0')
    return tuple(map(int,value.split('.')))


def validate_route(route):
    if not isinstance(route,dict) or set(route)!={'company_id','business_id','version','url','sha256','revision'}:
        raise ValueError('Invalid business update route')
    identifier(route['company_id']);identifier(route['business_id']);version(route['version'])
    if (not isinstance(route['url'],str) or not re.fullmatch(re.escape(ASSET_ROOT)+r'business-[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.zip',route['url'])
            or not isinstance(route['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',route['sha256'])
            or type(route['revision']) is not int or route['revision']<1):
        raise ValueError('Некорректный адрес, контрольная сумма или ревизия бизнес-пакета')
    return route


class CompanyPackages:
    def __init__(self,store):
        self.store=store
        with store.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS business_package_versions (
                    company_id TEXT NOT NULL,business_id TEXT NOT NULL,version TEXT NOT NULL,
                    url TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(company_id,business_id,version));
                CREATE TABLE IF NOT EXISTS business_package_channels (
                    company_id TEXT NOT NULL,business_id TEXT NOT NULL,version TEXT NOT NULL,
                    revision INTEGER NOT NULL,department TEXT,company_name TEXT,PRIMARY KEY(company_id,business_id));
                CREATE TABLE IF NOT EXISTS business_package_members (
                    account_id INTEGER NOT NULL,company_id TEXT NOT NULL,business_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(account_id,business_id));
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(business_package_channels)')}
            for column in ('department','company_name'):
                if column not in columns:db.execute('ALTER TABLE business_package_channels ADD COLUMN '+column+' TEXT')
            if 'enabled' not in {r[1] for r in db.execute('PRAGMA table_info(business_package_members)')}:
                db.execute('ALTER TABLE business_package_members ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')

    def publish(self,company,business,release,url,checksum):
        route=validate_route(dict(company_id=company,business_id=business,version=release,url=url,sha256=checksum,revision=1))
        with self.store.connection() as db:
            if not db.execute('SELECT 1 FROM companies WHERE id=?',(company,)).fetchone():
                raise ValueError('Компания не найдена')
            current=db.execute('SELECT * FROM business_package_channels WHERE company_id=? AND business_id=?',(company,business)).fetchone()
            old=db.execute('SELECT * FROM business_package_versions WHERE company_id=? AND business_id=? AND version=?',(company,business,release)).fetchone()
            if old:
                if old['url']!=url or old['sha256']!=checksum:raise ValueError('Нельзя заменить содержимое существующей версии')
                if current and current['version']==release:return
            if current and version(release)<=version(current['version']):
                raise ValueError('Для возврата используйте package_rollback; данные не откатываются')
            db.execute('INSERT OR IGNORE INTO business_package_versions VALUES (?,?,?,?,?)',(company,business,release,url,checksum))
            db.execute('INSERT INTO business_package_channels(company_id,business_id,version,revision) VALUES (?,?,?,1) ON CONFLICT(company_id,business_id) '
                       'DO UPDATE SET version=excluded.version,revision=revision+1',(company,business,release))

    def assign(self,account,business,enabled=True):
        identifier(business)
        with self.store.connection() as db:
            row=db.execute('SELECT company_id FROM managers WHERE account_id=?',(account,)).fetchone()
            if not row or not row['company_id']:raise ValueError('Сначала подключите менеджера к компании')
            if enabled:
                channel=db.execute('SELECT * FROM business_package_channels WHERE company_id=? AND business_id=?',(row['company_id'],business)).fetchone()
                if not channel:
                    raise ValueError('У компании ещё нет такого бизнес-пакета')
                if channel['department']:
                    card=db.execute('SELECT department,company_name FROM manager_directory WHERE account_id=?',(account,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE name='manager_directory'").fetchone() else None
                    if card is None or any(str(card[k]).strip().casefold()!=channel[k].strip().casefold() for k in ('department','company_name')):
                        raise ValueError('Менеджер не относится к организации и компании этого пакета')
            db.execute('INSERT INTO business_package_members VALUES (?,?,?,?) ON CONFLICT(account_id,business_id) '
                       'DO UPDATE SET company_id=excluded.company_id,enabled=excluded.enabled',(account,row['company_id'],business,int(enabled)))

    def rollback(self,company,business,release):
        identifier(company);identifier(business);version(release)
        with self.store.connection() as db:
            if not db.execute('SELECT 1 FROM business_package_versions WHERE company_id=? AND business_id=? AND version=?',(company,business,release)).fetchone():
                raise ValueError('Компания не выпускала такую версию пакета')
            db.execute('UPDATE business_package_channels SET version=?,revision=revision+1 WHERE company_id=? AND business_id=?',(release,company,business))

    def audience(self,company,business,department,company_name):
        identifier(company);identifier(business)
        if not all(isinstance(v,str) and 0<len(v.strip())<=100 for v in (department,company_name)):
            raise ValueError('Укажите организацию и компанию из внутренней таблицы')
        with self.store.connection() as db:
            if db.execute('UPDATE business_package_channels SET department=?,company_name=?,revision=revision+1 '
                          'WHERE company_id=? AND business_id=?',(department.strip(),company_name.strip(),company,business)).rowcount!=1:
                raise ValueError('Сначала зарегистрируйте пакет компании')

    def routes(self,account,subject):
        with self.store.connection() as db:
            manager=db.execute('SELECT company_id FROM managers WHERE account_id=? AND subject=?',(account,subject)).fetchone()
            if not manager:return []
            company=manager['company_id']
            rows=db.execute('''SELECT v.*,c.revision,c.department,c.company_name FROM business_package_channels c
                JOIN business_package_versions v ON v.company_id=c.company_id AND v.business_id=c.business_id AND v.version=c.version
                WHERE c.company_id=? ORDER BY c.business_id LIMIT 8''',(company,)).fetchall()
            members={r[0]:bool(r[1]) for r in db.execute('SELECT business_id,enabled FROM business_package_members WHERE account_id=? AND company_id=?',(account,company))}
            card=None
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_directory'").fetchone():
                card=db.execute('SELECT department,company_name FROM manager_directory WHERE account_id=?',(account,)).fetchone()
        selected=[]
        for row in rows:
            if members.get(row['business_id']) is False:continue
            matches=(card is not None and all(str(card[k]).strip().casefold()==row[k].strip().casefold() for k in ('department','company_name'))) if row['department'] else members.get(row['business_id'],False)
            if matches:selected.append({k:row[k] for k in ('company_id','business_id','version','url','sha256','revision')})
        return selected

    def disabled(self,account,subject):
        with self.store.connection() as db:
            return [r[0] for r in db.execute('SELECT b.business_id FROM business_package_members b JOIN managers m '
                'ON m.account_id=b.account_id AND m.company_id=b.company_id WHERE m.account_id=? AND m.subject=? AND b.enabled=0',
                (account,subject))]


def build_package(company,business,release,profile,output):
    identifier(company);identifier(business);version(release)
    value=json.loads(Path(profile).read_text('utf-8-sig'));policy=DialoguePolicy(value)
    if policy.business_id!=business:raise ValueError('Профиль относится к другому бизнесу')
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True).encode('utf-8')
    manifest=dict(format=1,core_api=1,company_id=company,business_id=business,version=release,
                  profile_sha256=hashlib.sha256(raw).hexdigest())
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps(manifest,sort_keys=True))
        archive.writestr('profile.json',raw)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def verify_package(raw,route):
    validate_route(route)
    if len(raw)>LIMIT or hashlib.sha256(raw).hexdigest()!=route['sha256']:raise ValueError('Business package checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries=archive.infolist()
        if (len(entries)!=2 or {i.filename for i in entries}!={'manifest.json','profile.json'}
                or sum(i.file_size for i in entries)>LIMIT or any(stat.S_ISLNK(i.external_attr>>16) for i in entries)):
            raise ValueError('Business packages may contain only a manifest and dialogue profile')
        manifest=json.loads(archive.read('manifest.json'));profile=archive.read('profile.json')
    if (manifest.get('format')!=1 or manifest.get('core_api')!=1
            or any(manifest.get(k)!=route[k] for k in ('company_id','business_id','version'))
            or manifest.get('profile_sha256')!=hashlib.sha256(profile).hexdigest()):
        raise ValueError('Business package identity or compatibility mismatch')
    policy=DialoguePolicy(json.loads(profile))
    if policy.business_id!=route['business_id']:raise ValueError('Wrong business policy')
    return profile


def fetch_package(url):
    request=urllib.request.Request(url,headers={'User-Agent':'DVK-business-package'})
    with urllib.request.urlopen(request,timeout=10) as response:raw=response.read(LIMIT+1)
    if len(raw)>LIMIT:raise ValueError('Business package too large')
    return raw


def verified_routes(store):
    grant=store.require_access()
    routes=grant.get('business_packages',[])
    disabled=grant.get('business_packages_disabled',[])
    if not isinstance(disabled,list) or any(not isinstance(v,str) for v in disabled):raise ValueError('Invalid disabled businesses')
    if not isinstance(routes,list) or len(routes)>8:raise ValueError('Invalid business assignment')
    seen=set()
    for route in routes:
        validate_route(route)
        if route['company_id']!=grant.get('company_id') or route['business_id'] in seen or route['business_id'] in disabled:
            raise ValueError('Wrong company or duplicate business assignment')
        seen.add(route['business_id'])
    return routes


def package_path(root,route):
    directory=Path(root)/'data/business-packages'/route['company_id']/route['business_id']
    for path in (Path(root)/'data',directory.parent.parent,directory.parent,directory):
        if path.is_symlink():raise ValueError('Unsafe business package directory')
    return directory/(route['version']+'-'+route['sha256']+'.zip')


def sync_packages(root,store,fetch=fetch_package):
    errors=[]
    for route in verified_routes(store):
        try:
            path=package_path(root,route)
            if path.is_symlink():raise ValueError('Unsafe package file')
            if path.exists():verify_package(path.read_bytes(),route)
            else:
                profile_raw=fetch(route['url']);verify_package(profile_raw,route)
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_suffix('.tmp')
                if temporary.is_symlink():raise ValueError('Unsafe package temporary file')
                temporary.write_bytes(profile_raw)
                temporary.replace(path)
            marker=path.parent/'assigned'
            if marker.is_symlink():raise ValueError('Unsafe assignment marker')
            marker.write_text('1',encoding='ascii')
        except Exception as exc:
            errors.append((route['business_id'],type(exc).__name__))
    return errors


def load_policy(root,store,business,builtin):
    if business in store.require_access().get('business_packages_disabled',[]):
        raise AccessDenied('Владелец отключил бизнес-пакет. Автодиалоги приостановлены.')
    routes=[r for r in verified_routes(store) if r['business_id']==business]
    if not routes:
        grant=store.require_access();company=grant.get('company_id')
        if company is None:return DialoguePolicy.load(builtin)  # Old center/explicit pilot compatibility.
        identifier(company);identifier(business)
        directory=package_path(root,dict(company_id=company,business_id=business,version='0.0.0',sha256='0'*64)).parent
        if (directory/'assigned').exists() or any(directory.glob('*.zip')):
            raise AccessDenied('Владелец отключил бизнес-пакет. Автодиалоги приостановлены.')
        # Pin the pre-existing rules: a future common core must not change an unassigned company.
        legacy=directory/'legacy-profile.json'
        if legacy.is_symlink():raise ValueError('Unsafe legacy policy')
        if not legacy.exists():
            raw=Path(builtin).read_bytes();DialoguePolicy(json.loads(raw))
            directory.mkdir(parents=True,exist_ok=True)
            temporary=legacy.with_suffix('.tmp')
            if temporary.is_symlink():raise ValueError('Unsafe legacy policy temporary file')
            temporary.write_bytes(raw);temporary.replace(legacy)
        return DialoguePolicy.load(legacy)
    route=routes[0];path=package_path(root,route)
    if path.is_symlink() or not path.is_file():raise AccessDenied('Бизнес-пакет ещё не получен. Перезапустите программу после проверки связи с центром.')
    policy=DialoguePolicy(json.loads(verify_package(path.read_bytes(),route)))
    policy.managed_package=True
    return policy


def register_admin(application,packages,admin_id):
    from telegram.ext import CommandHandler
    async def command(update,context):
        if update.effective_chat.type!='private' or update.effective_user.id!=admin_id:return
        name=(update.effective_message.text or '').split()[0].split('@')[0]
        args=context.args
        try:
            if name=='/package_release' and len(args)==5:packages.publish(*args)
            elif name in ('/package_assign','/package_unassign') and len(args)==2 and args[0].isdigit():
                packages.assign(int(args[0]),args[1],name=='/package_assign')
            elif name=='/package_rollback' and len(args)==3:packages.rollback(*args)
            elif name=='/package_audience' and len(args)==4:packages.audience(*args)
            else:raise ValueError('package_release КОМПАНИЯ БИЗНЕС ВЕРСИЯ URL SHA256; package_assign ID БИЗНЕС; package_unassign ID БИЗНЕС; package_rollback КОМПАНИЯ БИЗНЕС ВЕРСИЯ')
            await update.effective_message.reply_text('Назначение бизнес-пакета сохранено. Другие компании не изменены. Автоответы и рассылка не запускались.')
        except ValueError as exc:await update.effective_message.reply_text(str(exc))
    for name in ('package_release','package_assign','package_unassign','package_rollback','package_audience'):
        application.add_handler(CommandHandler(name,command))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Build a data-only company dialogue package; no sender startup.')
    for name in ('company','business','version','profile','output'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    print(build_package(args.company,args.business,args.version,args.profile,args.output))
