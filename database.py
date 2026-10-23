import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_manager_id() -> str:
    manager_id = getattr(config, "MANAGER_ID", "").strip()

    if not manager_id:
        raise ValueError(
            "Не задан MANAGER_ID в .env. "
            "Например: MANAGER_ID=manager_001"
        )

    return manager_id


@contextmanager
def get_connection():
    db_path = Path(config.DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")

    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }

    if column_name not in columns:
        connection.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {column_definition}"
        )


def _verify_manager_id(connection: sqlite3.Connection) -> str:
    """
    Проверяет соответствие MANAGER_ID в .env локальной базе.
    Не меняет существующий идентификатор менеджера.
    """

    manager_id = get_manager_id()

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS local_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    row = connection.execute(
        """
        SELECT value
        FROM local_settings
        WHERE key = 'manager_id'
        """
    ).fetchone()

    if row is None:
        connection.execute(
            """
            INSERT INTO local_settings (key, value, updated_at)
            VALUES ('manager_id', ?, ?)
            """,
            (manager_id, utc_now()),
        )
    elif row["value"] != manager_id:
        raise ValueError(
            "MANAGER_ID в .env не совпадает с ID локальной базы. "
            f"В базе: {row['value']}; в .env: {manager_id}. "
            "Операция отменена, чтобы не смешать данные менеджеров."
        )

    return manager_id


def init_database() -> None:
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inn TEXT NOT NULL,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL,
                telegram_id INTEGER,
                product TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_clients_inn
                ON clients(inn);

            CREATE INDEX IF NOT EXISTS idx_clients_username
                ON clients(username);

            CREATE INDEX IF NOT EXISTS idx_clients_telegram_id
                ON clients(telegram_id);

            CREATE TABLE IF NOT EXISTS research_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                product TEXT NOT NULL,
                prompt_file TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                model TEXT NOT NULL,
                raw_result TEXT NOT NULL,
                final_message TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id)
            );

            CREATE INDEX IF NOT EXISTS idx_research_runs_client_id
                ON research_runs(client_id);

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                research_run_id INTEGER,
                text TEXT NOT NULL,
                telegram_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'not_sent',
                send_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                FOREIGN KEY(client_id) REFERENCES clients(id),
                FOREIGN KEY(research_run_id)
                    REFERENCES research_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_client_id
                ON messages(client_id);

            CREATE TABLE IF NOT EXISTS imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                total_rows INTEGER NOT NULL,
                duplicate_rows INTEGER NOT NULL DEFAULT 0,
                new_rows INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prepared_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_id TEXT NOT NULL,
                inn TEXT NOT NULL,
                full_name TEXT NOT NULL,
                username TEXT NOT NULL,
                product TEXT NOT NULL,
                prompt_file TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                model TEXT NOT NULL,
                raw_result TEXT NOT NULL,
                final_message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'prepared',
                send_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_prepared_messages_lookup
                ON prepared_messages(
                    manager_id, inn, username, product, status
                );
            """
        )

        _ensure_column(connection, "messages", "send_error", "TEXT")
        _ensure_column(
            connection,
            "messages",
            "telegram_message_id",
            "INTEGER",
        )
        _ensure_column(connection, "clients", "manager_id", "TEXT")

        manager_id = _verify_manager_id(connection)

        connection.execute(
            """
            UPDATE clients
            SET manager_id = ?
            WHERE manager_id IS NULL OR manager_id = ''
            """,
            (manager_id,),
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_clients_manager_id
            ON clients(manager_id)
            """
        )
        from sync_data import initialize
        initialize(connection)


def normalize_username(username: str | None) -> str:
    if username is None:
        return ""

    return str(username).strip().lstrip("@").lower()


def normalize_inn(inn: str | None) -> str:
    if inn is None:
        return ""

    value = str(inn).strip()

    if value.endswith(".0"):
        value = value[:-2]

    return "".join(
        character for character in value
        if character.isdigit()
    )


