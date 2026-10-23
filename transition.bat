@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
python transition_setup.py %*
set "TRANSITION_EXIT=%ERRORLEVEL%"
if not "%TRANSITION_EXIT%"=="0" pause
exit /b %TRANSITION_EXIT%
