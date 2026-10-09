@echo off
cd /d "%~dp0"
if exist ".venv-win\Scripts\python.exe" goto ready
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1"
if errorlevel 1 goto failed
:ready
".venv-win\Scripts\python.exe" -m talk2g %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo talk2g could not start. See the message above.
pause
exit /b 1
