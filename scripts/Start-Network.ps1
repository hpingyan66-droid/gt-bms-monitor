[CmdletBinding()]
param([switch]$OpenUI)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
$python = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run scripts\Setup.ps1 first.' }
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8767 -ErrorAction SilentlyContinue)
if ($listeners.Count) {
    $health = $null
    try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8767/api/health' -TimeoutSec 3 } catch {}
    if ($health.application -ne 'gtbms-network' -or $health.ok -ne $true) { throw 'Port 8767 is occupied by another application. No process was stopped.' }
    if ($OpenUI) { Start-Process 'http://127.0.0.1:8767/' }
    Write-Host 'Connection manager is already running: http://127.0.0.1:8767/'
    exit 0
}
if ($OpenUI) {
    # The requested UI is visible; the manager itself is a hidden helper.
    $logDirectory=Join-Path $workspace 'logs'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $stamp=Get-Date -Format 'yyyyMMddTHHmmssfff'
    $child=Start-Process -FilePath $python -ArgumentList '-m','gtbms.network','serve' -WorkingDirectory $workspace -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory ('network-'+$stamp+'.out.log')) -RedirectStandardError (Join-Path $logDirectory ('network-'+$stamp+'.err.log'))
    $ready=$false
    for($attempt=0;$attempt -lt 30;$attempt++) {
        try { $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8767/api/health' -TimeoutSec 1; if($health.application -eq 'gtbms-network' -and $health.ok -eq $true) {$ready=$true;break} } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Connection manager did not become ready. Check logs/network-*.err.log.' }
    Start-Process 'http://127.0.0.1:8767/'
} else {
    Push-Location -LiteralPath $workspace
    try { & $python -m gtbms.network serve; exit $LASTEXITCODE }
    finally { Pop-Location }
}
