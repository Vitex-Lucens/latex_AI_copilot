@echo off
rem Double-click to install or update tex-annotator on Windows. To remove it: install.cmd -Uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
