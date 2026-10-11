[CmdletBinding()]
param(
    [ValidateSet('inspect', 'connect', 'start_hotspot', 'stop_hotspot', 'open_settings')]
    [string]$Action = 'inspect'
)

# Windows PowerShell 5.1 bridge. JSON stdout only; never serialize WinRT objects,
# profile XML, credentials, BSSID/MAC addresses, or raw exception messages.
# connect accepts UTF-8 stdin JSON: {"profile":"saved name","interface_id":"GUID"}.
# A successful WlanConnect means request accepted, not connection established.
# The caller must inspect current_profile and fresh Preferred IPv4 afterwards.
# API references:
# https://learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlangetprofilelist
# https://learn.microsoft.com/en-us/windows/win32/api/wlanapi/nf-wlanapi-wlanconnect
# https://learn.microsoft.com/en-us/windows/win32/api/wlanapi/ns-wlanapi-wlan_connection_attributes
# https://learn.microsoft.com/en-us/uwp/api/windows.networking.networkoperators.networkoperatortetheringmanager
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Write-Result($Result) {
    [Console]::WriteLine(($Result | ConvertTo-Json -Depth 8 -Compress))
}

if ($PSVersionTable.PSVersion.Major -ne 5) {
    Write-Result @{ ok = $false; code = 'windows_powershell_51_required' }
    exit 0
}

