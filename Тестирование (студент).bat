@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Ищем Python: сначала лаунчер py, потом python
where py >nul 2>nul
if not errorlevel 1 goto run_py
where python >nul 2>nul
if not errorlevel 1 goto run_python

echo.
echo Python не найден на этом компьютере.
echo Установите его с сайта https://python.org
echo и при установке отметьте галочку "Add Python to PATH".
echo.
pause
exit /b 1

:run_py
py cadet.py
goto done

:run_python
python cadet.py

:done
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Программа завершилась с ошибкой - её текст выше.
echo Сделайте снимок экрана и покажите его тому, кто настраивал программу.
echo.
pause
exit /b 1
