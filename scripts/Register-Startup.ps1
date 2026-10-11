param([switch]$Remove, [switch]$StartNow)
$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
$taskName = 'GT-BMS-Monitor'
if ($Remove) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host '自動起動を解除しました。'
    exit
}
if (-not (Test-Path -LiteralPath (Join-Path $workspace '.venv\Scripts\python.exe'))) { throw 'Run Setup.ps1 first' }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) { throw '同名タスクがあります。内容を確認し、必要なら -Remove で解除してください。既存タスクは上書きしません。' }
$shellPath = (Get-Process -Id $PID).Path
$watchdog = Join-Path $PSScriptRoot 'Watchdog.ps1'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $shellPath -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $watchdog + '"') -WorkingDirectory $workspace
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
if ($StartNow) { Start-ScheduledTask -TaskName $taskName }
Write-Host 'サインイン後にユーザーセッションで自動起動します。スリープ中は計測しません。'