def _has_explicit_peer_flood(
    connection: sqlite3.Connection,
    manager_id: str,
    inn: str,
    username: str,
    product: str,
) -> bool:
    """
    Проверяет наличие явного PeerFloodError для клиента и продукта.
    """

    normalized_inn = normalize_inn(inn)
    normalized_username = normalize_username(username)
    normalized_product = str(product or "").strip()

    if not normalized_inn and not normalized_username:
        return False

    conditions = []
    parameters = [manager_id, normalized_product]

    if normalized_inn:
        conditions.append("inn = ?")
        parameters.append(normalized_inn)

    if normalized_username:
        conditions.append("username = ?")
        parameters.append(normalized_username)

    row = connection.execute(
        f"""
        SELECT id
        FROM prepared_messages
        WHERE manager_id = ?
          AND product = ?
          AND status = 'uncertain'
          AND send_error LIKE 'PeerFloodError:%'
          AND ({' OR '.join(conditions)})
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        tuple(parameters),
    ).fetchone()

    return row is not None


def get_existing_client_keys() -> tuple[set[str], set[str]]:
    """
    Возвращает ключи клиентов, которых нельзя обрабатывать повторно.
    Клиенты с явным PeerFloodError исключаются из списка дублей.
    """

    existing_inns: set[str] = set()
    existing_usernames: set[str] = set()

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        rows = connection.execute(
            """
            SELECT id, inn, username, product, status
            FROM clients
            WHERE COALESCE(manager_id, '') IN ('', ?)
            """,
            (manager_id,),
        ).fetchall()

        for row in rows:
            inn = normalize_inn(row["inn"])
            username = normalize_username(row["username"])
            status = str(row["status"] or "").strip().lower()
            product = str(row["product"] or "").strip()

            if (
                status == "uncertain"
                and _has_explicit_peer_flood(
                    connection,
                    manager_id,
                    inn,
                    username,
                    product,
                )
            ):
                continue

            if inn:
                existing_inns.add(inn)

            if username:
                existing_usernames.add(username)

    return existing_inns, existing_usernames


def _find_existing_client(
    connection: sqlite3.Connection,
    inn: str,
    username: str,
) -> sqlite3.Row | None:
    """
    Ищет клиента по ИНН или username вместе с его статусом.
    """

    normalized_inn = normalize_inn(inn)
    normalized_username = normalize_username(username)

    if normalized_inn and normalized_username:
        return connection.execute(
            """
            SELECT id, status, product, inn, username
            FROM clients
            WHERE inn = ?
               OR lower(ltrim(username, '@')) = ?
            ORDER BY
                CASE WHEN status = 'sent' THEN 0 ELSE 1 END,
                id DESC
            LIMIT 1
            """,
            (normalized_inn, normalized_username),
        ).fetchone()

    if normalized_inn:
        return connection.execute(
            """
            SELECT id, status, product, inn, username
            FROM clients
            WHERE inn = ?
            ORDER BY
                CASE WHEN status = 'sent' THEN 0 ELSE 1 END,
                id DESC
            LIMIT 1
            """,
            (normalized_inn,),
        ).fetchone()

    if normalized_username:
        return connection.execute(
            """
            SELECT id, status, product, inn, username
            FROM clients
            WHERE lower(ltrim(username, '@')) = ?
            ORDER BY
                CASE WHEN status = 'sent' THEN 0 ELSE 1 END,
                id DESC
            LIMIT 1
            """,
            (normalized_username,),
        ).fetchone()

    return None


def create_client(
    inn: str,
    full_name: str,
    username: str,
    product: str,
    status: str = "new",
    telegram_id: int | None = None,
) -> int:
    now = utc_now()

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        existing = _find_existing_client(connection, inn, username)

        if existing is not None:
            raise ValueError(
                "Клиент уже есть в локальной базе. "
                "Повторная запись отменена."
            )

        cursor = connection.execute(
            """
            INSERT INTO clients (
                inn, full_name, username, telegram_id, product,
                status, created_at, updated_at, manager_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalize_inn(inn),
                full_name.strip(),
                normalize_username(username),
                telegram_id,
                product,
                status,
                now,
                now,
                manager_id,
            ),
        )

        return int(cursor.lastrowid)


def save_research(
    client_id: int,
    product: str,
    prompt_file: str,
    prompt_text: str,
    model: str,
    raw_result: str,
    final_message: str | None = None,
) -> int:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO research_runs (
                client_id, product, prompt_file, prompt_text,
                model, raw_result, final_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                product,
                prompt_file,
                prompt_text,
                model,
                raw_result,
                final_message,
                utc_now(),
            ),
        )

        return int(cursor.lastrowid)


def save_message(
    client_id: int,
    text: str,
    research_run_id: int | None = None,
    status: str = "not_sent",
) -> int:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO messages (
                client_id, research_run_id, text, status, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                client_id,
                research_run_id,
                text,
                status,
                utc_now(),
            ),
        )

        return int(cursor.lastrowid)


