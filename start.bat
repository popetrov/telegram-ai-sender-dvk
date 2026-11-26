@echo off
setlocal
cd /d "%~dp0"
if exist ".dvk\layout.json" if exist "venv\Scripts\python.exe" goto UNIFIED
if exist "data\python-path.txt" (
    set /p DVK_PYTHON=<"data\python-path.txt"
    goto SAVED
)
py -3.11 --version >nul 2>&1
if errorlevel 1 goto FALLBACK
py -3.11 start.py %*
exit /b %ERRORLEVEL%
:FALLBACK
python --version >nul 2>&1
if errorlevel 1 goto MISSING
python start.py %*
exit /b %ERRORLEVEL%
:MISSING
echo Python 3.11 or newer is required. Install Python and try again.
exit /b 1
:SAVED
if not exist "%DVK_PYTHON%" goto FALLBACK
"%DVK_PYTHON%" start.py %*
exit /b %ERRORLEVEL%

:UNIFIED
"venv\Scripts\python.exe" start.py %*
exit /b %ERRORLEVEL%
