[CmdletBinding()]
param([switch]$Remove)
$ErrorActionPreference='Stop'
$workspace=Split-Path $PSScriptRoot -Parent
$sha=[Security.Cryptography.SHA256]::Create()
try {$suffix=([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($workspace.ToLowerInvariant())))).Replace('-','').Substring(0,16)} finally {$sha.Dispose()}
$taskName='GTBMS-Network-'+$suffix
if ($Remove) {
    $task=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        $expected=Join-Path $PSScriptRoot 'Start-Network.ps1'
        if (@($task.Actions).Count -ne 1 -or $task.Actions[0].Arguments -notlike ('*"'+$expected+'"*')) {throw 'Task ownership mismatch. No change.'}
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Host 'Connection manager startup registration removed. Running services were not stopped.'
    exit 0
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {throw 'Task already exists. It has not been overwritten.'}
$shell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$startScript=Join-Path $PSScriptRoot 'Start-Network.ps1'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute $shell -Argument ('-NoProfile -WindowStyle Hidden -File "'+$startScript+'"') -WorkingDirectory $workspace
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Write-Host 'Connection manager starts in your user session after sign-in. No BMS task or network was changed.'