def save_processed_client(
    inn: str,
    full_name: str,
    username: str,
    product: str,
    prompt_file: str,
    prompt_text: str,
    model: str,
    raw_result: str,
    final_message: str,
    status: str = "sending",
) -> tuple[int, int, int]:
    """
    Сохраняет клиента, результат исследования и сообщение
    в одной транзакции до попытки отправки.
    """

    now = utc_now()

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        existing = _find_existing_client(connection, inn, username)

        if existing is not None:
            raise ValueError(
                "Клиент уже есть в локальной базе. "
                "Отправка отменена."
            )

        cursor = connection.execute(
            """
            INSERT INTO clients (
                inn, full_name, username, product, status,
                created_at, updated_at, manager_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalize_inn(inn),
                full_name.strip(),
                normalize_username(username),
                product,
                status,
                now,
                now,
                manager_id,
            ),
        )
        client_id = int(cursor.lastrowid)

        cursor = connection.execute(
            """
            INSERT INTO research_runs (
                client_id, product, prompt_file, prompt_text,
                model, raw_result, final_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                product,
                prompt_file,
                prompt_text,
                model,
                raw_result,
                final_message,
                now,
            ),
        )
        research_run_id = int(cursor.lastrowid)

        cursor = connection.execute(
            """
            INSERT INTO messages (
                client_id, research_run_id, text, status, created_at
            )
            VALUES (?, ?, ?, 'sending', ?)
            """,
            (client_id, research_run_id, final_message, now),
        )
        message_id = int(cursor.lastrowid)

        return client_id, research_run_id, message_id


def start_peerflood_retry(
    prepared_message_id: int,
    inn: str,
    full_name: str,
    username: str,
    product: str,
    prompt_file: str,
    prompt_text: str,
    model: str,
    raw_result: str,
    final_message: str,
) -> tuple[int, int, int]:
    """
    Атомарно подготавливает повторную отправку ранее отклонённого
    сообщения. Использует сохранённый текст, не создаёт клиента
    повторно и допускает только явный PeerFloodError.
    """

    now = utc_now()
    normalized_inn = normalize_inn(inn)
    normalized_username = normalize_username(username)
    normalized_product = str(product).strip()

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        prepared = connection.execute(
            """
            SELECT *
            FROM prepared_messages
            WHERE id = ?
              AND manager_id = ?
              AND status = 'uncertain'
              AND send_error LIKE 'PeerFloodError:%'
            """,
            (prepared_message_id, manager_id),
        ).fetchone()

        if prepared is None:
            raise ValueError(
                "Повтор запрещён: запись не является сообщением "
                "с подтверждённым PeerFloodError."
            )

        if (
            normalize_inn(prepared["inn"]) != normalized_inn
            or normalize_username(prepared["username"])
            != normalized_username
            or str(prepared["product"]).strip() != normalized_product
            or prepared["final_message"] != final_message
        ):
            raise ValueError(
                "Данные повторной отправки не совпадают "
                "с сохранённой записью. Отправка отменена."
            )

        existing = connection.execute(
            """
            SELECT id, status, product, inn, username
            FROM clients
            WHERE manager_id = ?
              AND (
                  inn = ?
                  OR lower(ltrim(username, '@')) = ?
              )
            ORDER BY
                CASE WHEN status = 'sent' THEN 0 ELSE 1 END,
                id DESC
            LIMIT 1
            """,
            (manager_id, normalized_inn, normalized_username),
        ).fetchone()

        if existing is None:
            raise ValueError(
                "Исходная запись клиента не найдена. "
                "Автоматическое создание нового клиента запрещено."
            )

        if (
            str(existing["status"]).lower() != "uncertain"
            or str(existing["product"]).strip() != normalized_product
            or not _has_explicit_peer_flood(
                connection,
                manager_id,
                normalized_inn,
                normalized_username,
                normalized_product,
            )
        ):
            raise ValueError(
                "Статус клиента или история ошибки не позволяют "
                "безопасно повторить отправку."
            )

        client_id = int(existing["id"])

        cursor = connection.execute(
            """
            INSERT INTO research_runs (
                client_id, product, prompt_file, prompt_text,
                model, raw_result, final_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_id,
                normalized_product,
                prompt_file,
                prompt_text,
                model,
                raw_result,
                final_message,
                now,
            ),
        )
        research_run_id = int(cursor.lastrowid)

        cursor = connection.execute(
            """
            INSERT INTO messages (
                client_id, research_run_id, text, status, created_at
            )
            VALUES (?, ?, ?, 'sending', ?)
            """,
            (client_id, research_run_id, final_message, now),
        )
        message_id = int(cursor.lastrowid)

        connection.execute(
            """
            UPDATE clients
            SET status = 'sending', updated_at = ?
            WHERE id = ? AND manager_id = ?
            """,
            (now, client_id, manager_id),
        )

        connection.execute(
            """
            UPDATE prepared_messages
            SET status = 'sending',
                send_error = NULL,
                updated_at = ?
            WHERE id = ? AND manager_id = ?
            """,
            (now, prepared_message_id, manager_id),
        )

        return client_id, research_run_id, message_id

def save_research_only_client(
    inn: str,
    full_name: str,
    username: str,
    product: str,
    prompt_file: str,
    prompt_text: str,
    model: str,
    raw_result: str,
) -> tuple[int, int]:
    """
    Сохраняет клиента и результат ИИ для ручной проверки.
    """

    now = utc_now()

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        existing = _find_existing_client(connection, inn, username)

        if existing is not None:
            raise ValueError(
                "Клиент уже есть в локальной базе. "
                "Результат повторно не сохранён."
            )

        cursor = connection.execute(
            """
            INSERT INTO clients (
                inn, full_name, username, product, status,
                created_at, updated_at, manager_id
            )
            VALUES (?, ?, ?, ?, 'manual_review', ?, ?, ?)
            """,
            (
                normalize_inn(inn),
                full_name.strip(),
                normalize_username(username),
                product,
                now,
                now,
                manager_id,
            ),
        )
        client_id = int(cursor.lastrowid)

        cursor = connection.execute(
            """
            INSERT INTO research_runs (
                client_id, product, prompt_file, prompt_text,
                model, raw_result, final_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                client_id,
                product,
                prompt_file,
                prompt_text,
                model,
                raw_result,
                now,
            ),
        )
        research_run_id = int(cursor.lastrowid)

        return client_id, research_run_id


