@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  python center_setup.py run
) else (
  python center_setup.py %*
)
set "CENTER_EXIT=%ERRORLEVEL%"
if not "%CENTER_EXIT%"=="0" pause
exit /b %CENTER_EXIT%
