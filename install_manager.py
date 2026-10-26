"""First installation only; preserve existing configurations and never send clients."""
from pathlib import Path
import os,subprocess,sys
from start import prepare_environment

ROOT=Path(__file__).resolve().parent

def main():
    os.chdir(ROOT)
    if (ROOT/'data/active-installation.json').exists():
        print('Эта папка уже обновлена. Используйте обычный файл запуска.');return 1
    from file_lock import FileLock
    with FileLock(ROOT/'data/install.lock'):
        python=prepare_environment(ROOT)
    return subprocess.run([str(python),str(ROOT/'setup_wizard.py'),'--launch'],cwd=ROOT).returncode

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print('Установка остановлена: '+type(exc).__name__+'. Сохранённые настройки не удалялись.',flush=True)
        raise SystemExit(1)
