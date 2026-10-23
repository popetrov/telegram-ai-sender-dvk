@echo off
setlocal
cd /d "%~dp0"
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
