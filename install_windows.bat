@echo off
setlocal
cd /d "%~dp0"

echo.
echo ======================================================
echo  Instradar Windows Installer
echo ======================================================
echo.

powershell.exe ^
  -NoLogo ^
  -NoProfile ^
  -ExecutionPolicy Bypass ^
  -File "%~dp0install_windows.ps1" %*

if errorlevel 1 (
    echo.
    echo ======================================================
    echo  INSTALLATION FAILED
    echo ======================================================
    echo.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo  INSTALLATION COMPLETED
echo ======================================================
echo.
pause
