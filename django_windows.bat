@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found.
    echo Run install_windows.bat first.
    pause
    exit /b 1
)

if not exist "manage.py" (
    echo manage.py was not found.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" manage.py %*
