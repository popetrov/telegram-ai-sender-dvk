
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from datetime import datetime
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

PROJECT_DIR = Path(__file__).resolve().parent

REPOSITORY = "popetrov/telegram-ai-sender-dvk"
RELEASES_API_URL = (
    f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
)

VERSION_FILE = PROJECT_DIR / "version.txt"
MAIN_FILE = PROJECT_DIR / "main.py"

DATA_DIR = PROJECT_DIR / "data"
UPDATE_DIR = DATA_DIR / "updates"
BACKUP_DIR = DATA_DIR / "backups"

UPDATE_REQUEST = DATA_DIR / "update.request"
UPDATE_STATUS = DATA_DIR / "update.status"

CHECK_INTERVAL = 6 * 60 * 60
POLL_INTERVAL = 2
HTTP_TIMEOUT = 30

UPDATE_ALLOWLIST = {
    "ai.py",
    "arbitration_api.py",
    "bot.py",
    "database.py",
    "launcher.py",
    "main.py",
    "products.json",
    "prompts/bg.md",
    "prompts/credit.md",
    "prompts/pravo_ip.md",
    "prompts/pravo_ooo.md",
    "prompts/zakupki_konsalt.md",
    "updater.py",
    "version.txt",
}


# ============================================================
# LOGGING AND STATUS
# ============================================================

def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def write_status(message):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    temporary = UPDATE_STATUS.with_suffix(".tmp")
    temporary.write_text(message + "\n", encoding="utf-8")
    os.replace(temporary, UPDATE_STATUS)


def parse_version(value):
    value = str(value).strip()

    if value.startswith("v"):
        value = value[1:]

    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"Некорректный номер версии: {value!r}")

    return tuple(int(part) for part in value.split("."))


def get_current_version():
    if not VERSION_FILE.is_file():
        raise FileNotFoundError(
            f"Не найден файл версии: {VERSION_FILE}"
        )

    return VERSION_FILE.read_text(encoding="utf-8-sig").strip()


# ============================================================
# HTTP
# ============================================================

def download_bytes(url, accept=None):
    """Download HTTPS content, falling back to Windows curl without disabling TLS checks."""
    headers = {
        "User-Agent": "Telegram-AI-Sender-Updater",
    }

    if accept:
        headers["Accept"] = accept

    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:
            return response.read()
    except Exception as urllib_exc:
        # On Windows, curl.exe commonly uses the Windows certificate store
        # (Schannel), which may work when Python's OpenSSL CA configuration
        # is broken. TLS verification remains enabled; never use -k/--insecure.
        curl_exe = shutil.which("curl.exe") or shutil.which("curl")
        if not curl_exe:
            raise

        command = [
            curl_exe,
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "10",
            "--max-time",
            str(HTTP_TIMEOUT),
            "--user-agent",
            headers["User-Agent"],
        ]

        if accept:
            command.extend(["--header", f"Accept: {accept}"])

        command.append(url)
        log(
            "HTTPS-запрос через Python не прошёл "
            f"({type(urllib_exc).__name__}); пробуем curl.exe "
            "с обычной проверкой сертификата."
        )

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=HTTP_TIMEOUT + 5,
                check=False,
            )
        except Exception as curl_exc:
            raise RuntimeError(
                "HTTPS-запрос не удался через Python "
                f"({urllib_exc}) и curl.exe ({curl_exc})."
            ) from curl_exc

        if result.returncode != 0:
            details = result.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()
            raise RuntimeError(
                "HTTPS-запрос не удался через Python "
                f"({urllib_exc}) и curl.exe "
                f"({details or 'неизвестная ошибка'})."
            ) from urllib_exc

        return result.stdout


# ============================================================
# GITHUB RELEASE DISCOVERY
# ============================================================

