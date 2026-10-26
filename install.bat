@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "install_manager.py" (
  echo Распакуйте ZIP целиком, затем запустите install.bat из распакованной папки.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo Установка остановлена. Передайте руководителю снимок ошибки без токенов и ключей.
  pause
)
exit /b %RESULT%
