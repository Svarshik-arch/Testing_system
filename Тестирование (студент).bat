@echo off
chcp 65001 >nul
cd /d "%~dp0"
py cadet.py 2>nul || python cadet.py
if errorlevel 1 (
  echo.
  echo Не удалось запустить. Проверьте, установлен ли Python (python.org).
  pause
)