def update_message_status(
    message_id: int,
    status: str,
    telegram_message_id: int | None = None,
    send_error: str | None = None,
) -> None:
    """
    Обновляет статус отправки.
    """

    sent_at = utc_now() if status == "sent" else None

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE messages
            SET status = ?,
                telegram_message_id = COALESCE(
                    ?, telegram_message_id
                ),
                send_error = ?,
                sent_at = COALESCE(?, sent_at)
            WHERE id = ?
            """,
            (
                status,
                telegram_message_id,
                send_error,
                sent_at,
                message_id,
            ),
        )


def update_client_status(
    client_id: int,
    status: str,
    telegram_id: int | None = None,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE clients
            SET status = ?,
                telegram_id = COALESCE(?, telegram_id),
                updated_at = ?
            WHERE id = ?
            """,
            (status, telegram_id, utc_now(), client_id),
        )


def update_research_final_message(
    research_run_id: int,
    final_message: str,
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE research_runs
            SET final_message = ?
            WHERE id = ?
            """,
            (final_message, research_run_id),
        )


def save_import(
    file_name: str,
    total_rows: int,
    duplicate_rows: int,
    new_rows: int,
) -> int:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO imports (
                file_name, total_rows, duplicate_rows, new_rows, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                file_name,
                total_rows,
                duplicate_rows,
                new_rows,
                utc_now(),
            ),
        )

        return int(cursor.lastrowid)


def save_prepared_message(
    inn: str,
    full_name: str,
    username: str,
    product: str,
    prompt_file: str,
    prompt_text: str,
    model: str,
    raw_result: str,
    final_message: str,
) -> int:
    """
    Сохраняет подготовленное сообщение независимо от факта отправки.
    Обновляет только существующую запись со статусом prepared.
    """

    now = utc_now()
    normalized_inn = normalize_inn(inn)
    normalized_username = normalize_username(username)
    normalized_product = str(product).strip()

    if not normalized_inn and not normalized_username:
        raise ValueError(
            "Нельзя сохранить подготовленное сообщение без ИНН "
            "и username."
        )

    if not final_message or not final_message.strip():
        raise ValueError("Нельзя сохранить пустое сообщение.")

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        existing = None

        if normalized_inn:
            existing = connection.execute(
                """
                SELECT id
                FROM prepared_messages
                WHERE manager_id = ?
                  AND inn = ?
                  AND product = ?
                  AND status = 'prepared'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (manager_id, normalized_inn, normalized_product),
            ).fetchone()

        if existing is None and normalized_username:
            existing = connection.execute(
                """
                SELECT id
                FROM prepared_messages
                WHERE manager_id = ?
                  AND username = ?
                  AND product = ?
                  AND status = 'prepared'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (manager_id, normalized_username, normalized_product),
            ).fetchone()

        if existing is not None:
            connection.execute(
                """
                UPDATE prepared_messages
                SET inn = ?,
                    full_name = ?,
                    username = ?,
                    prompt_file = ?,
                    prompt_text = ?,
                    model = ?,
                    raw_result = ?,
                    final_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized_inn,
                    full_name.strip(),
                    normalized_username,
                    prompt_file,
                    prompt_text,
                    model,
                    raw_result,
                    final_message,
                    now,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])

        cursor = connection.execute(
            """
            INSERT INTO prepared_messages (
                manager_id, inn, full_name, username, product,
                prompt_file, prompt_text, model, raw_result,
                final_message, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
            """,
            (
                manager_id,
                normalized_inn,
                full_name.strip(),
                normalized_username,
                normalized_product,
                prompt_file,
                prompt_text,
                model,
                raw_result,
                final_message,
                now,
                now,
            ),
        )

        return int(cursor.lastrowid)


