[CmdletBinding()]
param()

# Pure mock tests. Only function definitions are loaded from the production
# script; its main body, UAC, request files and real NetSecurity cmdlets never run.
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$path = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\Network-Firewall.ps1'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'production_parse_error' }
foreach ($definition in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    Invoke-Expression $definition.Extent.Text
}
$checks = 0
function Assert-Check([bool]$Value, [string]$Label) {
    if (-not $Value) { throw ('test_failed_' + $Label) }
    $script:checks++
}
function Assert-Throws([scriptblock]$Code, [string]$Expected) {
    $caught = ''
    try { & $Code } catch { $caught = $_.Exception.Message }
    Assert-Check ($caught -eq $Expected) $Expected
}

Assert-Check (Same-Value '192.168.55.0/255.255.255.0' '192.168.55.0/24' 'RemoteAddress') 'netmask_equivalence'
Assert-Check (Same-Value '192.168.55.34/24' '192.168.55.0/24' 'RemoteAddress') 'network_bits'
Assert-Check (Same-Value '192.168.55.1' '192.168.55.1/32' 'LocalAddress') 'host_equivalence'
Assert-Check (-not (Same-Value '192.168.55.0/23' '192.168.55.0/24' 'RemoteAddress')) 'different_scope'
Assert-Check (-not (Same-Value 'Any' '192.168.55.0/24' 'RemoteAddress')) 'any_not_subnet'
Assert-Check (Same-Value @('10.1.1.0/24', '192.168.55.0/24') @('192.168.55.0/255.255.255.0', '10.1.1.0/24') 'RemoteAddress') 'address_set_order'
Assert-Check (Same-Value '6' 'TCP' 'Protocol') 'protocol_number'
Assert-Check (-not (Same-Value @('alpha,beta') @('alpha', 'beta') 'InterfaceAlias')) 'array_collision'
Assert-Throws { Normalize-Address '192.168.55.0/255.0.255.0' } 'invalid_firewall_address_filter'
Assert-Throws { Normalize-Address '192.168.55.0/33' } 'invalid_firewall_address_filter'

# Exercise the real deadline check using mocked file reads, never a request file.
$originalFreshCheck = ${function:Assert-FreshRequest}
$requestPath = 'mock-request'
$mockRequestExists = $true
$mockExpiration = [DateTimeOffset]::UtcNow.AddSeconds(55).ToString('o')
function Test-Path { param($LiteralPath, $PathType) return $script:mockRequestExists }
function Get-Content { param($LiteralPath, [switch]$Raw) return (@{expires_at=$script:mockExpiration} | ConvertTo-Json -Compress) }
& $originalFreshCheck
Assert-Check ($request.expires_at -eq $mockExpiration) 'fresh_deadline'
$mockExpiration = [DateTimeOffset]::UtcNow.AddSeconds(-1).ToString('o')
Assert-Throws { & $originalFreshCheck } 'request_expired'
$mockExpiration = [DateTimeOffset]::UtcNow.AddMinutes(3).ToString('o')
Assert-Throws { & $originalFreshCheck } 'request_expired'
$mockRequestExists = $false
Assert-Throws { & $originalFreshCheck } 'request_expired'

$ruleName = 'GTBMS-Mobile-mock-8766'
$group = 'GT BMS Mobile Viewer'
$description = 'GTBMS mobile viewer; workspace=mock'
$literalAlias = 'Local Area Connection* 1[?]'
$expected = @{
    Name=$ruleName;Profile='Any';Enabled='True';Direction='Inbound';Action='Allow'
    EdgeTraversalPolicy='Block';Protocol='TCP';LocalPort=8766;RemotePort='Any'
    LocalAddress='192.168.55.1';RemoteAddress='192.168.55.0/24'
    Program='C:\Mock\python.exe';InterfaceAlias=$literalAlias
}
$desired = $expected.Clone()
$desired.InterfaceAlias = [Management.Automation.WildcardPattern]::Escape($literalAlias)
$pattern = [Management.Automation.WildcardPattern]::new($desired.InterfaceAlias)
Assert-Check ($pattern.IsMatch($literalAlias)) 'escaped_alias_matches_literal'
Assert-Check (-not $pattern.IsMatch('Local Area ConnectionABC 1x')) 'escaped_alias_not_wildcard'