def get_latest_release_from_atom():
    """Fallback that discovers the latest release without the GitHub API."""
    feed_url = f"https://github.com/{REPOSITORY}/releases.atom"
    try:
        raw = download_bytes(
            feed_url,
            accept="application/atom+xml, application/xml",
        )
        root = ET.fromstring(raw)
    except Exception as exc:
        log(f"Не удалось прочитать публичную ленту релизов GitHub: {exc}")
        return None

    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", namespace)
    if entry is None:
        log("В публичной ленте GitHub нет опубликованных релизов.")
        return None

    # В Atom id может быть не URL вида /tag/v2.0.17, а
    # идентификатором tag:github.com,.../v2.0.17. Сначала ищем
    # ссылку на сам релиз, затем используем последний компонент atom:id.
    tag = ""
    for link in entry.findall("atom:link", namespace):
        href = (link.get("href") or "").strip()
        rel = (link.get("rel") or "").strip().lower()
        if href and rel in {"alternate", ""}:
            match = re.search(r"/releases/tag/([^/?#]+)", href)
            if match:
                tag = match.group(1)
                break

    if not tag:
        id_element = entry.find("atom:id", namespace)
        release_id = (
            id_element.text.strip()
            if id_element is not None and id_element.text
            else ""
        )
        # Поддерживаем URL-формат и стандартный GitHub Atom ID,
        # который заканчивается номером версии.
        match = re.search(r"(?:/tag/|/)(v?\d+\.\d+\.\d+)$", release_id)
        if match:
            tag = match.group(1)

    if not tag:
        log("Не удалось определить версию релиза из ленты GitHub.")
        return None

    try:
        version = parse_version(tag)
    except ValueError:
        log("В ленте GitHub обнаружен некорректный номер версии.")
        return None

    package_name = f"telegram_ai_sender_{tag}.zip"
    base_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}"
    log("Используем публичную ленту релизов вместо GitHub API.")
    return {
        "tag": tag,
        "version": version,
        "package_url": f"{base_url}/{package_name}",
        "checksum_url": f"{base_url}/{package_name}.sha256",
    }


def get_latest_release():
    try:
        raw = download_bytes(
            RELEASES_API_URL,
            accept="application/vnd.github+json",
        )
        release = json.loads(raw.decode("utf-8"))

        if release.get("draft") or release.get("prerelease"):
            return None

        tag = release.get("tag_name", "").strip()
        version = parse_version(tag)
        assets = release.get("assets", [])
        package_name = f"telegram_ai_sender_{tag}.zip"
        checksum_name = package_name + ".sha256"

        packages = [
            item for item in assets
            if item.get("name") == package_name
        ]
        checksums = [
            item for item in assets
            if item.get("name") == checksum_name
        ]

        if len(packages) != 1 or len(checksums) != 1:
            log(
                "Релиз пропущен: необходимы ZIP-пакет "
                "и соответствующий файл SHA-256."
            )
            return None

        package_url = packages[0].get("browser_download_url")
        checksum_url = checksums[0].get("browser_download_url")
        if not package_url or not checksum_url:
            log("В релизе отсутствуют ссылки на файлы.")
            return None

        return {
            "tag": tag,
            "version": version,
            "package_url": package_url,
            "checksum_url": checksum_url,
        }

    except urllib.error.HTTPError as exc:
        log(f"GitHub API вернул HTTP {exc.code}; пробуем публичную ленту.")
        return get_latest_release_from_atom()
    except Exception as exc:
        log(f"Не удалось получить релиз через GitHub API: {exc}")
        return get_latest_release_from_atom()


# ============================================================
# DOWNLOAD AND VERIFY RELEASE
# ============================================================

def prepare_release(release):
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)

    tag = release["tag"]
    package_name = f"telegram_ai_sender_{tag}.zip"
    package_path = UPDATE_DIR / package_name

    log(f"Загружаем релиз {tag}.")

    package = download_bytes(release["package_url"])

    checksum_text = download_bytes(
        release["checksum_url"]
    ).decode("utf-8-sig").strip()

    match = re.fullmatch(
        r"([0-9a-fA-F]{64})(?:\s+\*?.*)?",
        checksum_text,
    )

    if not match:
        raise ValueError("Некорректный формат файла SHA-256.")

    expected_hash = match.group(1).lower()
    actual_hash = hashlib.sha256(package).hexdigest()

    if actual_hash != expected_hash:
        raise ValueError(
            "SHA-256 не совпадает. Обновление отменено."
        )

    with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
        bad_file = archive.testzip()

        if bad_file is not None:
            raise ValueError(f"Архив повреждён: {bad_file}")

    temporary = package_path.with_suffix(".tmp")

    try:
        temporary.write_bytes(package)
        os.replace(temporary, package_path)
    finally:
        if temporary.exists():
            temporary.unlink()

    log("SHA-256 и целостность архива проверены.")
    return package_path


