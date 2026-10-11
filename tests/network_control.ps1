[CmdletBinding()]
param()

# Pure mocks: load definitions only, never the production main body or OS APIs.
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$path = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\Network-Control.ps1'
$tokens = $null; $parseErrors = $null
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
function Copy-Value($Value) { return ($Value | ConvertTo-Json -Depth 12 -Compress | ConvertFrom-Json) }
$upstreamId = '11111111-1111-1111-1111-111111111111'
$privateId = '22222222-2222-2222-2222-222222222222'
$otherId = '33333333-3333-3333-3333-333333333333'
$profileHash = Get-ProfileHash 'mock-profile'
$before = @{
    state='Off';upstream_guid=$upstreamId;upstream_pnp='MOCK_PNP';transition_capable=$true;upstream_profile_hash=$profileHash
    stations=@([pscustomobject]@{interface_id=$upstreamId;state='connected';current_profile='mock-profile'})
    adapters=@(
        [pscustomobject]@{guid=$upstreamId;index=20;pnp='MOCK_PNP';interface_type=71;status='Up';description='Mock Wi-Fi'}
        [pscustomobject]@{guid=$privateId;index=17;pnp='MOCK_PNP';interface_type=71;status='Up';description='Mock Wi-Fi'}
    )
    addresses=@([pscustomobject]@{guid=$upstreamId;index=20;ip='192.168.44.20';prefix=24})
}
$after = Copy-Value $before
$after.state = 'On'
$after.addresses += [pscustomobject]@{guid=$privateId;index=17;ip='192.168.55.1';prefix=24}
$mapping = @{
    version=1;source='start_transition';adapter_guid=$privateId;interface_index=17;ip_address='192.168.55.1';prefix_length=24
    pnp_device_id='MOCK_PNP';upstream_adapter_guid=$upstreamId;upstream_profile_hash=$profileHash
}
Assert-Check ((Get-VerifiedTransitionAddress $mapping $after).guid -eq $privateId) 'valid_proof'
Assert-Check ((Find-TransitionAddress $before $after).guid -eq $privateId) 'unique_new_address'
Assert-Check ((Get-ProfileHash 'mock-profile') -cmatch '^[0-9a-f]{64}$') 'profile_hash_format'
Assert-Check ((Get-ProfileHash 'mock-profile') -cne (Get-ProfileHash 'Mock-profile')) 'profile_hash_case_sensitive'