function Reset-Mock {
    $script:mockRule = $null
    $script:newCalls = 0
    $script:setCalls = 0
    $script:removeCalls = 0
    $script:freshCalls = 0
    $script:expired = $false
    $script:behavior = 'normal'
}
function Assert-FreshRequest {
    $script:freshCalls++
    if ($script:expired) { throw 'request_expired' }
}
function Get-NetFirewallRule { param($Name, $ErrorAction) if ($null -ne $script:mockRule) { return $script:mockRule } }
function Snapshot-Rule($Rule) { return $Rule.snapshot.Clone() }
function New-MockRule($Snapshot) {
    return [pscustomobject]@{Name=$script:ruleName;Group=$script:group;Description=$script:description;snapshot=$Snapshot.Clone()}
}
function Store-MockSnapshot($Values) {
    $snapshot = @{}
    foreach ($key in $script:expected.Keys) { $snapshot[$key] = $Values[$key] }
    $snapshot.InterfaceAlias = @($Values.InterfaceAlias | ForEach-Object { [Management.Automation.WildcardPattern]::Unescape([string]$_) })
    # NetSecurity commonly emits a dotted mask after receiving CIDR.
    $snapshot.RemoteAddress = @($snapshot.RemoteAddress | ForEach-Object { ([string]$_).Replace('/24', '/255.255.255.0') })
    $script:mockRule = New-MockRule $snapshot
}
function New-NetFirewallRule {
    param($Name,$Profile,$Enabled,$Direction,$Action,$EdgeTraversalPolicy,$Protocol,$LocalPort,$RemotePort,$LocalAddress,$RemoteAddress,$Program,$InterfaceAlias,$DisplayName,$Group,$Description)
    $script:newCalls++
    Store-MockSnapshot $PSBoundParameters
    if ($script:behavior -eq 'new_partial_throw') { throw 'mock_partial_new_failure' }
    if ($script:behavior -eq 'new_ownership_changed') { $script:mockRule.Group = 'Someone else'; throw 'mock_partial_new_failure' }
}
function Set-NetFirewallRule {
    param($Name,$Profile,$Enabled,$Direction,$Action,$EdgeTraversalPolicy,$Protocol,$LocalPort,$RemotePort,$LocalAddress,$RemoteAddress,$Program,$InterfaceAlias)
    $script:setCalls++
    if ($script:behavior -eq 'rollback_silently_fails' -and $script:setCalls -eq 2) { return }
    Store-MockSnapshot $PSBoundParameters
    if ($script:behavior -in @('set_partial_throw', 'rollback_silently_fails') -and $script:setCalls -eq 1) { throw 'mock_partial_set_failure' }
}
function Remove-NetFirewallRule {
    param($InputObject)
    $script:removeCalls++
    Assert-Check ([object]::ReferenceEquals($InputObject,$script:mockRule)) 'remove_exact_owned_object'
    if ($script:behavior -eq 'remove_silently_fails') { return }
    $script:mockRule = $null
}

Reset-Mock
Update-OwnedRule $desired $expected $null $group $description 'mock'
Assert-Check ($newCalls -eq 1 -and $setCalls -eq 0 -and $removeCalls -eq 0 -and $freshCalls -eq 1) 'new_success'
Assert-Check (Same-Snapshot (Snapshot-Rule $mockRule) $expected) 'new_readback_semantics'

Reset-Mock
$behavior = 'new_partial_throw'
Assert-Throws { Update-OwnedRule $desired $expected $null $group $description 'mock' } 'firewall_apply_failed'
Assert-Check ($newCalls -eq 1 -and $removeCalls -eq 1 -and $null -eq $mockRule) 'partial_new_removed'

Reset-Mock
$behavior = 'new_ownership_changed'
Assert-Throws { Update-OwnedRule $desired $expected $null $group $description 'mock' } 'firewall_recovery_required'
Assert-Check ($removeCalls -eq 0 -and $null -ne $mockRule) 'do_not_remove_foreign_rule'

Reset-Mock
$before = $expected.Clone()
$before.Profile = 'Private'
$before.LocalAddress = '192.168.44.1'
$before.RemoteAddress = '192.168.44.0/255.255.255.0'
$mockRule = New-MockRule $before
$behavior = 'set_partial_throw'
Assert-Throws { Update-OwnedRule $desired $expected $before $group $description 'mock' } 'firewall_apply_failed'
Assert-Check ($setCalls -eq 2 -and $removeCalls -eq 0 -and (Same-Snapshot (Snapshot-Rule $mockRule) $before)) 'partial_set_restored'
Assert-Check ($before.InterfaceAlias -eq $literalAlias) 'before_snapshot_not_mutated'

Reset-Mock
$mockRule = New-MockRule $before
$behavior = 'rollback_silently_fails'
Assert-Throws { Update-OwnedRule $desired $expected $before $group $description 'mock' } 'firewall_recovery_required'
Assert-Check ($setCalls -eq 2) 'rollback_verification'

