[CmdletBinding()]
param(
    [switch]$Json
)

# Read-only network inventory. Never inspect saved Wi-Fi profiles, SSIDs, MAC
# addresses, application settings, passwords, or viewer keys.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

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

function Test-WiFiDirectDescription([string]$Description) {
    return $Description -match '(?i)Wi[\s-]?Fi\s+Direct'
}

$inspectionErrors = @()
$addresses = @()
$adapters = @()
$profiles = @()

try {
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Select-Object IPAddress, InterfaceAlias, InterfaceIndex, PrefixLength, AddressState, SkipAsSource |
        Where-Object { Test-PrivateIPv4 ([string]$_.IPAddress) })
} catch {
    # Raw exception messages can contain machine/network details. Use fixed codes.
    $inspectionErrors += 'ipv4_inventory_unavailable'
}
try {
    $adapters = @(Get-NetAdapter -IncludeHidden -ErrorAction Stop |
        Select-Object InterfaceIndex, Name, InterfaceDescription, Status)
} catch {
    $inspectionErrors += 'adapter_inventory_unavailable'
}
try {
    # Do not select Name: a connection profile name can reveal the Wi-Fi SSID.
    $profiles = @(Get-NetConnectionProfile -ErrorAction Stop |
        Select-Object InterfaceIndex, NetworkCategory)
} catch {
    $inspectionErrors += 'network_category_inventory_unavailable'
}

$inspectionComplete = $inspectionErrors.Count -eq 0
$candidates = @(
    foreach ($address in ($addresses | Sort-Object InterfaceIndex, IPAddress)) {
        $matchingAdapters = @($adapters | Where-Object { $_.InterfaceIndex -eq $address.InterfaceIndex })
        $adapter = if ($matchingAdapters.Count -eq 1) { $matchingAdapters[0] } else { $null }
        $description = if ($null -ne $adapter) { [string]$adapter.InterfaceDescription } else { $null }
        $adapterStatus = if ($null -ne $adapter) { [string]$adapter.Status } else { 'Unknown' }
        $isUp = $adapterStatus -eq 'Up'
        $categories = @($profiles | Where-Object { $_.InterfaceIndex -eq $address.InterfaceIndex } |
            ForEach-Object { [string]$_.NetworkCategory } | Sort-Object -Unique)
        $category = if ($categories.Count -eq 1) { $categories[0] } elseif ($categories.Count -gt 1) { 'Mixed' } else { 'Unknown' }
        $isDirect = Test-WiFiDirectDescription $description
        $reasons = @()
        if (-not $inspectionComplete) { $reasons += 'inspection_incomplete' }
        if ($matchingAdapters.Count -ne 1) { $reasons += 'adapter_not_uniquely_identified' }
        if (-not $isUp) { $reasons += 'adapter_not_up' }
        if ([string]$address.AddressState -ne 'Preferred') { $reasons += 'address_not_preferred' }
        if ($address.SkipAsSource) { $reasons += 'address_skip_as_source' }
        if ($category -ne 'Private') { $reasons += 'private_profile_not_confirmed' }
        if (@($addresses | Where-Object { $_.IPAddress -eq $address.IPAddress }).Count -ne 1) {
            $reasons += 'address_assigned_to_multiple_interfaces'
        }
        $eligible = $reasons.Count -eq 0
        $startCommand = if ($eligible) {
            '.\scripts\Start-Mobile.ps1 -HostAddress {0}' -f $address.IPAddress
        } else { $null }
        [pscustomobject][ordered]@{
            ip_address = [string]$address.IPAddress
            prefix_length = [int]$address.PrefixLength
            interface_alias = [string]$address.InterfaceAlias
            interface_index = [int]$address.InterfaceIndex
            network_category = $category
            adapter_description = $description
            adapter_status = $adapterStatus
            adapter_up = $isUp
            address_state = [string]$address.AddressState
            skip_as_source = [bool]$address.SkipAsSource
            candidate_kind = $(if ($isDirect) { 'wifi_direct_virtual_adapter' } else { 'other_private_ipv4' })
            hotspot_state = 'not_verified'
            eligible_for_start = $eligible
            blocked_reasons = @($reasons)
            start_command = $startCommand
        }
    }
)