function Initialize-NativeWifi {
    if ('GTBmsNetwork.NativeWifi' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace GTBmsNetwork {
    public sealed class WifiProfile {
        public string name;
        public string interface_id;
        public string interface_name;
    }
    public sealed class WifiInterface {
        public string interface_id;
        public string interface_name;
        public string state;
        public string current_profile;
    }
    public sealed class WifiInventory {
        public List<WifiProfile> wifi_profiles = new List<WifiProfile>();
        public List<WifiInterface> wifi_interfaces = new List<WifiInterface>();
        public List<string> errors = new List<string>();
    }
    public static class NativeWifi {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct InterfaceInfo {
            public Guid id;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string description;
            public int state;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct ProfileInfo {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string name;
            public uint flags;
        }
        // Only the documented first 520 bytes are marshalled. Association and
        // security attributes (including BSSID/MAC) are not read or exposed.
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct ConnectionPrefix {
            public int state;
            public int mode;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string profile;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct ConnectionParameters {
            public int mode;
            [MarshalAs(UnmanagedType.LPWStr)] public string profile;
            public IntPtr ssid;
            public IntPtr desiredBssidList;
            public int bssType;
            public uint flags;
        }
        [DllImport("wlanapi.dll")] private static extern uint WlanOpenHandle(uint version, IntPtr reserved, out uint negotiated, out IntPtr handle);
        [DllImport("wlanapi.dll")] private static extern uint WlanCloseHandle(IntPtr handle, IntPtr reserved);
        [DllImport("wlanapi.dll")] private static extern uint WlanEnumInterfaces(IntPtr handle, IntPtr reserved, out IntPtr list);
        [DllImport("wlanapi.dll")] private static extern uint WlanGetProfileList(IntPtr handle, ref Guid id, IntPtr reserved, out IntPtr list);
        [DllImport("wlanapi.dll")] private static extern uint WlanQueryInterface(IntPtr handle, ref Guid id, int opcode, IntPtr reserved, out uint size, out IntPtr data, out int valueType);
        [DllImport("wlanapi.dll", CharSet = CharSet.Unicode)] private static extern uint WlanConnect(IntPtr handle, ref Guid id, ref ConnectionParameters parameters, IntPtr reserved);
        [DllImport("wlanapi.dll")] private static extern void WlanFreeMemory(IntPtr pointer);

        private static string State(int value) {
            string[] states = { "not_ready", "connected", "ad_hoc_network_formed", "disconnecting", "disconnected", "associating", "discovering", "authenticating" };
            return value >= 0 && value < states.Length ? states[value] : "unknown";
        }
        private static int Count(IntPtr list) {
            int count = Marshal.ReadInt32(list);
            if (count < 0 || count > 4096) throw new InvalidOperationException();
            return count;
        }
        private static WifiInventory InspectHandle(IntPtr handle) {
            WifiInventory result = new WifiInventory();
            IntPtr interfaces = IntPtr.Zero;
            uint rc = WlanEnumInterfaces(handle, IntPtr.Zero, out interfaces);
            try {
                if (rc != 0) { result.errors.Add("wlan_interfaces_" + rc); return result; }
                int count = Count(interfaces);
                int stride = Marshal.SizeOf(typeof(InterfaceInfo));
                for (int n = 0; n < count; n++) {
                    InterfaceInfo item = (InterfaceInfo)Marshal.PtrToStructure(IntPtr.Add(interfaces, 8 + n * stride), typeof(InterfaceInfo));
                    WifiInterface view = new WifiInterface { interface_id = item.id.ToString("D"), interface_name = item.description, state = State(item.state), current_profile = null };
                    if (item.state == 1 || item.state == 2) {
                        IntPtr data = IntPtr.Zero;
                        uint size;
                        int kind;
                        rc = WlanQueryInterface(handle, ref item.id, 7, IntPtr.Zero, out size, out data, out kind);
                        try {
                            if (rc == 0 && data != IntPtr.Zero && size >= Marshal.SizeOf(typeof(ConnectionPrefix))) {
                                ConnectionPrefix current = (ConnectionPrefix)Marshal.PtrToStructure(data, typeof(ConnectionPrefix));
                                view.state = State(current.state);
                                if (current.state == 1 || current.state == 2) view.current_profile = current.profile;
                            } else { result.errors.Add("wlan_current_connection_" + rc); }
                        } finally { if (data != IntPtr.Zero) WlanFreeMemory(data); }
                    }
                    result.wifi_interfaces.Add(view);
                    IntPtr profiles = IntPtr.Zero;
                    rc = WlanGetProfileList(handle, ref item.id, IntPtr.Zero, out profiles);
                    try {
                        if (rc != 0) { result.errors.Add("wlan_profiles_" + rc); continue; }
                        int profileCount = Count(profiles);
                        int profileStride = Marshal.SizeOf(typeof(ProfileInfo));
                        for (int p = 0; p < profileCount; p++) {
                            ProfileInfo profile = (ProfileInfo)Marshal.PtrToStructure(IntPtr.Add(profiles, 8 + p * profileStride), typeof(ProfileInfo));
                            result.wifi_profiles.Add(new WifiProfile { name = profile.name, interface_id = item.id.ToString("D"), interface_name = item.description });
                        }
                    } finally { if (profiles != IntPtr.Zero) WlanFreeMemory(profiles); }
                }
            } finally { if (interfaces != IntPtr.Zero) WlanFreeMemory(interfaces); }
            return result;
        }
        public static WifiInventory Inspect() {
            IntPtr handle = IntPtr.Zero;
            uint negotiated;
            uint rc = WlanOpenHandle(2, IntPtr.Zero, out negotiated, out handle);
            try {
                if (rc != 0) {
                    WifiInventory result = new WifiInventory();
                    result.errors.Add("wlan_open_" + rc);
                    return result;
                }
                return InspectHandle(handle);
            } finally { if (handle != IntPtr.Zero) WlanCloseHandle(handle, IntPtr.Zero); }
        }
        public static string ConnectSaved(string profile, Guid id) {
            if (id == Guid.Empty || String.IsNullOrEmpty(profile) || profile.Length > 255) return "invalid_connect_input";
            foreach (char c in profile) if (Char.IsControl(c)) return "invalid_connect_input";
            IntPtr handle = IntPtr.Zero;
            uint negotiated;
            uint rc = WlanOpenHandle(2, IntPtr.Zero, out negotiated, out handle);
            try {
                if (rc != 0) return "wlan_open_" + rc;
                WifiInventory inventory = InspectHandle(handle);
                bool found = false;
                foreach (WifiProfile saved in inventory.wifi_profiles) {
                    if (String.Equals(saved.interface_id, id.ToString("D"), StringComparison.OrdinalIgnoreCase) && String.Equals(saved.name, profile, StringComparison.Ordinal)) { found = true; break; }
                }
                if (!found) return "saved_profile_not_found";
                ConnectionParameters parameters = new ConnectionParameters { mode = 0, profile = profile, ssid = IntPtr.Zero, desiredBssidList = IntPtr.Zero, bssType = 1, flags = 0 };
                rc = WlanConnect(handle, ref id, ref parameters, IntPtr.Zero);
                return rc == 0 ? "connect_requested" : "wlan_connect_" + rc;
            } finally { if (handle != IntPtr.Zero) WlanCloseHandle(handle, IntPtr.Zero); }
        }
    }
}
'@
}

function Test-PrivateIPv4([string]$Address) {
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsed)) { return $false }
    if ($parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $parsed.ToString() -ne $Address) { return $false }
    $octets = $parsed.GetAddressBytes()
    return ($octets[0] -eq 10 -or ($octets[0] -eq 172 -and $octets[1] -ge 16 -and $octets[1] -le 31) -or ($octets[0] -eq 192 -and $octets[1] -eq 168))
}

function Get-TetheringContext {
    try {
        Add-Type -AssemblyName System.Runtime.WindowsRuntime
        $null = [Windows.Networking.Connectivity.NetworkInformation, Windows, ContentType = WindowsRuntime]
        $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows, ContentType = WindowsRuntime]
        $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
        if ($null -eq $profile) { return @{ manager = $null; code = 'hotspot_no_upstream_profile' } }
        $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
        return @{ manager = $manager; profile = $profile; code = 'ok' }
    } catch { return @{ manager = $null; code = 'hotspot_api_unavailable' } }
}

