@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

echo ==========================================
echo     УСТАНОВКА TELEGRAM AI SENDER
echo ==========================================
echo.

if not exist "main.py" (
    echo ОШИБКА: Не найдена main.py.
    echo Распакуйте архив проекта перед запуском.
    pause
    exit /b 1
)

py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo ОШИБКА: Не найден Python 3.11.
    echo Установите Python 3.11 и повторите запуск.
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/4] Создание виртуального окружения...

if not exist "venv\Scripts\python.exe" (
    py -3.11 -m venv venv
    if errorlevel 1 goto ERROR
)

echo.
echo [2/4] Установка зависимостей...

"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto ERROR

echo.
echo [3/4] Подготовка конфигурации...

if not exist ".env" (
    if not exist ".env.example" (
        echo ОШИБКА: Не найден .env.example.
        goto ERROR
    )
    copy ".env.example" ".env" >nul
    if errorlevel 1 goto ERROR
)

echo.
echo [4/4] Установка завершена.
echo.
echo Следующий шаг:
echo 1. Откройте файл .env и внесите свои ключи.
echo 2. Сохраните файл.
echo 3. Запустите бота командой:
echo    venv\Scripts\python.exe main.py
echo.
echo Telegram-сессия будет использоваться локально.
echo Не передавайте файл .env и файлы *.session другим людям.
echo.
pause
exit /b 0

:ERROR
echo.
echo ОШИБКА: Установка не завершена.
echo Проверьте сообщение выше и повторите попытку.
pause
exit /b 1