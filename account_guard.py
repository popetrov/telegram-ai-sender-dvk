"""One active sender per Telegram account for this operating-system user."""
import atexit,os,sys
from pathlib import Path
from file_lock import FileLock

_held=None
_account=None


def storage_root():
    if os.name=='nt':return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'DVKSender/account-locks'
    if sys.platform=='darwin':return Path.home()/'Library/Application Support/DVKSender/account-locks'
    return Path(os.environ.get('XDG_DATA_HOME',str(Path.home()/'.local/share')))/'dvk-sender/account-locks'


def claim_account(account_id,base=None):
    global _held,_account
    if type(account_id) is not int or account_id<=0:raise ValueError('Invalid account identity')
    if _held is not None:
        if _account!=account_id:raise RuntimeError('Telegram-аккаунт изменился во время работы. Запуск остановлен.')
        return
    directory=Path(base) if base is not None else storage_root()
    path=directory/(str(account_id)+'.lock')
    if directory.is_symlink() or path.is_symlink():raise RuntimeError('Небезопасная папка блокировки аккаунта.')
    lock=FileLock(path)
    try:lock.__enter__()
    except RuntimeError:
        raise RuntimeError('Этот Telegram-аккаунт уже используется другой копией программы на этом компьютере. Сначала остановите её.') from None
    _held,_account=lock,account_id


def release_account():
    global _held,_account
    lock=_held;_held=None;_account=None
    if lock is not None:lock.__exit__()


atexit.register(release_account)