function Get-ProfileHash([string]$Profile) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Profile)))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-HotspotEvidenceSnapshot {
    # Read-only evidence. PnP IDs and profile hashes remain in the local proof
    # file; they are not returned to the viewer or general network inventory.
    $context = Get-TetheringContext
    if ($null -eq $context.manager) { throw 'hotspot_evidence_unavailable' }
    $state = [string]$context.manager.TetheringOperationalState
    $upstreamId = ([guid]$context.profile.NetworkAdapter.NetworkAdapterId).ToString('D')
    $rawAdapters = @(Get-NetAdapter -IncludeHidden -ErrorAction Stop |
        Select-Object InterfaceGuid, InterfaceIndex, PnPDeviceID, InterfaceType, InterfaceDescription, Status)
    $adapters = @(
        foreach ($adapter in $rawAdapters) {
            [pscustomobject]@{
                guid = ([guid]$adapter.InterfaceGuid).ToString('D'); index = [int]$adapter.InterfaceIndex
                pnp = [string]$adapter.PnPDeviceID; interface_type = [int]$adapter.InterfaceType; status = [string]$adapter.Status
                description = [string]$adapter.InterfaceDescription
            }
        }
    )
    Initialize-NativeWifi
    $wifi = [GTBmsNetwork.NativeWifi]::Inspect()
    if ($wifi.errors.Count) { throw 'hotspot_evidence_unavailable' }
    $stations = @($wifi.wifi_interfaces.ToArray())
    $upstreamStations = @($stations | Where-Object { $_.interface_id -eq $upstreamId -and $_.state -eq 'connected' -and -not [string]::IsNullOrEmpty($_.current_profile) })
    $upstreamAdapters = @($adapters | Where-Object guid -eq $upstreamId)
    if ($upstreamAdapters.Count -ne 1) { throw 'hotspot_evidence_unavailable' }
    # Only the compatibility path requires a WLAN upstream. A standard Direct
    # hotspot may share Ethernet and must retain its existing detection path.
    $transitionCapable = $upstreamStations.Count -eq 1 -and $upstreamAdapters[0].interface_type -eq 71 -and
        $upstreamAdapters[0].status -eq 'Up' -and -not [string]::IsNullOrEmpty($upstreamAdapters[0].pnp)
    $upstreamProfile = if ($upstreamStations.Count -eq 1) { $upstreamStations[0].current_profile } else { [string]$context.profile.ProfileName }
    $rawAddresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
        Select-Object IPAddress, PrefixLength, InterfaceIndex, AddressState, SkipAsSource)
    $addresses = @(
        foreach ($address in $rawAddresses) {
            if ([string]$address.AddressState -ne 'Preferred' -or $address.SkipAsSource -or
                -not (Test-PrivateIPv4 ([string]$address.IPAddress))) { continue }
            $matches = @($adapters | Where-Object index -eq $address.InterfaceIndex)
            if ($matches.Count -ne 1) { throw 'hotspot_evidence_unavailable' }
            [pscustomobject]@{ guid=$matches[0].guid; index=[int]$address.InterfaceIndex;
                ip=[string]$address.IPAddress; prefix=[int]$address.PrefixLength }
        }
    )
    # Re-fetch the upstream context after the snapshot to reject a concurrent switch.
    $after = Get-TetheringContext
    if ($null -eq $after.manager -or [string]$after.manager.TetheringOperationalState -ne $state -or
        ([guid]$after.profile.NetworkAdapter.NetworkAdapterId).ToString('D') -ne $upstreamId) { throw 'hotspot_evidence_changed' }
    return @{ state=$state; upstream_guid=$upstreamId; upstream_pnp=$upstreamAdapters[0].pnp; transition_capable=$transitionCapable;
        upstream_profile_hash=(Get-ProfileHash $upstreamProfile);
        stations=$stations; adapters=$adapters; addresses=$addresses }
}