foreach ($change in @(
    @{field='version';value=2}, @{field='source';value='invalidated'},
    @{field='adapter_guid';value=$otherId}, @{field='adapter_guid';value=$upstreamId},
    @{field='interface_index';value=99}, @{field='ip_address';value='192.168.55.2'},
    @{field='prefix_length';value=25}, @{field='pnp_device_id';value='OTHER'},
    @{field='upstream_adapter_guid';value=$otherId}, @{field='upstream_profile_hash';value='OTHER'}
)) {
    $bad = Copy-Value $mapping; $bad.($change.field) = $change.value
    Assert-Check ($null -eq (Get-VerifiedTransitionAddress $bad $after)) ('mapping_reject_' + $change.field)
}
foreach ($change in @(
    @{field='state';value='Off'}, @{field='transition_capable';value=$false},
    @{field='upstream_guid';value=$otherId}, @{field='upstream_profile_hash';value='OTHER'},
    @{field='upstream_pnp';value='OTHER'}
)) {
    $bad = Copy-Value $after; $bad.($change.field) = $change.value
    Assert-Check ($null -eq (Get-VerifiedTransitionAddress $mapping $bad)) ('snapshot_reject_' + $change.field)
}
foreach ($change in @(@{field='pnp';value='OTHER'},@{field='status';value='Disconnected'},@{field='interface_type';value=6})) {
    $bad = Copy-Value $after; $bad.adapters[1].($change.field) = $change.value
    Assert-Check ($null -eq (Get-VerifiedTransitionAddress $mapping $bad)) ('adapter_reject_' + $change.field)
}
$bad = Copy-Value $after; $bad.stations += [pscustomobject]@{interface_id=$privateId;state='disconnected';current_profile=$null}
Assert-Check ($null -eq (Get-VerifiedTransitionAddress $mapping $bad)) 'station_exclusion'
$bad = Copy-Value $after; $bad.addresses += [pscustomobject]@{guid=$otherId;index=88;ip='192.168.55.1';prefix=24}
Assert-Check ($null -eq (Get-VerifiedTransitionAddress $mapping $bad)) 'duplicate_ip_rejected'
Assert-Check ($null -eq (Get-VerifiedTransitionAddress @{version=1} $after)) 'malformed_mapping_rejected'
Assert-Check ($null -eq (Find-TransitionAddress $after $after)) 'no_off_baseline'
$same = Copy-Value $after; $same.state = 'Off'
Assert-Check ($null -eq (Find-TransitionAddress $same $after)) 'preexisting_address_rejected'
$same.addresses[1].prefix=25
Assert-Check ($null -eq (Find-TransitionAddress $same $after)) 'prefix_only_change_not_new_address'
$bad = Copy-Value $after; $bad.upstream_profile_hash='OTHER'
Assert-Throws { Find-TransitionAddress $before $bad } 'hotspot_upstream_changed'
$bad = Copy-Value $after; $bad.transition_capable=$false
Assert-Throws { Find-TransitionAddress $before $bad } 'hotspot_upstream_not_verified'
$bad = Copy-Value $after
$bad.adapters += [pscustomobject]@{guid=$otherId;index=18;pnp='MOCK_PNP';interface_type=71;status='Up';description='Mock Wi-Fi'}
$bad.addresses += [pscustomobject]@{guid=$otherId;index=18;ip='192.168.66.1';prefix=24}
Assert-Throws { Find-TransitionAddress $before $bad } 'hotspot_adapter_ambiguous'

$direct = Copy-Value $after
$direct.transition_capable=$false
$direct.adapters[0].interface_type=6
$direct.adapters[1].description='Microsoft Wi-Fi Direct Virtual Adapter'
$direct.stations=@()
Assert-Check ((Get-DirectHotspotAddress $direct).guid -eq $privateId) 'ethernet_direct_supported'
Assert-Check ($null -eq (Get-DirectHotspotAddress $after)) 'hardware_description_not_direct'
$bad = Copy-Value $direct; $bad.state='Off'
Assert-Check ($null -eq (Get-DirectHotspotAddress $bad)) 'off_not_direct'
$bad = Copy-Value $direct; $bad.adapters[1].status='Disconnected'
Assert-Check ($null -eq (Get-DirectHotspotAddress $bad)) 'down_not_direct'
$bad = Copy-Value $direct
$bad.adapters += [pscustomobject]@{guid=$otherId;index=18;pnp='OTHER';interface_type=71;status='Up';description='Wi-Fi Direct Virtual Adapter'}
$bad.addresses += [pscustomobject]@{guid=$otherId;index=18;ip='192.168.66.1';prefix=24}
Assert-Throws { Get-DirectHotspotAddress $bad } 'hotspot_adapter_ambiguous'

