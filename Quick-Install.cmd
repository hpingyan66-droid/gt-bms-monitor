@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Quick-Install.ps1" %*
set "result=%errorlevel%"
if not "%result%"=="0" echo Installation failed. Read the message above. No system driver changes were requested.
pause
exit /b %result%
