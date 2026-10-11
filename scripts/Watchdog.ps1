param([int]$MaxRestarts = 0)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $workspace
$venvPython = Join-Path $workspace '.venv\Scripts\python.exe'
$configFile = Join-Path $workspace 'config.local.json'
$settings = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
$sha = [Security.Cryptography.SHA256]::Create()
$mutexSuffix = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($workspace)))).Replace('-','').Substring(0,16)
$sha.Dispose()
$guard = [Threading.Mutex]::new($false, ('Local\GTBMS-' + $mutexSuffix))
if (-not $guard.WaitOne(0)) { $guard.Dispose(); exit 0 }
$restarts = 0
$child = $null
try {
    New-Item -ItemType Directory -Path (Join-Path $workspace 'logs') -Force | Out-Null
    while ($true) {
        # Do not kill or take over a server started by another process.
        $portCheck = [Net.Sockets.TcpClient]::new()
        $occupied = $false
        try { $portCheck.Connect('127.0.0.1', $settings.port); $occupied = $true }
        catch { }
        finally { $portCheck.Dispose() }
        if ($occupied) { throw 'The configured port is occupied. Stop the existing server before starting Watchdog.' }
        $arguments = @('-m', 'gtbms.cli', 'serve', '--config', ('"' + $configFile + '"'))
        $child = Start-Process -FilePath $venvPython -ArgumentList $arguments -WorkingDirectory $workspace -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $workspace 'logs\console.out.log') -RedirectStandardError (Join-Path $workspace 'logs\console.err.log')
        Start-Sleep -Seconds 10
        $failures = 0
        while (-not $child.HasExited) {
            try {
                $health = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $settings.port + '/api/health') -TimeoutSec 5
                if (-not $health.ok) { throw 'worker unhealthy' }
                $failures = 0
            } catch { $failures++ }
            if ($failures -ge 3) { Stop-Process -Id $child.Id -Force; break }
            Start-Sleep -Seconds 20
            $child.Refresh()
        }
        $restarts++
        if ($MaxRestarts -gt 0 -and $restarts -ge $MaxRestarts) { break }
        Start-Sleep -Seconds ([Math]::Min(60, 5 * $restarts))
    }
} finally {
    if ($child -and -not $child.HasExited) { Stop-Process -Id $child.Id -Force }
    $guard.ReleaseMutex()
    $guard.Dispose()
}
