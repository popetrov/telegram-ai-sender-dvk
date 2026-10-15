import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    manager_id = config.MANAGER_ID

    if not manager_id:
        raise ValueError(
            "Не задан MANAGER_ID в файле .env. "
            "Например: MANAGER_ID=manager_001"
        )

    db_path = Path(config.DATABASE_PATH)

    if not db_path.exists():
        raise FileNotFoundError(
            f"Локальная база не найдена: {db_path}"
        )

    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row

    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")

        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS local_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            existing_manager = connection.execute(
                """
                SELECT value
                FROM local_settings
                WHERE key = 'manager_id'
                """
            ).fetchone()

            if existing_manager is None:
                connection.execute(
                    """
                    INSERT INTO local_settings (
                        key,
                        value,
                        updated_at
                    )
                    VALUES ('manager_id', ?, ?)
                    """,
                    (manager_id, utc_now()),
                )
                print(f"Назначен manager_id: {manager_id}")

            elif existing_manager["value"] != manager_id:
                raise ValueError(
                    "ID в .env не совпадает с ID в базе. "
                    f"В базе: {existing_manager['value']}; "
                    f"в .env: {manager_id}. "
                    "Автоматическая замена отменена."
                )
            else:
                print(f"Существующий manager_id подтверждён: {manager_id}")

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sync_id TEXT NOT NULL UNIQUE,
                    entity_type TEXT NOT NULL,
                    entity_local_id INTEGER NOT NULL,
                    operation TEXT NOT NULL DEFAULT 'upsert',
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    synced_at TEXT
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sync_queue_status
                ON sync_queue(status, id)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sync_queue_entity
                ON sync_queue(entity_type, entity_local_id)
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

            client_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(clients)"
                ).fetchall()
            }

            if "manager_id" not in client_columns:
                connection.execute(
                    """
                    ALTER TABLE clients
                    ADD COLUMN manager_id TEXT
                    """
                )

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

            connection.execute(
                """
                INSERT INTO sync_state (
                    key,
                    value,
                    updated_at
                )
                VALUES ('sync_queue_initialized', 'true', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (utc_now(),),
            )

        print()
        print("Миграция завершена.")
        print(f"База: {db_path}")
        print(f"Manager ID: {manager_id}")

        for table in (
            "clients",
            "research_runs",
            "messages",
            "imports",
            "sync_queue",
        ):
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            print(f"{table}: {count}")

        missing_manager = connection.execute(
            """
            SELECT COUNT(*)
            FROM clients
            WHERE manager_id IS NULL OR manager_id = ''
            """
        ).fetchone()[0]

        print(f"Клиенты без manager_id: {missing_manager}")

    finally:
        connection.close()


if __name__ == "__main__":
    main()