function Get-HotspotMapPath {
    return Join-Path (Split-Path $PSScriptRoot -Parent) 'logs\network-hotspot-adapter.json'
}
function Read-HotspotMapping {
    try {
        $path = Get-HotspotMapPath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -gt 16384) { return $null }
        return ([IO.File]::ReadAllText($path) | ConvertFrom-Json -ErrorAction Stop)
    } catch { return $null }
}
function Save-HotspotMapping($Mapping) {
    $path = Get-HotspotMapPath
    $null = [IO.Directory]::CreateDirectory((Split-Path $path -Parent))
    $temporary = $path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($Mapping | ConvertTo-Json -Depth 5 -Compress), [Text.UTF8Encoding]::new($false))
        # Windows PowerShell 5.1 converts $null to an empty string for this .NET
        # overload; NullString is required for the optional backup filename.
        if ([IO.File]::Exists($path)) { [IO.File]::Replace($temporary, $path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $path) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}
function Clear-HotspotMapping {
    # Invalidate atomically. A previous proof must not survive an unverified restart.
    Save-HotspotMapping @{ version=1; source='invalidated'; invalidated_at_utc=[DateTime]::UtcNow.ToString('o') }
}

function Get-VerifiedTransitionAddress($Mapping, $Snapshot) {
    try {
        if ($null -eq $Mapping -or $Mapping.version -ne 1 -or $Mapping.source -ne 'start_transition' -or $Snapshot.state -ne 'On' -or
            -not $Snapshot.transition_capable -or
            $Mapping.upstream_adapter_guid -ne $Snapshot.upstream_guid -or
            $Mapping.upstream_profile_hash -cne $Snapshot.upstream_profile_hash -or
            $Mapping.pnp_device_id -ne $Snapshot.upstream_pnp -or
            $Mapping.adapter_guid -eq $Snapshot.upstream_guid -or
            @($Snapshot.stations | Where-Object interface_id -eq $Mapping.adapter_guid).Count -ne 0) { return $null }
        $adapters = @($Snapshot.adapters | Where-Object { $_.guid -eq $Mapping.adapter_guid -and $_.index -eq $Mapping.interface_index -and
            $_.pnp -eq $Mapping.pnp_device_id -and $_.interface_type -eq 71 -and $_.status -eq 'Up' })
        if ($adapters.Count -ne 1) { return $null }
        $addresses = @($Snapshot.addresses | Where-Object { $_.guid -eq $Mapping.adapter_guid -and $_.index -eq $Mapping.interface_index -and
            $_.ip -eq $Mapping.ip_address -and $_.prefix -eq $Mapping.prefix_length })
        if ($addresses.Count -ne 1 -or @($Snapshot.addresses | Where-Object ip -eq $Mapping.ip_address).Count -ne 1 -or
            -not (Test-PrivateIPv4 ([string]$Mapping.ip_address))) { return $null }
        return $addresses[0]
    } catch { return $null }
}