# ============================================================
# DATABASE LOCATION AND BACKUP
# ============================================================

def get_database_path():
    import config

    database_path = Path(config.DATABASE_PATH)

    if not database_path.is_absolute():
        database_path = PROJECT_DIR / database_path

    return database_path.resolve()


def create_backup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir = BACKUP_DIR / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)

    existing_files = []

    for filename in sorted(UPDATE_ALLOWLIST):
        source = PROJECT_DIR / Path(filename)

        if source.is_file():
            destination = backup_dir / Path(filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            existing_files.append(filename)

    database_path = get_database_path()
    database_existed = database_path.is_file()

    manifest = {
        "created_at": datetime.now().isoformat(),
        "existing_files": sorted(existing_files),
        "database_path": str(database_path),
        "database_existed": database_existed,
    }

    if database_existed:
        backup_db = backup_dir / "local.sqlite"
        source_db = sqlite3.connect(str(database_path), timeout=30)
        target_db = sqlite3.connect(str(backup_db))

        try:
            source_db.backup(target_db)

            result = target_db.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if not result or result[0] != "ok":
                raise RuntimeError(
                    "Резервная копия SQLite не прошла проверку."
                )
        finally:
            target_db.close()
            source_db.close()

    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    log(f"Резервная копия создана: {backup_dir}")
    return backup_dir


# ============================================================
# STAGING AND VALIDATION
# ============================================================

def extract_update(package_path, staging_dir, expected_tag):
    package_path = Path(package_path).resolve()
    staging_dir = Path(staging_dir).resolve()

    if not package_path.is_file():
        raise FileNotFoundError(
            f"Не найден ZIP-пакет: {package_path}"
        )

    if staging_dir.exists():
        raise FileExistsError(
            f"Каталог распаковки уже существует: {staging_dir}"
        )

    staging_dir.mkdir(parents=True, exist_ok=False)

    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            selected = {}

            for info in archive.infolist():
                if info.is_dir():
                    continue

                normalized = info.filename.replace("\\", "/")
                parts = normalized.split("/")

                if (
                    normalized.startswith("/")
                    or re.match(r"^[A-Za-z]:", normalized)
                    or any(part in {"", ".", ".."} for part in parts)
                ):
                    raise ValueError(
                        f"Недопустимый путь в архиве: {info.filename}"
                    )

                filename = "/".join(parts)

                if filename not in UPDATE_ALLOWLIST:
                    raise ValueError(
                        f"В архиве присутствует запрещённый файл: "
                        f"{filename}"
                    )

                if filename in selected:
                    raise ValueError(
                        f"Дублирующийся файл в архиве: {filename}"
                    )

                selected[filename] = info

            missing = UPDATE_ALLOWLIST - set(selected)
            if missing:
                raise ValueError(
                    "В архиве отсутствуют обязательные файлы: "
                    + ", ".join(sorted(missing))
                )

            for filename, info in selected.items():
                destination = staging_dir / Path(filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(info))

        staged_version = (
            staging_dir / "version.txt"
        ).read_text(encoding="utf-8-sig").strip()

        if parse_version(staged_version) != parse_version(expected_tag):
            raise ValueError(
                "Версия внутри архива не совпадает с релизом."
            )

        # IMPORTANT:
        # Use absolute file paths. Relative paths combined with
        # cwd=staging_dir caused Python to search for files twice
        # under the staging directory.
        python_files = sorted(
            str(path.resolve())
            for path in staging_dir.glob("*.py")
        )

        expected_python_count = sum(
            1 for filename in UPDATE_ALLOWLIST
            if filename.endswith(".py")
        )
        if len(python_files) != expected_python_count:
            raise RuntimeError(
                "В каталоге подготовки не все Python-файлы релиза."
            )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "py_compile",
                *python_files,
            ],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(
                "Проверка синтаксиса не пройдена:\n" + details
            )

        log("Содержимое пакета и синтаксис проверены.")
        return staged_version

    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


# ============================================================
# SAFE FILE REPLACEMENT
# ============================================================

