[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
$configPath = Join-Path $workspace 'mobile.local.json'
$venvPython = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) { throw '先に scripts\Start-Mobile.ps1 で mobile.local.json を作成してください。' }
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($null -eq $settings.port -or $settings.port -is [bool] -or [string]$settings.port -notmatch '^\d+$') { throw 'mobile.local.json のportが不正です。' }
$viewerPort = [int]$settings.port
if ($viewerPort -lt 1024 -or $viewerPort -gt 65535) { throw 'portの許容範囲は1024..65535です。' }
$sha = [Security.Cryptography.SHA256]::Create()
try { $workspaceHash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($workspace.ToLowerInvariant())))).Replace('-', '').Substring(0, 16) }
finally { $sha.Dispose() }
$ruleName = 'GTBMS-Mobile-' + $workspaceHash + '-' + $viewerPort
$ruleGroup = 'GT BMS Mobile Viewer'
$ruleDescription = 'GTBMS mobile viewer; workspace=' + $workspaceHash

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
try { $principal = [Security.Principal.WindowsPrincipal]::new($identity); $isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
finally { $identity.Dispose() }
if (-not $isAdministrator -and -not $WhatIfPreference) {
    throw 'この操作は管理者PowerShellから実行してください。自動昇格はしません。-WhatIf は事前確認用です。'
}

if ($Remove) {
    if ($WhatIfPreference) {
        $null = $PSCmdlet.ShouldProcess($ruleName, 'このワークスペース・ポート固有のスマホ閲覧ルールを削除')
        return
    }
    $existing = @(Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)
    if (-not $existing.Count) { Write-Host '該当ルールはありません。変更はありません。'; return }
    if ($existing.Count -ne 1 -or $existing[0].Name -ne $ruleName -or $existing[0].Group -ne $ruleGroup -or $existing[0].Description -ne $ruleDescription) {
        throw '同名ルールの所有情報が一致しません。削除していません。'
    }
    if ($PSCmdlet.ShouldProcess($ruleName, 'このワークスペース・ポート固有のスマホ閲覧ルールを削除')) {
        Remove-NetFirewallRule -Name $ruleName
        Write-Host 'スマホ閲覧用のルールを削除しました。通常のBMS監視は変更していません。'
    }
    return
}

$hostAddress = [string]$settings.host
$parsedAddress = $null
if (-not [Net.IPAddress]::TryParse($hostAddress, [ref]$parsedAddress) -or
    $parsedAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
    $parsedAddress.ToString() -ne $hostAddress) { throw 'hostは標準形式のIPv4アドレスで指定してください。' }
$octets = $parsedAddress.GetAddressBytes()
$rfc1918 = $octets[0] -eq 10 -or ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) -or ($octets[0] -eq 192 -and $octets[1] -eq 168)
if (-not $rfc1918) { throw 'プライベートIPv4アドレスのみ許可します。' }
if ($null -eq $settings.prefix_length -or $settings.prefix_length -is [bool] -or [string]$settings.prefix_length -notmatch '^\d+$') { throw 'prefix_lengthが不正です。' }
$prefixLength = [int]$settings.prefix_length
if ($prefixLength -lt 0 -or $prefixLength -gt 32) { throw 'prefix_lengthの許容範囲は0..32です。' }
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $hostAddress -and $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource })
if ($addresses.Count -ne 1 -or [int]$addresses[0].PrefixLength -ne $prefixLength) { throw '設定のIP／プレフィックスが現在の接続と一致しません。Start-Mobile.ps1で設定を確認してください。' }
$profiles = @(Get-NetConnectionProfile -InterfaceIndex $addresses[0].InterfaceIndex)
if (-not $profiles.Count -or @($profiles | Where-Object { $_.NetworkCategory -ne 'Private' }).Count) {
    throw '選択した接続はプライベートではありません。ネットワーク分類は変更していません。'
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { throw 'このワークスペースのPython環境がありません。' }

# Windows venv python.exe can be a redirector: Firewall needs the actual listener image.
# Only executable paths reported by this venv interpreter are eligible, never Any.
$pythonProbe = "import json,sys; print(json.dumps({'executable':sys.executable,'base_executable':sys._base_executable}))"
$pythonOutput = & $venvPython -c $pythonProbe
if ($LASTEXITCODE -ne 0) { throw 'Python実行ファイルの確認に失敗しました。ルールを変更していません。' }
try { $pythonPaths = ($pythonOutput -join "`n") | ConvertFrom-Json }
catch { throw 'Python実行ファイルの確認結果が不正です。ルールを変更していません。' }
$knownPythonPaths = @{}
foreach ($propertyName in @('executable', 'base_executable')) {
    $reportedPath = $pythonPaths.$propertyName
    if ($reportedPath -isnot [string] -or -not $reportedPath -or -not [IO.Path]::IsPathRooted($reportedPath) -or
        -not (Test-Path -LiteralPath $reportedPath -PathType Leaf)) {
        throw 'Pythonの報告した実行ファイルを確認できません。ルールを変更していません。'
    }
    $knownPythonPaths[$propertyName] = [IO.Path]::GetFullPath($reportedPath)
}
$viewerListeners = @(Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -eq $viewerPort })
if ($viewerListeners.Count -gt 1) { throw '閲覧ポートに複数のリスナーがあります。対象を特定できないため、ルールを変更していません。' }
if ($viewerListeners.Count -eq 1) {
    if ($viewerListeners[0].LocalAddress -ne $hostAddress) {
        throw '閲覧ポートの待ち受けIPが設定と一致しません。ルールを変更していません。'
    }
    try { $listenerImage = (Get-Process -Id $viewerListeners[0].OwningProcess -ErrorAction Stop).Path }
    catch { throw '閲覧サーバーの実行ファイルを確認できません。ルールを変更していません。' }
    if (-not $listenerImage -or -not (Test-Path -LiteralPath $listenerImage -PathType Leaf)) {
        throw '閲覧サーバーの実行ファイルが不明です。ルールを変更していません。'
    }
    $firewallProgram = [IO.Path]::GetFullPath($listenerImage)
    if ($firewallProgram -notin @($knownPythonPaths.executable, $knownPythonPaths.base_executable)) {
        throw '閲覧ポートを使用しているのは、この環境の既知Python以外のプロセスです。ルールを変更していません。'
    }
} else {
    # If the server is not running, use the verified real image behind the venv redirector.
    $firewallProgram = $knownPythonPaths.base_executable
}

