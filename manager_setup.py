"""Configure a separate pilot installation without requesting secrets in chat."""
import base64
from pathlib import Path
import uuid
import argparse
from dotenv import dotenv_values, set_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-bot', help='Move machine exchange to a dedicated service bot; keep keys and manager identity')
    args = parser.parse_args()
    path = Path(__file__).resolve().parent / '.env'
    if not path.exists():
        raise RuntimeError('Сначала создайте .env по образцу .env.example с личными ключами менеджера.')
    current = dotenv_values(path)
    if args.service_bot:
        import re
        from file_lock import FileLock
        name = args.service_bot.lstrip('@')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', name) or not name.lower().endswith('bot'):
            raise ValueError('Некорректное имя технического бота')
        if name.casefold() == 'asdvk2026_bot':
            raise ValueError('Для обмена нужен технический бот, а не административный.')
        with FileLock(path.parent / 'data/sender.lock'):
            backup = path.with_name('.env.before-center-' + uuid.uuid4().hex)
            with backup.open('xb') as stream:
                stream.write(path.read_bytes())
            set_key(str(path), 'ACCESS_BOT_USERNAME', name)
        print('Технический обмен перенесён в @' + name + '. Ключи и MANAGER_ID сохранены.')
        return 0
    print('Только для отдельной тестовой копии. Передача данных владельцу центра: см. DATA_POLICY.md.')
    if input('Для настройки этой копии введите PILOT: ').strip() != 'PILOT':
        return 1
    public = input('Публичный ключ доступа центра: ').strip()
    encryption = input('Публичный ключ шифрования данных центра: ').strip()
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True))
    X25519PublicKey.from_public_bytes(base64.b64decode(encryption, validate=True))
    service = input('Технический бот без @ [dvk_sync2026_bot]: ').strip().lstrip('@') or 'dvk_sync2026_bot'
    import re
    if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', service)
            or not service.lower().endswith('bot') or service.casefold() == 'asdvk2026_bot'):
        raise ValueError('Укажите отдельного технического бота')
    values = dict(ACCESS_BOT_USERNAME=service, ACCESS_PUBLIC_KEY=public, SYNC_PUBLIC_KEY=encryption,
                  MANAGER_ID=current.get('MANAGER_ID') or 'manager_' + uuid.uuid4().hex)
    backup = path.with_name('.env.before-center-' + uuid.uuid4().hex)
    with backup.open('xb') as stream:
        stream.write(path.read_bytes())
    for name, value in values.items():
        set_key(str(path), name, value)
    print('Настроено. Запустите python main.py. Владелец центра должен выполнить /approve ID КОМПАНИЯ.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print('Настройка не завершена: ' + str(exc))
        raise SystemExit(1)
