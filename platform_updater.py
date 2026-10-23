"""Pilot update path: validate a separate candidate and preserve the original installation."""
import argparse
from pathlib import Path
import subprocess
import sys
from file_lock import FileLock
from manual_update import prepare


def stage(source, package, digest, destination):
    source = Path(source).resolve()
    # Shared with python main.py: copying an active installation is refused.
    with FileLock(source / 'data/sender.lock'):
        return prepare(source, package, digest, destination)


def check(candidate, python=sys.executable):
    candidate = Path(candidate).resolve()
    # Only the separate candidate is migrated. No Telegram/AI network calls.
    result = subprocess.run([str(python), 'main.py', '--self-check'], cwd=candidate, timeout=90)
    if result.returncode:
        raise RuntimeError('Проверка копии не прошла. Исходная установка сохранена.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    stage_parser = sub.add_parser('stage')
    for name in ('source', 'package', 'sha256', 'destination'):
        stage_parser.add_argument('--' + name, required=True)
    probe = sub.add_parser('check')
    probe.add_argument('--candidate', required=True)
    probe.add_argument('--python', default=sys.executable)
    args = parser.parse_args()
    if args.command == 'stage':
        report = stage(args.source, args.package, args.sha256, args.destination)
        print('Копия подготовлена. Конфликтов: ' + str(len(report['conflicts'])))
        print('Проверьте update_report.json, затем зависимости и запуск на тестовых данных.')
        return 2 if report['conflicts'] else 0
    check(args.candidate, args.python)
    print('Локальная проверка копии прошла; Telegram и отправка ещё требуют пилота.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
