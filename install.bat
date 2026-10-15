@echo off
setlocal
cd /d "%~dp0"

echo TELEGRAM AI SENDER INSTALLER
echo.

if not exist "main.py" (
    echo ERROR: main.py not found.
    echo Extract the project ZIP archive first.
    pause
    exit /b 1
)

py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.11 is not installed.
    echo Install Python 3.11 from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment...

if not exist "venv\Scripts\python.exe" (
    py -3.11 -m venv venv
    if errorlevel 1 goto ERROR
)

echo [2/3] Installing dependencies...

"venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto ERROR

echo [3/3] Preparing configuration...

if not exist ".env" (
    if not exist ".env.example" (
        echo ERROR: .env.example not found.
        goto ERROR
    )
    copy ".env.example" ".env" >nul
    if errorlevel 1 goto ERROR
)

echo.
echo Installation completed.
echo Edit your .env file and enter your own keys.
echo Then run: venv\Scripts\python.exe main.py
echo.
pause
exit /b 0

:ERROR
echo.
echo ERROR: Installation failed.
echo Contact your manager and send a screenshot.
pause
exit /b 1