def replace_file(source, destination, suffix):
    source = Path(source)
    destination = Path(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + suffix)

    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def restore_database(backup_dir, manifest):
    database_path = Path(manifest["database_path"])

    if not database_path.is_absolute():
        database_path = PROJECT_DIR / database_path

    if manifest["database_existed"]:
        backup_db = backup_dir / "local.sqlite"

        if not backup_db.is_file():
            raise RuntimeError(
                "Не найдена резервная копия базы данных."
            )

        database_path.parent.mkdir(parents=True, exist_ok=True)

        temporary_db = database_path.with_suffix(
            database_path.suffix + ".rollback_tmp"
        )

        source_db = sqlite3.connect(str(backup_db))
        target_db = sqlite3.connect(str(temporary_db))

        try:
            source_db.backup(target_db)

            result = target_db.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            if not result or result[0] != "ok":
                raise RuntimeError(
                    "Восстановленная SQLite не прошла проверку."
                )
        finally:
            target_db.close()
            source_db.close()

        try:
            os.replace(temporary_db, database_path)
        finally:
            if temporary_db.exists():
                temporary_db.unlink()

    elif database_path.exists():
        database_path.unlink()


def restore_backup(backup_dir):
    backup_dir = Path(backup_dir)
    manifest_path = backup_dir / "manifest.json"

    if not manifest_path.is_file():
        raise RuntimeError(
            "Манифест резервной копии отсутствует. "
            "Автоматический откат остановлен."
        )

    manifest = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )

    existing_files = set(manifest.get("existing_files", []))

    if not existing_files.issubset(UPDATE_ALLOWLIST):
        raise RuntimeError(
            "Манифест содержит недопустимые имена файлов."
        )

    # Restore files first. If a required backup file is missing,
    # abort rather than deleting additional live files.
    for filename in existing_files:
        if not (backup_dir / filename).is_file():
            raise RuntimeError(
                f"Файл резервной копии отсутствует: {filename}"
            )

    # Remove files that did not exist before the update.
    for filename in UPDATE_ALLOWLIST - existing_files:
        destination = PROJECT_DIR / Path(filename)

        if destination.is_file():
            destination.unlink()

    for filename in sorted(existing_files):
        replace_file(
            backup_dir / Path(filename),
            PROJECT_DIR / Path(filename),
            ".rollback_tmp",
        )

    restore_database(backup_dir, manifest)
    log("Программные файлы и база данных восстановлены.")


# ============================================================
# INSTALLATION AND ROLLBACK
# ============================================================

def apply_update(package_path, expected_tag):
    staging_dir = UPDATE_DIR / f"staging_{expected_tag}"

    if staging_dir.exists():
        shutil.rmtree(staging_dir)

    backup_dir = None
    installation_started = False

    try:
        extract_update(
            package_path,
            staging_dir,
            expected_tag,
        )

        # No live file is replaced until the package has been
        # fully extracted, version-checked and syntax-checked.
        backup_dir = create_backup()
        installation_started = True

        for filename in sorted(UPDATE_ALLOWLIST):
            source = staging_dir / Path(filename)

            if source.is_file():
                replace_file(
                    source,
                    PROJECT_DIR / Path(filename),
                    ".update_tmp",
                )

        installed_version = get_current_version()

        if parse_version(installed_version) != parse_version(expected_tag):
            raise RuntimeError(
                "Проверка установленной версии не пройдена."
            )

        write_status(f"updated {installed_version}")
        log(f"Установлена версия {installed_version}.")
        return True

    except Exception as exc:
        log(f"Ошибка обновления: {exc}")

        if installation_started and backup_dir is not None:
            try:
                restore_backup(backup_dir)
                write_status(f"failed_rolled_back {expected_tag}")
                log("Откат завершён.")
            except Exception as rollback_exc:
                write_status(f"rollback_failed {expected_tag}")
                log(
                    "КРИТИЧЕСКАЯ ОШИБКА ОТКАТА: "
                    f"{rollback_exc}"
                )
        else:
            write_status(f"failed_before_install {expected_tag}")

        return False

    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


# ============================================================
# APPLICATION START AND STOP
# ============================================================