# Verify snapshot construction with mocked OS reads and a non-native test type.
$originalSnapshot = ${function:Get-HotspotEvidenceSnapshot}
Add-Type -TypeDefinition 'namespace GTBmsNetwork { public static class NativeWifi { public static object Current; public static object Inspect() { return Current; } } }'
function Initialize-NativeWifi { }
$mockStations = [Collections.Generic.List[object]]::new()
$mockStations.Add([pscustomobject]@{interface_id=$upstreamId;state='connected';current_profile='mock-profile'})
[GTBmsNetwork.NativeWifi]::Current = [pscustomobject]@{errors=[Collections.Generic.List[string]]::new();wifi_interfaces=$mockStations}
$mockContext = @{manager=[pscustomobject]@{TetheringOperationalState='On'};profile=[pscustomobject]@{NetworkAdapter=[pscustomobject]@{NetworkAdapterId=$upstreamId};ProfileName='mock-profile'}}
$mockAdapters = @($after.adapters | ForEach-Object { [pscustomobject]@{InterfaceGuid=$_.guid;InterfaceIndex=$_.index;PnPDeviceID=$_.pnp;InterfaceType=$_.interface_type;Status=$_.status;InterfaceDescription=$_.description} })
$mockAddresses = @($after.addresses | ForEach-Object { [pscustomobject]@{IPAddress=$_.ip;PrefixLength=$_.prefix;InterfaceIndex=$_.index;AddressState='Preferred';SkipAsSource=$false} })
function Get-TetheringContext { return $script:mockContext }
function Get-NetAdapter { param([switch]$IncludeHidden,$ErrorAction) return $script:mockAdapters }
function Get-NetIPAddress { param($AddressFamily,$ErrorAction) return $script:mockAddresses }
$snapshot = & $originalSnapshot
Assert-Check ($snapshot.transition_capable -and $snapshot.addresses.Count -eq 2) 'snapshot_wifi_capable'
$mockAdapters[0].InterfaceType=6; $mockStations.Clear()
$snapshot = & $originalSnapshot
Assert-Check (-not $snapshot.transition_capable -and $snapshot.addresses.Count -eq 2) 'snapshot_ethernet_allowed'
$mockAddresses[1].SkipAsSource=$true
$snapshot = & $originalSnapshot
Assert-Check ($snapshot.addresses.Count -eq 1) 'snapshot_skip_source_excluded'
$mockAddresses[1].SkipAsSource=$false; $mockAddresses[1].AddressState='Tentative'
$snapshot = & $originalSnapshot
Assert-Check ($snapshot.addresses.Count -eq 1) 'snapshot_tentative_excluded'
[GTBmsNetwork.NativeWifi]::Current.errors.Add('mock_error')
Assert-Throws { & $originalSnapshot } 'hotspot_evidence_unavailable'

# Exercise the real atomic persistence against a unique scratch file. Never
# write the operational mapping. This catches .NET / PS5 null-string behavior.
$originalMapPath = ${function:Get-HotspotMapPath}
$script:testMapPath = Join-Path ([IO.Path]::GetTempPath()) ('gtbms-network-proof-test-' + [guid]::NewGuid().ToString('N') + '.json')
function Get-HotspotMapPath { return $script:testMapPath }
try {
    Save-HotspotMapping $mapping
    Assert-Check ((Read-HotspotMapping).source -eq 'start_transition') 'mapping_first_save_readback'
    Clear-HotspotMapping
    Assert-Check ((Read-HotspotMapping).source -eq 'invalidated') 'mapping_atomic_invalidation'
    Save-HotspotMapping $mapping
    Assert-Check ($null -ne (Get-VerifiedTransitionAddress (Read-HotspotMapping) $after)) 'mapping_atomic_replacement'
} finally {
    if ([IO.File]::Exists($script:testMapPath)) { [IO.File]::Delete($script:testMapPath) }
    ${function:Get-HotspotMapPath} = $originalMapPath
}

# Wrapper calls are completely mocked. Never call WinRT or touch a real map.
function Reset-Operation($Snapshots, $ExistingMapping=$null) {
    $script:snapshotQueue = [Collections.Generic.Queue[object]]::new()
    foreach ($item in $Snapshots) { $script:snapshotQueue.Enqueue($item) }
    $script:existingMapping=$ExistingMapping
    $script:calls=[Collections.Generic.List[bool]]::new()
    $script:saved=@();$script:clears=0;$script:sleeps=0
    $script:startResult=@{ok=$true;code='hotspot_started'}
    $script:stopResult=@{ok=$true;code='hotspot_stopped'}
}
function Get-HotspotEvidenceSnapshot {
    if (-not $script:snapshotQueue.Count) { throw 'hotspot_evidence_unavailable' }
    return $script:snapshotQueue.Dequeue()
}
function Read-HotspotMapping { return $script:existingMapping }
function Clear-HotspotMapping { $script:clears++;$script:existingMapping=$null }
function Save-HotspotMapping($Mapping) { $script:saved += $Mapping }
function Invoke-TetheringCall([bool]$Start) {
    $script:calls.Add($Start)
    if ($Start) { return $script:startResult }; return $script:stopResult
}
function Start-Sleep { param($Milliseconds) $script:sleeps++ }

