[CmdletBinding()]
param(
    [string]$HostAddress = '',
    [ValidateRange(1024, 65535)][int]$Port = 8766,
    [switch]$OpenConnection,
    [switch]$RotateKey
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path $PSScriptRoot -Parent
$venvPython = Join-Path $workspace '.venv\Scripts\python.exe'
$mainConfigPath = Join-Path $workspace 'config.local.json'

function Test-PrivateIPv4([string]$Address) {
    $parsedAddress = $null
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsedAddress)) { return $false }
    if ($parsedAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { return $false }
    if ($parsedAddress.ToString() -ne $Address) { return $false }
    $octets = $parsedAddress.GetAddressBytes()
    return ($octets[0] -eq 10 -or
        ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) -or
        ($octets[0] -eq 192 -and $octets[1] -eq 168))
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { throw '先に scripts\Setup.ps1 で環境を構築してください。' }
if (-not (Test-Path -LiteralPath $mainConfigPath -PathType Leaf)) { throw 'config.local.json がありません。先に通常のBMS監視を起動してください。' }
$mainSettings = Get-Content -LiteralPath $mainConfigPath -Raw | ConvertFrom-Json
$mainPort = if ($null -ne $mainSettings.port) { [int]$mainSettings.port } else { 8765 }
if ($mainPort -lt 1024 -or $mainPort -gt 65535 -or $mainPort -eq $Port) { throw '通常監視とスマホ閲覧には別の有効なポートが必要です。' }

$addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource -and (Test-PrivateIPv4 $_.IPAddress)
})
if ($HostAddress) {
    if (-not (Test-PrivateIPv4 $HostAddress)) { throw '-HostAddress には、このPCのプライベートIPv4アドレスを指定してください。' }
    $candidates = @($addresses | Where-Object { $_.IPAddress -eq $HostAddress })
} else {
    $wifiInterfaces = @(Get-NetAdapter | Where-Object {
        $_.Status -eq 'Up' -and ($_.InterfaceType -eq 71 -or [int]$_.NdisPhysicalMedium -in @(1, 9))
    } | Select-Object -ExpandProperty InterfaceIndex)
    $defaultInterfaces = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' |
        Select-Object -ExpandProperty InterfaceIndex -Unique)
    $candidates = @($addresses | Where-Object {
        $_.InterfaceIndex -in $wifiInterfaces -and $_.InterfaceIndex -in $defaultInterfaces
    })
}
if ($candidates.Count -ne 1) {
    if ($addresses.Count) {
        $addresses | Select-Object IPAddress, PrefixLength, InterfaceAlias | Format-Table -AutoSize | Out-Host
    }
    throw '接続先を1つに特定できません。スマホと同じWi-Fi側のIPを -HostAddress に指定してください。'
}
$selectedAddress = $candidates[0]
$HostAddress = [string]$selectedAddress.IPAddress
$profiles = @(Get-NetConnectionProfile -InterfaceIndex $selectedAddress.InterfaceIndex)
if (-not $profiles.Count -or @($profiles | Where-Object { $_.NetworkCategory -ne 'Private' }).Count) {
    throw '選択した接続はWindowsのプライベートネットワークではありません。本人管理のWi-Fiであることと接続設定を確認してください。このスクリプトは分類を変更しません。'
}

# The viewer must attach to an existing healthy application; it never starts or stops BLE.
$request = [Net.HttpWebRequest]::Create(('http://127.0.0.1:{0}/api/health' -f $mainPort))
$request.Proxy = $null
$request.AllowAutoRedirect = $false
$request.Timeout = 3000
$request.ReadWriteTimeout = 3000
$healthResponse = $null
$healthReader = $null
try {
    $healthResponse = $request.GetResponse()
    $healthReader = [IO.StreamReader]::new($healthResponse.GetResponseStream())
    $health = $healthReader.ReadToEnd() | ConvertFrom-Json
    if ($health.ok -ne $true -or $null -eq $health.monitor_state) { throw 'Invalid health response' }
} catch {
    throw '通常監視の /api/health に接続できないか、監視プログラムに異常があります。先に通常監視の状態を確認してください。'
} finally {
    if ($healthReader) { $healthReader.Dispose() }
    if ($healthResponse) { $healthResponse.Dispose() }
}

$sha = [Security.Cryptography.SHA256]::Create()
try { $workspaceHash = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($workspace.ToLowerInvariant())))).Replace('-', '').Substring(0, 16) }
finally { $sha.Dispose() }
$guard = [Threading.Mutex]::new($false, ('Local\GTBMS-Mobile-' + $workspaceHash + '-' + $Port))
$ownsGuard = $false
try {
    try { $ownsGuard = $guard.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsGuard = $true }
    if (-not $ownsGuard) { throw 'このワークスペースのスマホ閲覧サーバーは既に起動しています。重複起動しません。' }
    # Do not terminate, replace, or take over an existing listener.
    $listeners = @(Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -eq $Port })
    if ($listeners.Count) { throw ('ポート {0} は使用中です。既存プロセスは変更していません。' -f $Port) }
    Push-Location -LiteralPath $workspace
    try {
        $initArguments = @('-m', 'gtbms.mobile', 'init', '--host', $HostAddress,
            '--prefix-length', [string]$selectedAddress.PrefixLength, '--main-port', [string]$mainPort, '--port', [string]$Port)
        if ($RotateKey) { $initArguments += '--rotate-key' }
        & $venvPython @initArguments
        if ($LASTEXITCODE -ne 0) { throw 'スマホ閲覧設定の作成に失敗しました。' }
        Write-Host ('閲覧先: http://{0}:{1}/ （認証情報は mobile-connect.local.html 内）' -f $HostAddress, $Port)
        Write-Host '必要に応じて別の管理者PowerShellで scripts\Mobile-Firewall.ps1 を実行してください。ここではファイアウォールを変更しません。'
        if ($OpenConnection) {
            # This is the explicitly requested visible connection page, not a background helper.
            Start-Process -FilePath (Join-Path $workspace 'mobile-connect.local.html')
        }
        & $venvPython -m gtbms.mobile serve
        if ($LASTEXITCODE -ne 0) { throw ('スマホ閲覧サーバーが終了コード {0} で終了しました。' -f $LASTEXITCODE) }
    } finally { Pop-Location }
} finally {
    if ($ownsGuard) { $guard.ReleaseMutex() }
    $guard.Dispose()
}
