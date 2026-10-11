@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start.ps1" -OpenBrowser
set "result=%errorlevel%"
if not "%result%"=="0" (
  echo Application did not start. Run Quick-Install.cmd first and check the message above.
  pause
)
exit /b %result%