Reset-Mock
$expired = $true
Assert-Throws { Update-OwnedRule $desired $expected $null $group $description 'mock' } 'request_expired'
Assert-Check ($newCalls -eq 0 -and $setCalls -eq 0 -and $removeCalls -eq 0) 'late_uac_no_mutation'

Reset-Mock
$mockRule = New-MockRule $before
$mockRule.Group = 'Someone else'
Assert-Throws { Update-OwnedRule $desired $expected $before $group $description 'mock' } 'firewall_ownership_mismatch'
Assert-Check ($newCalls -eq 0 -and $setCalls -eq 0 -and $removeCalls -eq 0) 'foreign_rule_untouched'

Reset-Mock
$mockRule = New-MockRule $expected
Assert-Throws { Update-OwnedRule $desired $expected $null $group $description 'mock' } 'firewall_changed'
Assert-Check ($newCalls -eq 0 -and $setCalls -eq 0 -and $removeCalls -eq 0) 'concurrent_creation_untouched'

Reset-Mock
$mockRule = New-MockRule $before
$mockRule.snapshot.LocalPort = 9876
Assert-Throws { Update-OwnedRule $desired $expected $before $group $description 'mock' } 'firewall_changed'
Assert-Check ($setCalls -eq 0 -and $removeCalls -eq 0) 'concurrent_edit_untouched'

# Fresh hotspot inventory is mandatory even for the hardware compatibility path.
function New-HotspotInspection {
    return [pscustomobject]@{ok=$true;inventory=[pscustomobject]@{
        hotspot=[pscustomobject]@{state='on'};errors=@();addresses=@([pscustomobject]@{
            ip_address='192.168.55.1';prefix_length=24;interface_index=17
            adapter_guid='22222222-2222-2222-2222-222222222222';adapter_status='Up'
            address_state='Preferred';skip_as_source=$false;candidate_kind='hotspot_transition_adapter'
        })
    }}
}
$adapter=[pscustomobject]@{InterfaceGuid='22222222-2222-2222-2222-222222222222';InterfaceDescription='Mock hardware Wi-Fi'}
$inspection=New-HotspotInspection
Assert-HotspotAddress $inspection $adapter '192.168.55.1' 24 17
Assert-Check $true 'verified_hardware_transition_accepted'
$inspection.inventory.addresses[0].candidate_kind='wifi_direct_virtual_adapter'
Assert-Throws { Assert-HotspotAddress $inspection $adapter '192.168.55.1' 24 17 } 'hotspot_interface_required'
$adapter.InterfaceDescription='Microsoft Wi-Fi Direct Virtual Adapter'
Assert-HotspotAddress $inspection $adapter '192.168.55.1' 24 17
Assert-Check $true 'direct_description_accepted'
$inspection.inventory.addresses[0].candidate_kind='private_ipv4'
Assert-Throws { Assert-HotspotAddress $inspection $adapter '192.168.55.1' 24 17 } 'hotspot_interface_required'
$inspection=New-HotspotInspection;$inspection.inventory.hotspot.state='off'
Assert-Throws { Assert-HotspotAddress $inspection $adapter '192.168.55.1' 24 17 } 'hotspot_not_on'
$inspection=New-HotspotInspection;$inspection.inventory.errors=@('mock_error')
Assert-Throws { Assert-HotspotAddress $inspection $adapter '192.168.55.1' 24 17 } 'hotspot_inspection_incomplete'
foreach ($change in @(
    @{field='adapter_guid';value='33333333-3333-3333-3333-333333333333'},
    @{field='interface_index';value=99}, @{field='ip_address';value='192.168.55.2'},
    @{field='prefix_length';value=25}, @{field='adapter_status';value='Disconnected'},
    @{field='address_state';value='Tentative'}, @{field='skip_as_source';value=$true}
)) {
    $inspection=New-HotspotInspection;$inspection.inventory.addresses[0].($change.field)=$change.value
    Assert-Throws { Assert-HotspotAddress $inspection $adapter '192.168.55.1' 24 17 } 'network_changed'
}
$inspection=New-HotspotInspection;$inspection.inventory.addresses += $inspection.inventory.addresses[0]
Assert-Throws { Assert-HotspotAddress $inspection $adapter '192.168.55.1' 24 17 } 'network_changed'

[pscustomobject]@{ok=$true;checks=$checks;real_firewall_mutations=0;uac_prompts=0;powershell=$PSVersionTable.PSVersion.ToString()} | ConvertTo-Json -Compress