function Find-TransitionAddress($Before, $After) {
    if ($Before.state -ne 'Off' -or $After.state -ne 'On') { return $null }
    if (-not $Before.transition_capable -or -not $After.transition_capable) { throw 'hotspot_upstream_not_verified' }
    if ($Before.upstream_guid -ne $After.upstream_guid -or $Before.upstream_profile_hash -cne $After.upstream_profile_hash -or
        $Before.upstream_pnp -ne $After.upstream_pnp) { throw 'hotspot_upstream_changed' }
    $candidates = @(
        foreach ($address in $After.addresses) {
            # A prefix-only change is not a newly created private address.
            if (@($Before.addresses | Where-Object { $_.guid -eq $address.guid -and $_.ip -eq $address.ip }).Count) { continue }
            $mapping = @{version=1;source='start_transition';adapter_guid=$address.guid;interface_index=$address.index;
                ip_address=$address.ip;prefix_length=$address.prefix;upstream_adapter_guid=$After.upstream_guid;
                upstream_profile_hash=$After.upstream_profile_hash;pnp_device_id=$After.upstream_pnp}
            $verified = Get-VerifiedTransitionAddress $mapping $After
            if ($null -ne $verified) { $verified }
        }
    )
    if ($candidates.Count -gt 1) { throw 'hotspot_adapter_ambiguous' }
    if ($candidates.Count -eq 1) { return $candidates[0] }
    return $null
}

function Get-DirectHotspotAddress($Snapshot) {
    if ($Snapshot.state -ne 'On') { return $null }
    $candidates = @(
        foreach ($address in $Snapshot.addresses) {
            $matches = @($Snapshot.adapters | Where-Object { $_.guid -eq $address.guid -and $_.index -eq $address.index -and
                $_.status -eq 'Up' -and $_.description -match '(?i)Wi[\s-]?Fi\s+Direct' })
            if ($matches.Count -eq 1 -and @($Snapshot.addresses | Where-Object ip -eq $address.ip).Count -eq 1) { $address }
        }
    )
    if ($candidates.Count -gt 1) { throw 'hotspot_adapter_ambiguous' }
    if ($candidates.Count -eq 1) { return $candidates[0] }
    return $null
}