def start_application():
    if not MAIN_FILE.is_file():
        raise FileNotFoundError(f"Не найден файл: {MAIN_FILE}")

    log("Запускаем Telegram AI Sender.")

    return subprocess.Popen(
        [sys.executable, str(MAIN_FILE)],
        cwd=str(PROJECT_DIR),
    )


def request_update(release):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    temporary = UPDATE_REQUEST.with_suffix(".tmp")

    temporary.write_text(
        release["tag"] + "\n",
        encoding="utf-8",
    )

    os.replace(temporary, UPDATE_REQUEST)
    write_status(f"requested {release['tag']}")


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    if UPDATE_REQUEST.exists():
        log(
            "Обнаружен незавершённый запрос обновления. "
            "Автоматический запуск запрещён."
        )
        log(
            "Проверьте data/update.status и состояние очереди "
            "перед ручным устранением причины."
        )
        return 1

    try:
        current_version = get_current_version()
        current_version_tuple = parse_version(current_version)
    except Exception as exc:
        log(f"Ошибка чтения версии: {exc}")
        return 1

    log("=== TELEGRAM AI SENDER LAUNCHER ===")
    log(f"Текущая версия: {current_version}")

    # Apply a published update before starting the application. This is
    # important when the installed application cannot start until a fix is
    # installed (for example, first-run authorization support).
    try:
        release = get_latest_release()
        if (
            release is not None
            and release["version"] > current_version_tuple
        ):
            package_path = prepare_release(release)
            if not apply_update(package_path, release["tag"]):
                log("Предварительное обновление не удалось. Запуск отменён.")
                return 1

            current_version = get_current_version()
            current_version_tuple = parse_version(current_version)
            log("Предварительное обновление завершено.")

    except Exception as exc:
        log(f"Не удалось проверить обновления перед запуском: {exc}")
        # Do not block normal startup just because the network is unavailable.

    next_check = time.time() + CHECK_INTERVAL

    while True:
        try:
            process = start_application()
        except Exception as exc:
            log(f"Не удалось запустить приложение: {exc}")
            return 1

        pending_release = None
        pending_package = None

        while process.poll() is None:
            now = time.time()

            if now >= next_check and pending_release is None:
                next_check = now + CHECK_INTERVAL
                release = get_latest_release()

                if (
                    release is not None
                    and release["version"] > current_version_tuple
                ):
                    try:
                        package_path = prepare_release(release)

                        pending_release = release
                        pending_package = package_path

                        request_update(release)

                        log(
                            "Запрос безопасной остановки отправлен. "
                            "Ожидаем завершения текущего процесса."
                        )
                    except Exception as exc:
                        pending_release = None
                        pending_package = None

                        log(f"Подготовка обновления не удалась: {exc}")
                        write_status(f"prepare_failed {exc}")

            time.sleep(POLL_INTERVAL)

        exit_code = process.returncode
        log(f"Процесс завершился с кодом {exit_code}.")

        if pending_release is not None:
            if exit_code != 0:
                log(
                    "Приложение завершилось с ошибкой. "
                    "Обновление отменено."
                )
                write_status(
                    f"application_exit_error {pending_release['tag']}"
                )
                return exit_code or 1

            success = apply_update(
                pending_package,
                pending_release["tag"],
            )

            status = ""
            if UPDATE_STATUS.is_file():
                status = UPDATE_STATUS.read_text(
                    encoding="utf-8"
                ).strip()

            if success:
                UPDATE_REQUEST.unlink(missing_ok=True)

                current_version = get_current_version()
                current_version_tuple = parse_version(current_version)

                log("Обновление завершено. Перезапускаем приложение.")
                next_check = time.time() + CHECK_INTERVAL
                continue

            if status.startswith("failed_rolled_back"):
                UPDATE_REQUEST.unlink(missing_ok=True)
                log(
                    "Обновление отменено, прежняя версия восстановлена. "
                    "Перезапускаем приложение."
                )
                continue

            log(
                "Обновление не применено либо откат не подтверждён. "
                "Автоматический запуск остановлен для безопасности."
            )
            return 1

        if exit_code != 0:
            log(
                "Приложение завершилось с ошибкой. "
                "Автоматический перезапуск отменён."
            )
            return exit_code

        log("Приложение завершилось штатно.")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Лаунчер остановлен пользователем.")
        raise SystemExit(130)
