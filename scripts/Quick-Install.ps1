[CmdletBinding()]
param([string]$PythonExe = '', [switch]$WithTests, [switch]$SkipInstall, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$setup = Join-Path $PSScriptRoot 'Setup.ps1'
# No elevation, Python download, OS settings, firewall or automatic task changes.
& $setup -PythonExe $PythonExe -WithTests:$WithTests -SkipInstall:$SkipInstall -CheckOnly:$CheckOnly
if (-not $CheckOnly) { Write-Host 'Installation finished. Double-click Start.cmd to open the dashboard.' }