function Get-NetworkInventory {
    $result = [ordered]@{ wifi_profiles = @(); wifi_interfaces = @(); addresses = @(); hotspot = @{ state = 'unknown'; ssid = $null }; errors = @() }
    $adapters = @()
    $categories = @()
    try {
        $adapters = @(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Select-Object InterfaceIndex, InterfaceGuid, Name, InterfaceDescription, Status)
    } catch { $result.errors += 'adapter_inventory_unavailable' }
    try {
        $categories = @(Get-NetConnectionProfile -ErrorAction Stop | Select-Object InterfaceIndex, NetworkCategory)
    } catch { $result.errors += 'network_category_inventory_unavailable' }
    try {
        Initialize-NativeWifi
        $wifi = [GTBmsNetwork.NativeWifi]::Inspect()
        foreach ($row in $wifi.wifi_profiles) {
            $adapter = @($adapters | Where-Object { [string]$_.InterfaceGuid -eq $row.interface_id })
            if ($adapter.Count -eq 1) { $row.interface_name = [string]$adapter[0].Name }
        }
        foreach ($row in $wifi.wifi_interfaces) {
            $adapter = @($adapters | Where-Object { [string]$_.InterfaceGuid -eq $row.interface_id })
            if ($adapter.Count -eq 1) { $row.interface_name = [string]$adapter[0].Name }
        }
        $result.wifi_profiles = @($wifi.wifi_profiles.ToArray())
        $result.wifi_interfaces = @($wifi.wifi_interfaces.ToArray())
        $result.errors += @($wifi.errors.ToArray())
    } catch { $result.errors += 'wlan_inventory_unavailable' }
    try {
        $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Select-Object IPAddress, PrefixLength, InterfaceIndex, InterfaceAlias, AddressState, SkipAsSource | Where-Object { Test-PrivateIPv4 ([string]$_.IPAddress) })
        $result.addresses = @(
            foreach ($address in $addresses) {
                $matches = @($adapters | Where-Object { $_.InterfaceIndex -eq $address.InterfaceIndex })
                $adapter = if ($matches.Count -eq 1) { $matches[0] } else { $null }
                $categoryValues = @($categories | Where-Object { $_.InterfaceIndex -eq $address.InterfaceIndex } | ForEach-Object { [string]$_.NetworkCategory } | Sort-Object -Unique)
                $category = if ($categoryValues.Count -eq 1 -and $categoryValues[0] -in @('Private', 'Public')) { $categoryValues[0] } else { 'Unknown' }
                [ordered]@{
                    ip_address = [string]$address.IPAddress
                    prefix_length = [int]$address.PrefixLength
                    interface_index = [int]$address.InterfaceIndex
                    interface_alias = [string]$address.InterfaceAlias
                    adapter_guid = $(if ($null -ne $adapter) { ([guid]$adapter.InterfaceGuid).ToString('D') } else { $null })
                    network_category = $category
                    adapter_status = $(if ($null -ne $adapter) { [string]$adapter.Status } else { 'Unknown' })
                    address_state = [string]$address.AddressState
                    skip_as_source = [bool]$address.SkipAsSource
                    candidate_kind = $(if ($null -ne $adapter -and [string]$adapter.InterfaceDescription -match '(?i)Wi[\s-]?Fi\s+Direct') { 'wifi_direct_virtual_adapter' } else { 'private_ipv4' })
                }
            }
        )
    } catch { $result.errors += 'ipv4_inventory_unavailable' }
    $context = Get-TetheringContext
    if ($null -ne $context.manager) {
        try {
            $state = [string]$context.manager.TetheringOperationalState
            $result.hotspot.state = switch ($state) { 'On' { 'on' } 'Off' { 'off' } default { 'unknown' } }
            # Read only Ssid; never access Passphrase or serialize configuration.
            $result.hotspot.ssid = [string]$context.manager.GetCurrentAccessPointConfiguration().Ssid
        } catch { $result.errors += 'hotspot_status_unavailable' }
    } else { $result.errors += $context.code }
    # A name/description is insufficient on dual-interface Qualcomm hardware.
    # The optional compatibility proof is verified afresh; any mismatch or read
    # failure leaves the ordinary address kind and cannot grant hotspot access.
    if ($result.hotspot.state -eq 'on') {
        try {
            $mapping = Read-HotspotMapping
            if ($null -ne $mapping) {
                $verified = Get-VerifiedTransitionAddress $mapping (Get-HotspotEvidenceSnapshot)
                if ($null -ne $verified) {
                    foreach ($address in $result.addresses) {
                        if ($address.adapter_guid -eq $verified.guid -and $address.interface_index -eq $verified.index -and
                            $address.ip_address -eq $verified.ip -and $address.prefix_length -eq $verified.prefix -and
                            $address.adapter_status -eq 'Up' -and $address.address_state -eq 'Preferred' -and -not $address.skip_as_source) {
                            $address.candidate_kind = 'hotspot_transition_adapter'
                        }
                    }
                }
            }
        } catch { } # No proof means no compatibility candidate; router inspection still works.
    }
    return $result
}

