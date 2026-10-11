[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F-]{36}$')][string]$RequestId,
    [switch]$Elevated,
    [switch]$CheckOnly
)

# One scoped, owned rule. No network-category changes and no blanket port rules.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$workspace = Split-Path $PSScriptRoot -Parent
$requestDirectory = Join-Path $workspace 'logs\network-requests'
$requestPath = Join-Path $requestDirectory ($RequestId + '.json')
$resultPath = Join-Path $requestDirectory ($RequestId + '.result.json')

function Finish([bool]$Ok, [string]$Code) {
    $result = @{ok=$Ok;code=$Code} | ConvertTo-Json -Compress
    if ($Elevated) { [IO.File]::WriteAllText($resultPath, $result, [Text.UTF8Encoding]::new($false)) }
    else { [Console]::WriteLine($result) }
}
function Assert-FreshRequest {
    if (-not (Test-Path -LiteralPath $requestPath -PathType Leaf)) { throw 'request_expired' }
    $script:request = Get-Content -LiteralPath $requestPath -Raw | ConvertFrom-Json
    $expiration = [DateTimeOffset]::Parse([string]$request.expires_at)
    if ($expiration -le [DateTimeOffset]::UtcNow -or $expiration -gt [DateTimeOffset]::UtcNow.AddMinutes(2)) { throw 'request_expired' }
}
function Snapshot-Rule($Rule) {
    $address = $Rule | Get-NetFirewallAddressFilter
    $port = $Rule | Get-NetFirewallPortFilter
    $application = $Rule | Get-NetFirewallApplicationFilter
    $interface = $Rule | Get-NetFirewallInterfaceFilter
    return @{
        Name=[string]$Rule.Name;Profile=[string]$Rule.Profile;Enabled=[string]$Rule.Enabled
        Direction=[string]$Rule.Direction;Action=[string]$Rule.Action;EdgeTraversalPolicy=[string]$Rule.EdgeTraversalPolicy
        LocalAddress=@($address.LocalAddress);RemoteAddress=@($address.RemoteAddress)
        Protocol=[string]$port.Protocol;LocalPort=@($port.LocalPort);RemotePort=@($port.RemotePort)
        Program=[string]$application.Program;InterfaceAlias=@($interface.InterfaceAlias)
    }
}
function Normalize-Address([string]$Value) {
    $parts = $Value.Split('/')
    $address = $null
    if ($parts.Count -gt 2 -or -not [Net.IPAddress]::TryParse($parts[0], [ref]$address) -or
        $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
        $address.ToString() -ne $parts[0]) {
        # Named address sets / IPv6 are compared literally, never widened.
        return $Value.ToLowerInvariant()
    }
    $prefix = 32
    if ($parts.Count -eq 2) {
        if ($parts[1] -match '^\d{1,2}$') {
            $prefix = [int]$parts[1]
            if ($prefix -gt 32) { throw 'invalid_firewall_address_filter' }
        } else {
            $maskAddress = $null
            if (-not [Net.IPAddress]::TryParse($parts[1], [ref]$maskAddress) -or
                $maskAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
                $maskAddress.ToString() -ne $parts[1]) { throw 'invalid_firewall_address_filter' }
            $prefix = 0
            $sawZero = $false
            foreach ($octet in $maskAddress.GetAddressBytes()) {
                for ($bit = 7; $bit -ge 0; $bit--) {
                    if (($octet -band (1 -shl $bit)) -ne 0) {
                        if ($sawZero) { throw 'invalid_firewall_address_filter' }
                        $prefix++
                    } else { $sawZero = $true }
                }
            }
        }
    }
    $octets = $address.GetAddressBytes()
    $network = for ($i = 0; $i -lt 4; $i++) {
        $bits = [Math]::Min(8, [Math]::Max(0, $prefix - 8 * $i))
        $mask = if ($bits -eq 0) { 0 } else { 256 - [int][Math]::Pow(2, 8 - $bits) }
        [int]$octets[$i] -band $mask
    }
    return ($network -join '.') + '/' + $prefix
}
function Same-Value($Actual, $Wanted, [string]$Key = '') {
    $left = @($Actual)
    $right = @($Wanted)
    if ($Key -in @('LocalAddress', 'RemoteAddress')) {
        $left = @($left | ForEach-Object { Normalize-Address ([string]$_) } | Sort-Object -Unique)
        $right = @($right | ForEach-Object { Normalize-Address ([string]$_) } | Sort-Object -Unique)
    } elseif ($Key -eq 'Protocol') {
        $left = @($left | ForEach-Object { if ([string]$_ -eq '6') { 'TCP' } else { [string]$_ } })
        $right = @($right | ForEach-Object { if ([string]$_ -eq '6') { 'TCP' } else { [string]$_ } })
    }
    # Compare each array element; comma-containing aliases cannot collide.
    if ($left.Count -ne $right.Count) { return $false }
    for ($i = 0; $i -lt $left.Count; $i++) {
        if (-not [string]::Equals([string]$left[$i], [string]$right[$i], [StringComparison]::OrdinalIgnoreCase)) { return $false }
    }
    return $true
}
function Same-Snapshot($Actual, $Wanted) {
    foreach ($key in $Wanted.Keys) {
        if (-not (Same-Value $Actual[$key] $Wanted[$key] $key)) { return $false }
    }
    return $true
}
function Get-OwnedRule([string]$Name, [string]$Group, [string]$Description) {
    $rules = @(Get-NetFirewallRule -Name $Name -ErrorAction SilentlyContinue)
    if (-not $rules.Count) { return $null }
    if ($rules.Count -ne 1 -or $rules[0].Name -ne $Name -or
        $rules[0].Group -ne $Group -or $rules[0].Description -ne $Description) { throw 'firewall_ownership_mismatch' }
    return $rules[0]
}
function Restore-OwnedRule($Before, [string]$Name, [string]$Group, [string]$Description) {
    # Run even when New/Set throws before returning: a provider can partially
    # apply a change. Never edit/remove a rule without checking its ownership.
    $currentRule = Get-OwnedRule $Name $Group $Description
    if ($Before) {
        if ($null -eq $currentRule) { throw 'firewall_recovery_required' }
        $restore = $Before.Clone()
        $restore.InterfaceAlias = @($Before.InterfaceAlias | ForEach-Object { [Management.Automation.WildcardPattern]::Escape($_) })
        Set-NetFirewallRule @restore | Out-Null
        $restored = Get-OwnedRule $Name $Group $Description
        if ($null -eq $restored -or -not (Same-Snapshot (Snapshot-Rule $restored) $Before)) { throw 'firewall_recovery_required' }
    } elseif ($null -ne $currentRule) {
        Remove-NetFirewallRule -InputObject $currentRule | Out-Null
        if ($null -ne (Get-OwnedRule $Name $Group $Description)) { throw 'firewall_recovery_required' }
    }
}
function Update-OwnedRule($Desired, $Expected, $Before, [string]$Group, [string]$Description, [string]$DisplayName) {
    $currentRule = Get-OwnedRule $Desired.Name $Group $Description
    if ($Before) {
        if ($null -eq $currentRule -or -not (Same-Snapshot (Snapshot-Rule $currentRule) $Before)) { throw 'firewall_changed' }
    } elseif ($null -ne $currentRule) { throw 'firewall_changed' }
    # Final expiry check after all reads and immediately before the first write.
    # Expiration fails outside the rollback block: an unapplied request writes no rule.
    Assert-FreshRequest
    try {
        if ($Before) { Set-NetFirewallRule @Desired | Out-Null }
        else { New-NetFirewallRule @Desired -DisplayName $DisplayName -Group $Group -Description $Description | Out-Null }
        $applied = Get-OwnedRule $Desired.Name $Group $Description
        if ($null -eq $applied -or -not (Same-Snapshot (Snapshot-Rule $applied) $Expected)) { throw 'firewall_verification_failed' }
    } catch {
        try { Restore-OwnedRule $Before $Desired.Name $Group $Description }
        catch { throw 'firewall_recovery_required' }
        throw 'firewall_apply_failed'
    }
}
function Assert-HotspotAddress($Inspection, $Adapter, [string]$Address, [int]$Prefix, [int]$Index) {
    if ($Inspection.ok -ne $true -or $Inspection.inventory.hotspot.state -ne 'on') { throw 'hotspot_not_on' }
    if (@($Inspection.inventory.errors).Count) { throw 'hotspot_inspection_incomplete' }
    $adapterId = ([guid]$Adapter.InterfaceGuid).ToString('D')
    $matches = @($Inspection.inventory.addresses | Where-Object {
        $_.ip_address -eq $Address -and $_.prefix_length -eq $Prefix -and $_.interface_index -eq $Index -and
        $_.adapter_guid -eq $adapterId -and $_.adapter_status -eq 'Up' -and $_.address_state -eq 'Preferred' -and
        $_.skip_as_source -eq $false
    })
    if ($matches.Count -ne 1 -or @($Inspection.inventory.addresses | Where-Object ip_address -eq $Address).Count -ne 1) { throw 'network_changed' }
    $kind = [string]$matches[0].candidate_kind
    if ($kind -eq 'hotspot_transition_adapter') { return }
    if ($kind -ne 'wifi_direct_virtual_adapter' -or $Adapter.InterfaceDescription -notmatch '(?i)Wi[\s-]?Fi\s+Direct') { throw 'hotspot_interface_required' }
}
try {
    Assert-FreshRequest
    if ($request.port -ne 8766 -or $request.local_area -isnot [bool] -or
        [string]$request.prefix_length -notmatch '^\d+$' -or [string]$request.interface_index -notmatch '^\d+$') { throw 'invalid_firewall_request' }
    $hostAddress = [string]$request.host
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($hostAddress, [ref]$parsed) -or $parsed.AddressFamily -ne 'InterNetwork' -or $parsed.ToString() -ne $hostAddress) { throw 'invalid_firewall_address' }
    $octets = $parsed.GetAddressBytes()
    $minimumPrefix = if ($octets[0] -eq 10) {8} elseif ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) {12} elseif ($octets[0] -eq 192 -and $octets[1] -eq 168) {16} else {throw 'invalid_firewall_address'}
    $prefix = [int]$request.prefix_length
    if ($prefix -lt $minimumPrefix -or $prefix -gt 30) { throw 'invalid_firewall_subnet' }
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -eq $hostAddress -and $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource })
    if ($addresses.Count -ne 1 -or $addresses[0].InterfaceIndex -ne [int]$request.interface_index -or $addresses[0].PrefixLength -ne $prefix) { throw 'network_changed' }
    $adapters = @(Get-NetAdapter -IncludeHidden | Where-Object InterfaceIndex -eq $addresses[0].InterfaceIndex)
    if ($adapters.Count -ne 1 -or $adapters[0].Status -ne 'Up') { throw 'network_changed' }
    $ruleProfile = 'Private'
    if ($request.local_area) {
        # Fresh read verifies either a normal Direct interface or the separately
        # proven start-transition GUID. Description alone never accepts hardware.
        $inspectionShell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $inventoryJson = & $inspectionShell -NoProfile -NonInteractive -File (Join-Path $PSScriptRoot 'Network-Control.ps1') -Action inspect
        $inventoryResult = ($inventoryJson -join "`n") | ConvertFrom-Json
        Assert-HotspotAddress $inventoryResult $adapters[0] $hostAddress $prefix ([int]$request.interface_index)
        # ICS virtual interfaces may have no NLM category. IF/IP/subnet/program/port remain exact.
        $ruleProfile = 'Any'
    } else {
        $profiles = @(Get-NetConnectionProfile -InterfaceIndex $addresses[0].InterfaceIndex)
        if (-not $profiles.Count -or @($profiles | Where-Object NetworkCategory -ne 'Private').Count) { throw 'private_network_required' }
    }
    $python = Join-Path $workspace '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'python_unavailable' }
    $pythonJson = & $python -c "import json,sys; print(json.dumps({'image':sys._base_executable}))"
    if ($LASTEXITCODE -ne 0) { throw 'python_unavailable' }
    $image = [string](($pythonJson -join "`n" | ConvertFrom-Json).image)
    if (-not [IO.Path]::IsPathRooted($image) -or -not (Test-Path -LiteralPath $image -PathType Leaf)) { throw 'python_unavailable' }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $suffix = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($workspace.ToLowerInvariant())))).Replace('-','').Substring(0,16) }
    finally { $sha.Dispose() }
    $ruleName = 'GTBMS-Mobile-' + $suffix + '-8766'
    $group = 'GT BMS Mobile Viewer'
    $description = 'GTBMS mobile viewer; workspace=' + $suffix
    $networkOctets = for ($i=0;$i -lt 4;$i++) {
        $bits=[Math]::Min(8,[Math]::Max(0,$prefix-8*$i))
        $mask=if($bits -eq 0){0}else{256-[int][Math]::Pow(2,8-$bits)}
        [int]$octets[$i] -band $mask
    }
    $desired = @{
        Name=$ruleName;Profile=$ruleProfile;Enabled='True';Direction='Inbound';Action='Allow'
        EdgeTraversalPolicy='Block';Protocol='TCP';LocalPort=8766;RemotePort='Any'
        LocalAddress=$hostAddress;RemoteAddress=(($networkOctets -join '.')+'/'+$prefix)
        Program=[IO.Path]::GetFullPath($image)
        InterfaceAlias=[Management.Automation.WildcardPattern]::Escape([string]$addresses[0].InterfaceAlias)
    }
    $expected = $desired.Clone()
    $expected.InterfaceAlias = [string]$addresses[0].InterfaceAlias
    $existing = Get-OwnedRule $ruleName $group $description
    $before = $null
    if ($null -ne $existing) {
        $before = Snapshot-Rule $existing
        if (Same-Snapshot $before $expected) { Finish $true 'firewall_already_matches'; exit 0 }
    }
    if ($CheckOnly) { Finish $false 'firewall_update_required'; exit 0 }
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try { $administrator=([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
    finally { $identity.Dispose() }
    if (-not $administrator) {
        if ($Elevated) { throw 'administrator_required' }
        try {
            $shell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            $child=Start-Process -FilePath $shell -ArgumentList @('-NoProfile','-NonInteractive','-File',('"'+$PSCommandPath+'"'),'-RequestId',$RequestId,'-Elevated') -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        } catch { throw 'administrator_approval_cancelled' }
        if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) { throw 'firewall_result_missing' }
        $answer=Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
        Finish ([bool]$answer.ok) ([string]$answer.code)
        exit 0
    }
    Assert-FreshRequest
    if ($before) {
        $backup = Join-Path $requestDirectory ($RequestId + '.firewall-before.json')
        [IO.File]::WriteAllText($backup, ($before | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    }
    Update-OwnedRule $desired $expected $before $group $description ('GT BMS smartphone viewer ['+$suffix+']')
    Finish $true 'firewall_ready'
} catch {
    $code=[string]$_.Exception.Message
    if ($code -notmatch '^[a-z0-9_]{1,80}$') { $code='firewall_operation_failed' }
    Finish $false $code
}