def get_prepared_message(
    inn: str,
    username: str,
    product: str,
) -> dict | None:
    """
    Возвращает запись prepared или запись uncertain с явным
    PeerFloodError. Записи sent не возвращаются.
    """

    normalized_inn = normalize_inn(inn)
    normalized_username = normalize_username(username)
    normalized_product = str(product).strip()

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        status_condition = """
            (
                status = 'prepared'
                OR (
                    status = 'uncertain'
                    AND send_error LIKE 'PeerFloodError:%'
                )
            )
        """

        row = None

        if normalized_inn:
            row = connection.execute(
                f"""
                SELECT *
                FROM prepared_messages
                WHERE manager_id = ?
                  AND inn = ?
                  AND product = ?
                  AND {status_condition}
                ORDER BY
                    CASE WHEN status = 'prepared' THEN 0 ELSE 1 END,
                    updated_at DESC
                LIMIT 1
                """,
                (manager_id, normalized_inn, normalized_product),
            ).fetchone()

        if row is None and normalized_username:
            row = connection.execute(
                f"""
                SELECT *
                FROM prepared_messages
                WHERE manager_id = ?
                  AND username = ?
                  AND product = ?
                  AND {status_condition}
                ORDER BY
                    CASE WHEN status = 'prepared' THEN 0 ELSE 1 END,
                    updated_at DESC
                LIMIT 1
                """,
                (manager_id, normalized_username, normalized_product),
            ).fetchone()

        return dict(row) if row is not None else None


def update_prepared_message_status(
    prepared_message_id: int,
    status: str,
    send_error: str | None = None,
) -> None:
    """
    Меняет статус подготовленной записи.
    """

    allowed_statuses = {
        "prepared",
        "sending",
        "sent",
        "uncertain",
        "duplicate",
        "send_failed",
        "manual_review",
    }

    if status not in allowed_statuses:
        raise ValueError(
            f"Недопустимый статус подготовленного сообщения: {status}"
        )

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        cursor = connection.execute(
            """
            UPDATE prepared_messages
            SET status = ?,
                send_error = ?,
                updated_at = ?
            WHERE id = ?
              AND manager_id = ?
            """,
            (
                status,
                send_error,
                utc_now(),
                prepared_message_id,
                manager_id,
            ),
        )

        if cursor.rowcount == 0:
            raise ValueError(
                "Подготовленное сообщение не найдено "
                "для текущего менеджера."
            )


def get_all_prepared_messages() -> list[dict]:
    """
    Возвращает подготовленные сообщения текущего менеджера.
    """

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        rows = connection.execute(
            """
            SELECT *
            FROM prepared_messages
            WHERE manager_id = ?
            ORDER BY id
            """,
            (manager_id,),
        ).fetchall()

    return [dict(row) for row in rows]


def get_pending_prepared_messages() -> list[dict]:
    """
    Возвращает новые подготовленные сообщения и сообщения,
    отклонённые с явным PeerFloodError.

    Остальные uncertain, sent и sending не восстанавливаются.
    """

    with get_connection() as connection:
        manager_id = _verify_manager_id(connection)

        rows = connection.execute(
            """
            SELECT *
            FROM prepared_messages
            WHERE manager_id = ?
              AND (
                  status = 'prepared'
                  OR (
                      status = 'uncertain'
                      AND send_error LIKE 'PeerFloodError:%'
                  )
              )
            ORDER BY id
            """,
            (manager_id,),
        ).fetchall()

    return [dict(row) for row in rows]