function Invoke-SavedWifiConnection {
    try {
        if (-not [Console]::IsInputRedirected) { return @{ ok = $false; code = 'connect_stdin_required' } }
        $raw = [Console]::In.ReadToEnd()
        if ($raw.Length -gt 4096) { return @{ ok = $false; code = 'invalid_connect_input' } }
        $inputObject = ConvertFrom-Json -InputObject $raw -ErrorAction Stop
        if ($null -eq $inputObject -or $inputObject -isnot [pscustomobject]) { return @{ ok = $false; code = 'invalid_connect_input' } }
        $names = @($inputObject.PSObject.Properties | ForEach-Object { $_.Name })
        if ($names.Count -ne 2 -or $names -notcontains 'profile' -or $names -notcontains 'interface_id' -or
            $inputObject.profile -isnot [string] -or $inputObject.interface_id -isnot [string]) {
            return @{ ok = $false; code = 'invalid_connect_input' }
        }
        $id = [guid]::Empty
        if (-not [guid]::TryParseExact($inputObject.interface_id, 'D', [ref]$id) -or $id -eq [guid]::Empty) {
            return @{ ok = $false; code = 'invalid_connect_input' }
        }
    } catch { return @{ ok = $false; code = 'invalid_connect_input' } }
    try {
        Initialize-NativeWifi
        $code = [GTBmsNetwork.NativeWifi]::ConnectSaved($inputObject.profile, $id)
        return @{ ok = $code -eq 'connect_requested'; code = $code }
    } catch { return @{ ok = $false; code = 'wifi_connect_failed' } }
}

function Invoke-TetheringCall([bool]$Start) {
    $context = Get-TetheringContext
    if ($null -eq $context.manager) { return @{ ok = $false; code = $context.code } }
    try {
        $manager = $context.manager
        $state = [string]$manager.TetheringOperationalState
        if (($Start -and $state -eq 'On') -or (-not $Start -and $state -eq 'Off')) {
            return @{ ok = $true; code = $(if ($Start) { 'hotspot_already_on' } else { 'hotspot_already_off' }) }
        }
        if ($state -notin @('On', 'Off')) { return @{ ok = $false; code = 'hotspot_state_not_ready' } }
        if ($Start) {
            $capability = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($context.profile)
            if ([int]$capability -ne 0) { return @{ ok = $false; code = 'hotspot_capability_blocked'; status = [int]$capability } }
        }
        # Resolve the await bridge BEFORE submitting an operation. The Windows
        # saved hotspot SSID, passphrase and band are never reconfigured here.
        $resultType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult, Windows, ContentType = WindowsRuntime]
        $asTask = @([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetGenericArguments().Count -eq 1 -and
            $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })
        if ($asTask.Count -ne 1) { return @{ ok = $false; code = 'hotspot_await_unavailable' } }
        $waitMethod = $asTask[0].MakeGenericMethod(@($resultType))
        $operation = if ($Start) { $manager.StartTetheringAsync() } else { $manager.StopTetheringAsync() }
        $task = $waitMethod.Invoke($null, @($operation))
        if (-not $task.Wait(35000)) {
            # A timeout does not establish rollback. Inspect before retrying.
            return @{ ok = $false; code = 'hotspot_operation_timeout'; operation_may_complete = $true }
        }
        $status = [int]$task.Result.Status
        if ($status -ne 0) { return @{ ok = $false; code = 'hotspot_operation_failed'; status = $status } }
        return @{ ok = $true; code = $(if ($Start) { 'hotspot_started' } else { 'hotspot_stopped' }) }
    } catch { return @{ ok = $false; code = 'hotspot_operation_failed' } }
}

