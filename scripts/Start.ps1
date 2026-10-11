param([switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $workspace
$venvPython = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run scripts\Setup.ps1 first' }
$settings = Get-Content -LiteralPath 'config.local.json' -Raw | ConvertFrom-Json
if (($settings.port -isnot [int] -and $settings.port -isnot [long]) -or
    $settings.port -lt 1024 -or $settings.port -gt 65535) { throw 'Invalid local dashboard port.' }
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $settings.port -ErrorAction SilentlyContinue)
if ($listeners.Count) {
    throw 'The configured port is already in use. No browser was opened and no existing process was stopped. Close the existing application or choose another local port.'
}
if ($OpenBrowser) {
    Start-Process ('http://127.0.0.1:' + $settings.port)
}
& $venvPython -m gtbms.cli serve --config (Join-Path $workspace 'config.local.json')
exit $LASTEXITCODE