# List Wi-Fi Direct adapters even when they have no private IPv4. Presence or
# Status=Up does not establish that the Windows mobile hotspot is enabled.
$directAdapters = @(
    foreach ($adapter in ($adapters | Sort-Object InterfaceIndex)) {
        if (Test-WiFiDirectDescription ([string]$adapter.InterfaceDescription)) {
            [pscustomobject][ordered]@{
                interface_alias = [string]$adapter.Name
                interface_index = [int]$adapter.InterfaceIndex
                adapter_description = [string]$adapter.InterfaceDescription
                adapter_status = [string]$adapter.Status
                adapter_up = [string]$adapter.Status -eq 'Up'
                has_private_ipv4 = @($addresses | Where-Object { $_.InterfaceIndex -eq $adapter.InterfaceIndex }).Count -gt 0
                hotspot_state = 'not_verified'
            }
        }
    }
)

$report = [pscustomobject][ordered]@{
    schema = 1
    inspected_at_utc = [DateTime]::UtcNow.ToString('o')
    read_only = $true
    discovery_complete = $inspectionComplete
    hotspot_state = 'not_verified'
    smartphone_reachability = 'not_tested'
    errors = @($inspectionErrors)
    candidates = @($candidates)
    wifi_direct_adapters = @($directAdapters)
}

if ($Json) {
    $report | ConvertTo-Json -Depth 6
} else {
    Write-Output 'スマホ接続用ネットワーク確認（読取専用）'
    Write-Output 'ホットスポットのON/OFF、スマホからの到達性、ファイアウォール許可は未確認です。'
    Write-Output 'ネットワーク設定・サービス・ファイアウォール・ドライバーは変更しません。'
    if (-not $inspectionComplete) {
        Write-Output ('一部の情報を取得できません。接続候補の利用判定は保留します: ' + ($inspectionErrors -join ', '))
    }
    if (-not $candidates.Count) { Write-Output '現在、RFC1918のプライベートIPv4候補はありません。' }
    foreach ($candidate in $candidates) {
        $kind = if ($candidate.candidate_kind -eq 'wifi_direct_virtual_adapter') {
            'Wi-Fi Direct仮想アダプター（ホットスポット状態は未確認）'
        } else { 'プライベートIPv4（スマホと接続する側か確認が必要）' }
        Write-Output ''
        Write-Output ('{0}/{1}  {2}  ifIndex={3}' -f $candidate.ip_address, $candidate.prefix_length,
            $candidate.interface_alias, $candidate.interface_index)
        Write-Output ('  種類: {0}' -f $kind)
        Write-Output ('  状態: {0} / NetworkCategory: {1} / AddressState: {2}' -f
            $candidate.adapter_status, $candidate.network_category, $candidate.address_state)
        Write-Output ('  アダプター: {0}' -f $candidate.adapter_description)
        if ($candidate.eligible_for_start) {
            Write-Output ('  起動候補（実行しません）: {0}' -f $candidate.start_command)
            Write-Output '  既存の閲覧サーバーが動作中の場合は重複起動せず、接続先変更の手順を確認してください。'
        } else {
            Write-Output ('  起動候補にできません: {0}' -f ($candidate.blocked_reasons -join ', '))
            if ($candidate.network_category -ne 'Private') {
                Write-Output '  Privateを確認できない接続はStart-Mobileでブロックされます。本人管理の接続か確認してください。分類は自動変更しません。'
            }
        }
    }
    if ($directAdapters.Count) {
        Write-Output ''
        Write-Output '検出したWi-Fi Direct仮想アダプター（存在だけではホットスポットONと判定できません）:'
        foreach ($adapter in $directAdapters) {
            Write-Output ('  ifIndex={0} {1}: {2}, private IPv4={3}' -f $adapter.interface_index,
                $adapter.interface_alias, $adapter.adapter_status, $adapter.has_private_ipv4)
        }
    }
    Write-Output ''
    Write-Output 'JSON出力: .\scripts\Inspect-MobileNetwork.ps1 -Json'
    Write-Output '表示するIPは実際の割り当てから取得します。特定のホットスポットIPには固定しません。'
}