Reset-Operation @($before,$after,$after)
$result=Invoke-HotspotOperation $true
Assert-Check ($result.ok -and $result.identification -eq 'start_transition') 'start_transition_succeeds'
Assert-Check ($calls.Count -eq 1 -and $calls[0] -and $saved.Count -eq 1 -and $sleeps -eq 1) 'two_observations_required'
Assert-Check ($null -ne (Get-VerifiedTransitionAddress $saved[0] $after)) 'saved_mapping_verifies'
Assert-Check ($saved[0].Keys -notcontains 'current_profile') 'no_profile_name_in_mapping'
Reset-Operation @($after) $mapping
$result=Invoke-HotspotOperation $true
Assert-Check ($result.ok -and $result.code -eq 'hotspot_already_on' -and $calls.Count -eq 0 -and $clears -eq 0) 'valid_mapping_keeps_hotspot'
Reset-Operation @($after,$before,$after,$after)
$result=Invoke-HotspotOperation $true
Assert-Check ($result.ok -and $calls.Count -eq 2 -and -not $calls[0] -and $calls[1]) 'unproven_on_establishes_baseline'
Reset-Operation @($after)
$stopResult=@{ok=$false;code='hotspot_operation_failed'}
$result=Invoke-HotspotOperation $true
Assert-Check (-not $result.ok -and $calls.Count -eq 1 -and -not $calls[0] -and $saved.Count -eq 0) 'stop_failure_no_start'
Reset-Operation @($before)
$startResult=@{ok=$false;code='hotspot_operation_timeout';operation_may_complete=$true}
$result=Invoke-HotspotOperation $true
Assert-Check (-not $result.ok -and $result.operation_may_complete -and $saved.Count -eq 0) 'timeout_never_proves_mapping'
Reset-Operation @($before,$after)
$result=Invoke-HotspotOperation $true
Assert-Check (-not $result.ok -and $saved.Count -eq 0) 'single_observation_insufficient'
Reset-Operation @($direct)
$result=Invoke-HotspotOperation $true
Assert-Check ($result.ok -and $calls.Count -eq 0) 'ethernet_direct_already_on_no_restart'
$ethernetOff=Copy-Value $direct;$ethernetOff.state='Off';$ethernetOff.addresses=@($ethernetOff.addresses[0])
$ethernetPending=Copy-Value $ethernetOff;$ethernetPending.state='On'
Reset-Operation @($ethernetOff,$ethernetPending,$direct)
$result=Invoke-HotspotOperation $true
Assert-Check ($result.ok -and $result.identification -eq 'wifi_direct_description' -and $saved.Count -eq 0 -and $sleeps -eq 1) 'ethernet_direct_waits_for_ipv4'
Reset-Operation @()
$result=Invoke-HotspotOperation $false
Assert-Check ($result.ok -and $clears -eq 1 -and $calls.Count -eq 1 -and -not $calls[0]) 'stop_invalidates_mapping'
Reset-Operation @()
$stopResult=@{ok=$false;code='hotspot_operation_failed'}
$result=Invoke-HotspotOperation $false
Assert-Check (-not $result.ok -and $clears -eq 0) 'failed_stop_keeps_proof_for_revalidation'

[pscustomobject]@{ok=$true;checks=$checks;real_network_mutations=0;operational_mapping_writes=0;scratch_mapping_writes=3;powershell=$PSVersionTable.PSVersion.ToString()} | ConvertTo-Json -Compress