function Invoke-HotspotOperation([bool]$Start) {
    if (-not $Start) {
        $result = Invoke-TetheringCall $false
        if ($result.ok) {
            try { Clear-HotspotMapping } catch { return @{ok=$false;code='hotspot_mapping_write_failed'} }
        }
        return $result
    }
    try {
        $snapshot = Get-HotspotEvidenceSnapshot
        if ($snapshot.state -eq 'On') {
            if ($null -ne (Get-DirectHotspotAddress $snapshot)) { return @{ok=$true;code='hotspot_already_on'} }
            $verified = Get-VerifiedTransitionAddress (Read-HotspotMapping) $snapshot
            if ($null -ne $verified) { return @{ok=$true;code='hotspot_already_on'} }
            # Establish an off baseline once; preserve Windows SSID/password/band.
            Clear-HotspotMapping
            $stopped = Invoke-TetheringCall $false
            if (-not $stopped.ok) { return $stopped }
            $snapshot = Get-HotspotEvidenceSnapshot
        }
        if ($snapshot.state -ne 'Off') { return @{ok=$false;code='hotspot_state_not_ready'} }
        Clear-HotspotMapping
        $started = Invoke-TetheringCall $true
        if (-not $started.ok) { return $started }
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(15)
        $previousKey = $null
        do {
            $after = Get-HotspotEvidenceSnapshot
            if ($null -ne (Get-DirectHotspotAddress $after)) { return @{ok=$true;code='hotspot_started';identification='wifi_direct_description'} }
            # An Ethernet upstream cannot establish the hardware compatibility
            # proof, but a standard Direct address may still be acquiring DHCP.
            $candidate = $null
            if ($snapshot.transition_capable -and $after.transition_capable) {
                $candidate = Find-TransitionAddress $snapshot $after
            } elseif ($snapshot.transition_capable -and $after.state -eq 'On') {
                throw 'hotspot_upstream_changed'
            }
            if ($null -ne $candidate) {
                $key = '{0}|{1}|{2}|{3}' -f $candidate.guid,$candidate.index,$candidate.ip,$candidate.prefix
                if ($key -ceq $previousKey) {
                    $mapping = @{version=1;source='start_transition';verified_at_utc=[DateTime]::UtcNow.ToString('o');
                        adapter_guid=$candidate.guid;interface_index=$candidate.index;ip_address=$candidate.ip;prefix_length=$candidate.prefix;
                        pnp_device_id=$after.upstream_pnp;upstream_adapter_guid=$after.upstream_guid;upstream_profile_hash=$after.upstream_profile_hash}
                    Save-HotspotMapping $mapping
                    return @{ok=$true;code='hotspot_started';identification='start_transition'}
                }
                $previousKey = $key
            } else { $previousKey = $null }
            Start-Sleep -Milliseconds 400
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        return @{ok=$false;code='hotspot_adapter_not_verified'}
    } catch {
        $code = [string]$_.Exception.Message
        if ($code -notin @('hotspot_evidence_unavailable','hotspot_evidence_changed','hotspot_upstream_not_verified',
            'hotspot_upstream_changed','hotspot_adapter_ambiguous')) { $code='hotspot_identification_failed' }
        return @{ok=$false;code=$code}
    }
}

try {
    switch ($Action) {
        'inspect' {
            $inventory = Get-NetworkInventory
            Write-Result @{ ok = $true; code = $(if ($inventory.errors.Count) { 'inspection_partial' } else { 'ok' }); inventory = $inventory }
        }
        'connect' { Write-Result (Invoke-SavedWifiConnection) }
        'start_hotspot' { Write-Result (Invoke-HotspotOperation $true) }
        'stop_hotspot' { Write-Result (Invoke-HotspotOperation $false) }
        'open_settings' {
            # Explicit interactive UI action only; inspect never opens settings.
            Start-Process -FilePath 'ms-settings:network-mobilehotspot' -ErrorAction Stop
            Write-Result @{ ok = $true; code = 'settings_opened' }
        }
    }
} catch { Write-Result @{ ok = $false; code = 'network_operation_failed' } }