# Build the exact IPv4 CIDR without endian-sensitive 32-bit shifts.
$networkOctets = for ($i = 0; $i -lt 4; $i++) {
    $bits = [Math]::Min(8, [Math]::Max(0, $prefixLength - 8 * $i))
    $mask = if ($bits -eq 0) { 0 } else { 256 - [int][Math]::Pow(2, 8 - $bits) }
    [int]$octets[$i] -band $mask
}
$remoteSubnet = ($networkOctets -join '.') + '/' + $prefixLength
# Even an incorrectly broad local subnet must not create an Internet-wide rule.
$minimumPrefix = if ($octets[0] -eq 10) { 8 } elseif ($octets[0] -eq 172) { 12 } else { 16 }
if ($prefixLength -lt $minimumPrefix) { throw '設定のサブネットがプライベートアドレス範囲を越えるため、ルールを作成できません。' }
$ruleArguments = @{
    Name = $ruleName
    DisplayName = ('GT BMS スマホ閲覧 TCP {0} [{1}]' -f $viewerPort, $workspaceHash)
    Group = $ruleGroup
    Description = $ruleDescription
    Direction = 'Inbound'
    Action = 'Allow'
    Enabled = 'True'
    Profile = 'Private'
    Protocol = 'TCP'
    LocalPort = $viewerPort
    LocalAddress = $hostAddress
    RemoteAddress = $remoteSubnet
    InterfaceAlias = [string]$addresses[0].InterfaceAlias
    Program = $firewallProgram
    EdgeTraversalPolicy = 'Block'
}
Write-Host ('受信 TCP {0}; ローカル {1}; 接続元 {2}; IF {3}; Private のみ' -f $viewerPort, $hostAddress, $remoteSubnet, $addresses[0].InterfaceAlias)
Write-Host ('対象プログラム: ' + $firewallProgram)
if ($WhatIfPreference) {
    $null = $PSCmdlet.ShouldProcess($ruleName, '上記条件のスマホ閲覧ルールを新規作成')
    return
}
$existing = @(Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)
if ($existing.Count) { throw '同名ルールが既にあります。上書きしません。変更が必要なら設定変更前に -Remove で削除してください。' }
if ($PSCmdlet.ShouldProcess($ruleName, '上記条件のスマホ閲覧ルールを新規作成')) {
    New-NetFirewallRule @ruleArguments | Out-Null
    Write-Host '閲覧用ルールを追加しました。解除は同じスクリプトに -Remove を付けて実行してください。'
}
