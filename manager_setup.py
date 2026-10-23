"""Configure a separate pilot installation without requesting secrets in chat."""
import base64
from pathlib import Path
import uuid
from dotenv import dotenv_values, set_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey


def main():
    path = Path(__file__).resolve().parent / '.env'
    if not path.exists():
        raise RuntimeError('Сначала создайте .env по образцу .env.example с личными ключами менеджера.')
    current = dotenv_values(path)
    print('Только для отдельной тестовой копии. Передача данных владельцу центра: см. DATA_POLICY.md.')
    if input('Для настройки этой копии введите PILOT: ').strip() != 'PILOT':
        return 1
    public = input('Публичный ключ доступа центра: ').strip()
    encryption = input('Публичный ключ шифрования данных центра: ').strip()
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public, validate=True))
    X25519PublicKey.from_public_bytes(base64.b64decode(encryption, validate=True))
    values = dict(ACCESS_BOT_USERNAME='asdvk2026_bot', ACCESS_PUBLIC_KEY=public, SYNC_PUBLIC_KEY=encryption,
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
