"""Configure a separate pilot installation without requesting secrets in chat."""
import base64
from pathlib import Path
import uuid
import argparse
import json
import os
from dotenv import dotenv_values, set_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey


def validate_existing_number(current):
    """Preserve numbered legacy identities; never silently create another session."""
    import re
    manager=str(current.get('MANAGER_ID') or '').strip()
    numbered=re.fullmatch(r'(?:manager[_-]?)?([0-9]{1,12})',manager,re.I)
    if numbered:
        session=str(current.get('SESSION_NAME') or '').strip().removesuffix('.session')
        suffix=re.search(r'([0-9]+)$',session)
        if not suffix or suffix[1]!=numbered[1]:
            raise ValueError('Номер MANAGER_ID не совпадает с окончанием SESSION_NAME. Настройки не изменены; нужна проверка владельца.')


def configure_center(path, public):
    from file_lock import FileLock
    import re
    path = Path(path)
    if public.get('format') != 1:
        raise ValueError('Неподдерживаемая конфигурация центра')
    service = public.get('service_bot','')
    if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',service)
            or not service.lower().endswith('bot') or service.casefold()=='asdvk2026_bot'):
        raise ValueError('Нужен отдельный технический бот')
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public['access_public_key'],validate=True))
    X25519PublicKey.from_public_bytes(base64.b64decode(public['sync_public_key'],validate=True))
    with FileLock(path.parent/'data/sender.lock'):
        current = dotenv_values(path)
        if not current.get('MANAGER_ID','').strip():
            raise ValueError('В старой установке не задан MANAGER_ID; требуется локальная проверка')
        validate_existing_number(current)
        values = dict(ACCESS_BOT_USERNAME=service,ACCESS_PUBLIC_KEY=public['access_public_key'],
                      SYNC_PUBLIC_KEY=public['sync_public_key'])
        for name in ('ACCESS_PUBLIC_KEY','SYNC_PUBLIC_KEY'):
            if current.get(name) and current[name].strip()!=values[name]:
                raise ValueError('Установка уже доверяет другому ключу центра; автоматическая замена запрещена')
        backup = path.with_name('.env.before-center-'+uuid.uuid4().hex)
        original = path.read_bytes()
        with backup.open('xb') as stream:
            stream.write(original)
        if os.name!='nt':
            backup.chmod(0o600)
        try:
            for name,value in values.items():
                set_key(str(path),name,value)
        except BaseException:
            path.write_bytes(original)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-bot', help='Move machine exchange to a dedicated service bot; keep keys and manager identity')
    parser.add_argument('--center-config',type=Path,help='Apply owner-supplied public center configuration to this stopped installation')
    args = parser.parse_args()
    path = Path(__file__).resolve().parent / '.env'
    if not path.exists():
        raise RuntimeError('Сначала создайте .env по образцу .env.example с личными ключами менеджера.')
    current = dotenv_values(path)
    if args.center_config:
        if args.service_bot:
            raise ValueError('Выберите только один режим настройки')
        configure_center(path,json.loads(args.center_config.read_text('utf-8')))
        print('Публичная конфигурация центра установлена. MANAGER_ID, сессия и личные ключи сохранены.')
        return 0
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
