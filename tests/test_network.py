"""Network mode orchestration with fake Windows, sockets and viewer only."""
import asyncio
import copy
from dataclasses import asdict
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from gtbms import network
from gtbms.mobile import MobileSettings


WIFI = 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'
VIRTUAL = 'cccccccc-1111-2222-3333-dddddddddddd'
OTHER = 'eeeeeeee-1111-2222-3333-ffffffffffff'
KEY = 'A' * 43
PRIVATE = 'never-output-password-or-private-platform-exception'
BASE = 'http://127.0.0.1:8767'


def address(ip='192.168.50.10', *, adapter=WIFI, category='Private', kind='private_ipv4'):
    return dict(ip_address=ip, prefix_length=24, interface_index=20 if adapter == WIFI else 30,
                interface_alias='Wi-Fi', adapter_guid=adapter, network_category=category,
                adapter_status='Up', address_state='Preferred', candidate_kind=kind)


def inventory():
    return dict(wifi_profiles=[dict(name=name, interface_id=WIFI, interface_name='Wi-Fi')
                               for name in ('Home', 'Travel')],
                wifi_interfaces=[dict(interface_id=WIFI, interface_name='Wi-Fi',
                                      state='connected', current_profile='Home')],
                addresses=[address()], hotspot=dict(state='off', ssid=None), errors=[])


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    async def sleep(self, amount):
        self.value += amount
        await asyncio.sleep(0)


class Platform:
    def __init__(self, trace):
        self.data = inventory()
        self.trace = trace
        self.errors = {}
        self.connect_updates = True
        self.inspect_hook = None
        self.firewall_gate = None

    def call(self, name, *args):
        self.trace.append((name, *args))
        if name in self.errors:
            raise RuntimeError(PRIVATE)

    async def inspect(self):
        self.call('inspect')
        if self.inspect_hook:
            self.inspect_hook(self)
        return copy.deepcopy(self.data)

    async def connect(self, profile, interface):
        self.call('connect', profile, interface)
        if self.connect_updates:
            self.data['wifi_interfaces'][0]['current_profile'] = profile
            self.data['addresses'] = [address('192.168.70.10')]

    async def stop_hotspot(self):
        self.call('stop_hotspot')
        self.data['hotspot']['state'] = 'off'

    async def start_hotspot(self):
        self.call('start_hotspot')
        self.data['hotspot']['state'] = 'on'
        self.data['addresses'].append(address('192.168.137.1', adapter=VIRTUAL,
             category='Unknown', kind='wifi_direct_virtual_adapter'))

    async def ensure_firewall(self, settings, index, local_area=False):
        self.call('firewall', settings.host, index, local_area)
        if self.firewall_gate:
            await self.firewall_gate.wait()

    async def open_hotspot_settings(self):
        self.call('settings')


class Viewer:
    def __init__(self, trace):
        self.trace, self.running, self.settings = trace, False, None
        self.fail_hosts = set()
        self.on_start = None
        self.external = False

    async def start(self, settings):
        self.trace.append(('viewer_start', settings.host))
        if self.external:
            raise network.NetworkError('viewer_port_in_use', 409)
        if settings.host in self.fail_hosts:
            raise RuntimeError(PRIVATE)
        if self.on_start:
            self.on_start(settings)
        self.running, self.settings = True, settings

    async def stop(self):
        self.trace.append(('viewer_stop',))
        self.running, self.settings = False, None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    trace = []
    platform, viewer, clock = Platform(trace), Viewer(trace), Clock()
    health = SimpleNamespace(ok=True)

    async def check(port):
        trace.append(('health', port))
        return health.ok

    files = network.PrivateFiles(tmp_path)
    config = network.NetworkSettings(profiles=network.Profiles(
        mobile_router=network.RouterProfile(wlan_profile='Travel', interface_id=WIFI),
        home_router=network.RouterProfile(wlan_profile='Home', interface_id=WIFI)), active_mode='home_router')
    files.save_network(config)
    files.atomic(files.path('mobile.local.json'), json.dumps(asdict(MobileSettings('192.168.50.10', token=KEY))).encode())
    files.atomic(files.path('mobile-connect.local.html'), b'OLD PRIVATE LOCAL CONNECTION PAGE')

    def page(settings, path):
        files.atomic(path, f'<html>{settings.host}#key={settings.token}</html>'.encode())

    monkeypatch.setattr(network, 'connection_page', page)
    manager = network.NetworkManager(tmp_path, platform=platform, viewer=viewer,
        health_check=check, connect_timeout=2, poll_interval=.5, clock=clock, sleep=clock.sleep)
    return SimpleNamespace(manager=manager, platform=platform, viewer=viewer, trace=trace,
                           health=health, files=files, root=tmp_path, clock=clock)


async def switch(setup, mode='mobile_router'):
    await setup.manager.begin_switch(mode)
    await setup.manager.task


def client(setup, *, peer='127.0.0.1', url=BASE):
    app = network.create_network_app(manager=setup.manager)
    return TestClient(app, base_url=url, client=(peer, 3210))


def test_private_files_restrict_names_atomic_restore_and_backups(setup):
    files = setup.files
    before = files.snapshot()
    files.backup(before)
    files.atomic(files.path('network.local.json'), b'changed')
    files.restore(before)
    assert files.snapshot() == before
    backup = list((setup.root / 'logs/network-backups').iterdir())
    assert len(backup) == 1
    assert (backup[0] / 'mobile.local.json').read_bytes() == before['mobile.local.json']
    assert set(json.loads((backup[0] / 'manifest.json').read_text())['present']) == set(files.NAMES)
    assert not list(setup.root.glob('.*.tmp'))
    with pytest.raises(network.NetworkError):
        files.path('../config.local.json')


def test_new_settings_have_three_modes_without_invented_profile(tmp_path):
    settings = network.PrivateFiles(tmp_path).load_network()
    assert settings.active_mode == 'unknown'
    assert settings.profiles.model_dump() == dict(mobile_router=None, home_router=None, local_area={})


@pytest.mark.parametrize('name', ['network.local.json', 'mobile.local.json'])
def test_invalid_settings_use_fixed_error_without_content(setup, name):
    setup.files.atomic(setup.files.path(name), PRIVATE.encode())
    method = setup.files.load_network if name.startswith('network') else setup.files.load_mobile
    with pytest.raises(network.NetworkError) as error:
        method()
    assert PRIVATE not in str(error.value)


async def test_profile_is_existing_name_interface_pair_and_normalized(setup):
    request = network.SaveProfile(mode='mobile_router', wlan_profile='Home', interface_id='{'+WIFI.upper()+'}')
    await setup.manager.save_profile(request)
    assert setup.files.load_network().profiles.mobile_router.interface_id == WIFI
    assert setup.files.load_network().profiles.mobile_router.wlan_profile == 'Home'
    assert setup.trace == [('inspect',)]
    assert len(list((setup.root / 'logs/network-backups').iterdir())) == 1


@pytest.mark.parametrize(('name', 'interface'), [('Unknown', WIFI), ('Home', OTHER), ('cmd /c anything', WIFI)])
async def test_profile_not_in_inventory_cannot_be_saved(setup, name, interface):
    before = setup.files.snapshot()
    with pytest.raises(network.NetworkError, match='profile_not_available'):
        await setup.manager.save_profile(network.SaveProfile(mode='home_router', wlan_profile=name, interface_id=interface))
    assert setup.files.snapshot() == before


@pytest.mark.parametrize('payload', [dict(wlan_profile='Home', interface_id='not-guid'),
    dict(wlan_profile='Home\ncmd', interface_id=WIFI), dict(wlan_profile='x'*256, interface_id=WIFI),
    dict(wlan_profile='Home', interface_id=WIFI, command='anything')])
def test_profile_input_does_not_accept_commands_or_invalid_shape(payload):
    with pytest.raises(ValueError):
        network.RouterProfile(**payload)


def test_inventory_strips_credentials_raw_errors_and_unknown_fields():
    source = inventory()
    source['password'] = PRIVATE
    source['wifi_profiles'][0]['key_material'] = PRIVATE
    source['addresses'][0]['serial'] = PRIVATE
    source['errors'] = [PRIVATE]
    cleaned = network.clean_inventory(source)
    assert PRIVATE not in json.dumps(cleaned)
    assert cleaned['errors'] == ['inventory_incomplete']


@pytest.mark.parametrize('source', [None, [], dict(addresses=[dict(ip_address='invalid')]),
    dict(wifi_profiles=[dict(name='Home', interface_id='invalid')])])
def test_invalid_inventory_is_fixed_error(source):
    with pytest.raises(network.NetworkError, match='inventory_invalid'):
        network.clean_inventory(source)


async def test_router_switch_commits_only_after_firewall_and_viewer_start_reuses_token(setup):
    await setup.manager.start()
    before = setup.files.snapshot()
    def before_commit(settings):
        assert setup.files.snapshot() == before
        assert setup.manager.settings.active_mode == 'home_router'
        assert settings.token == KEY
        assert ('firewall', '192.168.70.10', 20, False) in setup.trace
    setup.viewer.on_start = before_commit
    await switch(setup)
    state = setup.manager.state()
    assert state['active_mode'] == 'mobile_router'
    assert state['operation']['state'] == 'succeeded'
    assert state['viewer'] == dict(state='running', url='http://192.168.70.10:8766/')
    assert setup.files.load_mobile().token == KEY
    assert setup.files.load_network().active_mode == 'mobile_router'
    assert KEY in setup.files.path('mobile-connect.local.html').read_text()
    assert KEY not in json.dumps(state)
    operations = [entry[0] for entry in setup.trace]
    assert 'stop_hotspot' not in operations  # Already confirmed off.
    assert operations.index('connect') < operations.index('firewall')
    assert all(entry == ('health', 8765) for entry in setup.trace if entry[0] == 'health')


async def test_local_mode_only_uses_confirmed_wifi_direct_hotspot_with_scoped_firewall(setup):
    await setup.manager.start()
    await switch(setup, 'local_area')
    assert setup.manager.state()['active_mode'] == 'local_area'
    assert setup.files.load_mobile().host == '192.168.137.1'
    assert ('firewall', '192.168.137.1', 30, True) in setup.trace
    assert not [entry for entry in setup.trace if entry[0] == 'connect']


async def test_observed_hotspot_transition_candidate_survives_cleaning_and_local_switch(setup, monkeypatch):
    await setup.manager.start()
    original = setup.platform.start_hotspot
    async def observed_transition():
        await original()
        setup.platform.data['addresses'][-1]['candidate_kind'] = 'hotspot_transition_adapter'
    monkeypatch.setattr(setup.platform, 'start_hotspot', observed_transition)
    await switch(setup, 'local_area')
    assert setup.manager.operation['state'] == 'succeeded'
    assert setup.manager.state()['active_mode'] == 'local_area'
    assert setup.files.load_mobile().host == '192.168.137.1'
    assert setup.manager.inventory['addresses'][-1]['candidate_kind'] == 'hotspot_transition_adapter'
    assert ('firewall', '192.168.137.1', 30, True) in setup.trace


@pytest.mark.parametrize('condition', ['off', 'unknown', 'down', 'tentative', 'skip',
    'public_ip', 'duplicate', 'two_candidates', 'invalid_kind', 'evidence_no_longer_matches'])
async def test_transition_kind_still_requires_live_unique_valid_local_candidate(setup, condition):
    await setup.platform.start_hotspot()
    row = setup.platform.data['addresses'][-1]
    row['candidate_kind'] = 'hotspot_transition_adapter'
    if condition in ('off', 'unknown'):
        setup.platform.data['hotspot']['state'] = condition
    elif condition == 'down':
        row['adapter_status'] = 'Down'
    elif condition == 'tentative':
        row['address_state'] = 'Tentative'
    elif condition == 'skip':
        row['skip_as_source'] = True
    elif condition == 'public_ip':
        row['ip_address'] = '8.8.8.8'
    elif condition == 'duplicate':
        setup.platform.data['addresses'].append(address(row['ip_address'], adapter=OTHER))
    elif condition == 'two_candidates':
        setup.platform.data['addresses'].append(address('192.168.138.1', adapter=OTHER,
            kind='wifi_direct_virtual_adapter'))
    elif condition == 'invalid_kind':
        row['candidate_kind'] = 'hotspot_transition_adapter_unverified'
    else:
        # The platform removes its special kind once the recorded observation
        # no longer matches. A remaining private address is not enough.
        row['candidate_kind'] = 'private_ipv4'
    await setup.manager.inspect()
    assert setup.manager.select_address('local_area') is None


@pytest.mark.parametrize('mode', ['local_area', 'unknown'])
async def test_transition_candidate_old_identity_is_checked_for_restore(setup, mode):
    await setup.platform.start_hotspot()
    setup.platform.data['addresses'][-1]['candidate_kind'] = 'hotspot_transition_adapter'
    await setup.manager.inspect()
    before = copy.deepcopy(setup.manager.inventory)
    old = setup.manager.settings.model_copy(deep=True)
    old.active_mode = mode
    mobile = MobileSettings('192.168.137.1', token=KEY)
    current, local = setup.manager.previous_address(old, mobile, before)
    assert local is True
    assert current['adapter_guid'] == VIRTUAL
    setup.manager.inventory['addresses'][-1]['adapter_guid'] = OTHER
    with pytest.raises(network.NetworkError, match='previous_network_unavailable'):
        setup.manager.previous_address(old, mobile, before)


async def test_transition_candidate_supports_known_mode_startup_and_loses_active_claim_if_proof_removed(setup):
    await setup.platform.start_hotspot()
    setup.platform.data['addresses'][-1]['candidate_kind'] = 'hotspot_transition_adapter'
    setup.manager.settings.active_mode = 'local_area'
    setup.files.atomic(setup.files.path('mobile.local.json'), json.dumps(asdict(MobileSettings(
        '192.168.137.1', token=KEY))).encode())
    await setup.manager.start()
    assert setup.viewer.running
    assert setup.manager.state()['active_mode'] == 'local_area'
    setup.platform.data['addresses'][-1]['candidate_kind'] = 'private_ipv4'
    await setup.manager.refresh()
    assert setup.manager.state()['active_mode'] == 'unknown'
    assert setup.viewer.running  # Status refresh never performs an automatic network action.


async def test_new_manager_can_create_private_mobile_key_only_after_success(tmp_path, monkeypatch):
    trace = []
    async def healthy(port):
        return True
    manager = network.NetworkManager(tmp_path, platform=Platform(trace), viewer=Viewer(trace), health_check=healthy)
    monkeypatch.setattr(network, 'connection_page', lambda settings, path: Path(path).write_text(settings.token))
    await manager.begin_switch('local_area')
    await manager.task
    assert manager.operation['state'] == 'succeeded'
    assert len(manager.files.load_mobile().token) == 43
    assert manager.files.load_mobile().token not in json.dumps(manager.state())


async def test_old_ip_wrong_profile_never_counts_as_connected(setup):
    await setup.manager.start()
    setup.platform.connect_updates = False
    before = setup.files.snapshot()
    await switch(setup)
    assert setup.manager.last_error == 'connection_not_ready'
    assert setup.clock.value == 2
    assert setup.manager.operation['state'] == 'recovery_required'
    assert setup.files.snapshot() == before
    assert setup.manager.settings.active_mode == 'home_router'
    assert setup.viewer.running  # Old IP remains assigned, so owned viewer can recover.
    assert not any(entry[:2] == ('viewer_start', '192.168.70.10') for entry in setup.trace)


async def test_connect_waits_for_actual_profile_and_new_preferred_address(setup):
    await setup.manager.start()
    setup.platform.connect_updates = False
    def progress(platform):
        if setup.clock.value >= .5:
            platform.data['wifi_interfaces'][0]['current_profile'] = 'Travel'
            platform.data['addresses'][0]['address_state'] = 'Tentative'
        if setup.clock.value >= 1:
            platform.data['addresses'] = [address('192.168.70.10')]
    setup.platform.inspect_hook = progress
    await switch(setup)
    assert setup.manager.operation['state'] == 'succeeded'
    assert setup.clock.value == 1.5  # Two successive matching observations, not a DHCP guarantee.


async def test_slow_inspection_is_bounded_by_connection_deadline(setup, monkeypatch):
    import time
    cancelled = asyncio.Event()
    async def never_finishes():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(setup.platform, 'inspect', never_finishes)
    setup.manager.clock, setup.manager.connect_timeout = time.monotonic, .01
    with pytest.raises(network.NetworkError, match='connection_not_ready'):
        await setup.manager.wait_address('home_router', setup.manager.profile('home_router'))
    assert cancelled.is_set()


async def test_backup_failure_leaves_running_viewer_and_config_untouched(setup, monkeypatch):
    await setup.manager.start()
    setup.trace.clear()
    before = setup.files.snapshot()
    monkeypatch.setattr(setup.manager.files, 'backup', lambda *_: (_ for _ in ()).throw(OSError(PRIVATE)))
    await switch(setup)
    assert setup.trace == [('inspect',), ('health', 8765)]
    assert setup.viewer.running
    assert setup.files.snapshot() == before
    assert setup.manager.last_error == 'configuration_backup_failed'


@pytest.mark.parametrize('change', ['public', 'unknown', 'wrong_interface', 'disconnected',
    'wrong_profile', 'duplicate_address', 'two_addresses', 'down', 'tentative', 'public_ip',
    'cross_private_boundary', 'inventory_error', 'skip_as_source'])
async def test_router_address_selection_fails_closed(setup, change):
    await setup.manager.inspect()
    data = setup.manager.inventory
    if change in ('public', 'unknown'):
        data['addresses'][0]['network_category'] = change.title()
    elif change == 'wrong_interface':
        data['addresses'][0]['adapter_guid'] = OTHER
    elif change == 'disconnected':
        data['wifi_interfaces'][0]['state'] = 'disconnected'
    elif change == 'wrong_profile':
        data['wifi_interfaces'][0]['current_profile'] = 'Travel'
    elif change == 'duplicate_address':
        data['addresses'].append(address(adapter=OTHER))
    elif change == 'two_addresses':
        data['addresses'].append(address('192.168.50.11'))
    elif change == 'down':
        data['addresses'][0]['adapter_status'] = 'Down'
    elif change == 'tentative':
        data['addresses'][0]['address_state'] = 'Tentative'
    elif change == 'public_ip':
        data['addresses'][0]['ip_address'] = '8.8.8.8'
    elif change == 'cross_private_boundary':
        data['addresses'][0]['prefix_length'] = 8
    elif change == 'skip_as_source':
        data['addresses'][0]['skip_as_source'] = True
    else:
        data['errors'] = ['inventory_incomplete']
    assert setup.manager.select_address('home_router', setup.manager.profile('home_router')) is None


@pytest.mark.parametrize('change', ['off', 'unknown', 'physical_only', 'two_virtual', 'down'])
async def test_local_selection_requires_on_unique_preferred_virtual_adapter(setup, change):
    await setup.platform.start_hotspot()
    await setup.manager.inspect()
    data = setup.manager.inventory
    if change in ('off', 'unknown'):
        data['hotspot']['state'] = change
    elif change == 'physical_only':
        data['addresses'].pop()
    elif change == 'two_virtual':
        data['addresses'].append(address('192.168.138.1', adapter=OTHER, kind='wifi_direct_virtual_adapter'))
    else:
        data['addresses'][1]['adapter_status'] = 'Down'
    assert setup.manager.select_address('local_area') is None


async def test_main_unavailable_does_not_stop_viewer_or_change_network_or_files(setup):
    await setup.manager.start()
    before = setup.files.snapshot()
    setup.trace.clear()
    setup.health.ok = False
    await switch(setup)
    assert setup.trace == [('inspect',), ('health', 8765)]
    assert setup.viewer.running
    assert setup.files.snapshot() == before
    assert setup.manager.last_error == 'main_unavailable'
    assert setup.manager.operation['state'] == 'failed'


@pytest.mark.parametrize('stage', ['start_hotspot', 'firewall', 'viewer', 'commit'])
async def test_failure_restores_private_files_and_old_owned_viewer_if_ip_still_present(setup, monkeypatch, stage):
    await setup.manager.start()
    before = setup.files.snapshot()
    if stage in ('start_hotspot', 'firewall'):
        original = getattr(setup.platform, stage if stage == 'start_hotspot' else 'ensure_firewall')
        failed = False
        async def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError(PRIVATE)
            return await original(*args, **kwargs)
        monkeypatch.setattr(setup.platform, stage if stage == 'start_hotspot' else 'ensure_firewall', fail_once)
    elif stage == 'viewer':
        setup.viewer.fail_hosts.add('192.168.137.1')
    else:
        monkeypatch.setattr(setup.manager.files, 'save_network', lambda *_: (_ for _ in ()).throw(OSError(PRIVATE)))
    await switch(setup, 'local_area')
    assert setup.files.snapshot() == before
    assert setup.manager.settings.active_mode == 'home_router'
    assert setup.manager.operation['state'] == 'recovery_required'
    assert setup.viewer.running and setup.viewer.settings.host == '192.168.50.10'
    assert ('firewall', '192.168.50.10', 20, False) in setup.trace
    assert PRIVATE not in json.dumps(setup.manager.state())
    assert not setup.manager.lock.locked()


async def test_missing_old_ip_does_not_show_old_url_as_recovered(setup):
    await setup.manager.start()
    before = setup.files.snapshot()
    setup.platform.errors['firewall'] = True
    await switch(setup)
    assert setup.files.snapshot() == before
    assert setup.manager.state()['viewer'] == dict(state='stopped', url=None)
    assert setup.manager.operation['state'] == 'recovery_required'


async def test_rollback_attempts_file_restore_even_when_viewer_stop_fails(setup, monkeypatch):
    await setup.manager.start()
    before = setup.files.snapshot()
    async def bad_stop():
        raise OSError(PRIVATE)
    def fail_commit(*args):
        monkeypatch.setattr(setup.viewer, 'stop', bad_stop)
        raise OSError(PRIVATE)
    monkeypatch.setattr(setup.manager.files, 'save_network', fail_commit)
    await switch(setup, 'local_area')
    assert setup.files.snapshot() == before
    assert setup.manager.operation['state'] == 'recovery_required'


@pytest.mark.parametrize('mode', ['home_router', 'unknown'])
async def test_rollback_never_reuses_same_ip_on_different_wifi_profile(setup, monkeypatch, mode):
    setup.manager.settings.active_mode = mode
    setup.files.save_network(setup.manager.settings)
    await setup.manager.start()
    before = setup.files.snapshot()
    async def same_ip_new_profile(profile, interface):
        setup.trace.append(('connect', profile, interface))
        setup.platform.data['wifi_interfaces'][0]['current_profile'] = profile
    monkeypatch.setattr(setup.platform, 'connect', same_ip_new_profile)
    setup.platform.errors['firewall'] = True
    await switch(setup)
    assert setup.files.snapshot() == before
    assert setup.manager.operation['state'] == 'recovery_required'
    assert setup.manager.state()['active_mode'] == 'unknown'
    assert setup.manager.state()['viewer'] == dict(state='stopped', url=None)
    assert [entry for entry in setup.trace if entry[0] == 'viewer_start'] == [('viewer_start', '192.168.50.10')]


@pytest.mark.parametrize('mode', ['home_router', 'unknown'])
async def test_rollback_never_reuses_old_profile_and_ip_on_different_adapter(setup, monkeypatch, mode):
    setup.manager.settings.active_mode = mode
    setup.files.save_network(setup.manager.settings)
    await setup.manager.start()
    async def fail_and_replace_adapter(*args):
        setup.platform.data['addresses'][0]['adapter_guid'] = OTHER
        setup.platform.data['wifi_interfaces'][0]['interface_id'] = OTHER
        raise OSError(PRIVATE)
    monkeypatch.setattr(setup.platform, 'ensure_firewall', fail_and_replace_adapter)
    await switch(setup, 'local_area')
    assert not setup.viewer.running
    assert setup.manager.state()['active_mode'] == 'unknown'


async def test_unknown_legacy_mode_can_restore_proven_original_identity_without_inventing_mode(setup):
    setup.manager.settings.active_mode = 'unknown'
    setup.files.save_network(setup.manager.settings)
    await setup.manager.start()
    setup.viewer.fail_hosts.add('192.168.137.1')
    await switch(setup, 'local_area')
    assert setup.viewer.running
    assert setup.viewer.settings.host == '192.168.50.10'
    assert setup.manager.state()['active_mode'] == 'unknown'
    assert setup.files.load_network().active_mode == 'unknown'


async def test_unknown_legacy_mode_does_not_restore_when_previous_identity_was_unavailable(setup):
    setup.manager.settings.active_mode = 'unknown'
    setup.files.save_network(setup.manager.settings)
    setup.platform.data['wifi_interfaces'] = []
    await setup.manager.start()
    setup.viewer.fail_hosts.add('192.168.137.1')
    await switch(setup, 'local_area')
    assert not setup.viewer.running
    assert setup.manager.operation['state'] == 'recovery_required'


async def test_local_rollback_requires_hotspot_on_and_original_virtual_adapter(setup, monkeypatch):
    await setup.manager.start()
    await switch(setup, 'local_area')
    assert setup.viewer.running
    setup.trace.clear()
    # A router change turns the hotspot off; retaining its old IP is not proof it works.
    async def keep_old_addresses(profile, interface):
        setup.platform.data['wifi_interfaces'][0]['current_profile'] = profile
    monkeypatch.setattr(setup.platform, 'connect', keep_old_addresses)
    setup.platform.errors['firewall'] = True
    await switch(setup)
    assert setup.manager.operation['state'] == 'recovery_required'
    assert not setup.viewer.running
    assert not [entry for entry in setup.trace if entry[0] == 'viewer_start']


async def test_matching_router_and_hotspot_off_do_not_reconnect_wifi(setup):
    await setup.manager.start()
    await switch(setup, 'home_router')
    assert setup.manager.operation['state'] == 'succeeded'
    assert not [entry for entry in setup.trace if entry[0] in ('connect', 'stop_hotspot', 'start_hotspot')]
    assert setup.clock.value == .5


async def test_matching_address_must_repeat_after_transient_change(setup):
    await setup.manager.start()
    def change(platform):
        if setup.clock.value >= .5:
            platform.data['addresses'] = [address('192.168.70.11')]
    setup.platform.inspect_hook = change
    await switch(setup)
    assert setup.manager.operation['state'] == 'succeeded'
    assert setup.files.load_mobile().host == '192.168.70.11'
    assert setup.clock.value == 1


@pytest.mark.parametrize('mismatch', ['profile', 'guid', 'ip', 'local_off'])
async def test_startup_does_not_claim_old_mode_or_start_viewer_on_different_network(setup, mismatch):
    if mismatch == 'profile':
        setup.platform.data['wifi_interfaces'][0]['current_profile'] = 'Travel'
    elif mismatch == 'guid':
        setup.platform.data['addresses'][0]['adapter_guid'] = OTHER
    elif mismatch == 'ip':
        setup.platform.data['addresses'][0]['ip_address'] = '192.168.50.11'
    else:
        setup.manager.settings.active_mode = 'local_area'
    await setup.manager.start()
    assert setup.manager.last_error == 'previous_network_unavailable'
    assert setup.manager.state()['active_mode'] == 'unknown'
    assert not setup.viewer.running
    assert not [entry for entry in setup.trace if entry[0] == 'viewer_start']


async def test_busy_switch_profile_refresh_and_settings_are_rejected_without_overlap(setup):
    await setup.manager.start()
    setup.platform.firewall_gate = asyncio.Event()
    await setup.manager.begin_switch('local_area')
    while setup.manager.operation['phase'] != 'firewall':
        await asyncio.sleep(0)
    assert setup.manager.settings.active_mode == 'home_router'
    operations = [setup.manager.begin_switch('home_router'), setup.manager.refresh(),
                  setup.manager.hotspot_settings(), setup.manager.save_profile(network.SaveProfile(
                  mode='home_router', wlan_profile='Home', interface_id=WIFI))]
    for operation in operations:
        with pytest.raises(network.NetworkError, match='operation_busy'):
            await operation
    setup.platform.firewall_gate.set()
    await setup.manager.task
    assert setup.manager.settings.active_mode == 'local_area'


async def test_external_listener_is_not_stopped_or_replaced(setup):
    setup.viewer.external = True
    await setup.manager.start()
    assert setup.manager.state()['viewer'] == dict(state='external', url=None)
    setup.trace.clear()
    with pytest.raises(network.NetworkError, match='external_viewer_running'):
        await setup.manager.begin_switch('home_router')
    assert setup.trace == []
    assert setup.manager.settings.active_mode == 'home_router'


async def test_shutdown_cancels_only_owned_operation_and_viewer(setup):
    await setup.manager.start()
    setup.platform.firewall_gate = asyncio.Event()
    await setup.manager.begin_switch('local_area')
    while setup.manager.operation['phase'] != 'firewall':
        await asyncio.sleep(0)
    setup.platform.firewall_gate = None  # Rollback must not block behind the fake gate.
    await setup.manager.close()
    assert setup.manager.task.done()
    assert not setup.viewer.running
    assert not setup.manager.lock.locked()
    assert setup.manager.last_error == 'operation_interrupted'


@pytest.mark.parametrize('path', ['/', '/api/session', '/api/state', '/api/health', '/connection'])
def test_all_pages_and_apis_reject_lan_peers(setup, path):
    with client(setup, peer='192.168.50.2') as web:
        response = web.get(path)
        assert response.status_code == 403
        assert response.json() == {'detail': 'local_access_only'}
        assert KEY not in response.text


@pytest.mark.parametrize('headers', [{'host': 'evil.example:8767'}, {'host': '127.0.0.1'},
    {'host': '127.0.0.1:8766'}, {'host': '127.0.0.1:8767.evil.example'},
    {'origin': 'http://evil.example'}, {'origin': 'http://localhost:8767'}, {'origin': 'null'},
    {'origin': 'http://127.0.0.1:8765'}, {'sec-fetch-site': 'cross-site'},
    {'sec-fetch-site': 'same-site'}, [('host', '127.0.0.1:8767'), ('host', '127.0.0.1:8767')],
    [('origin', BASE), ('origin', BASE)]])
def test_host_origin_and_fetch_site_strictly_guard_local_secrets(setup, headers):
    with client(setup) as web:
        assert web.get('/api/session', headers=headers).status_code == 403


@pytest.mark.parametrize('url', [BASE, 'http://localhost:8767'])
def test_valid_same_origin_health_static_state_and_connection(setup, url):
    with client(setup, url=url) as web:
        for path in ('/', '/static/network.js', '/static/network.css'):
            response = web.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['x-frame-options'] == 'DENY'
            assert "frame-ancestors 'none'" in response.headers['content-security-policy']
        assert web.get('/api/health').json() == dict(ok=True, application='gtbms-network', version=1)
        state = web.get('/api/state', headers={'origin': url}).json()
        assert KEY not in json.dumps(state)
        assert web.get('/connection').text == 'OLD PRIVATE LOCAL CONNECTION PAGE'
        web.headers['X-GTBMS-Token'] = web.get('/api/session').json()['token']
        assert web.post('/api/refresh', json={}, headers={'origin': url}).status_code == 200


@pytest.mark.parametrize('path', ['/api/monitor/start', '/api/settings', '/api/history', '/api/export',
                                '/api/gps', '/docs', '/redoc', '/openapi.json', '/static/mobile.js', '/static/../mobile.local.json'])
def test_no_main_control_export_documentation_or_arbitrary_file_routes(setup, path):
    with client(setup) as web:
        assert web.get(path).status_code == 404


@pytest.mark.parametrize('method', ['put', 'delete', 'patch', 'options', 'head'])
def test_only_get_post_methods_are_accepted(setup, method):
    with client(setup) as web:
        assert getattr(web, method)('/api/state').status_code == 405


def test_csrf_requires_exact_header_not_query_cookie_or_duplicate(setup):
    with client(setup) as web:
        token = web.get('/api/session').json()['token']
        assert web.post('/api/refresh', json={}).status_code == 403
        assert web.post('/api/refresh?token='+token, json={}).status_code == 403
        assert web.post('/api/refresh', json={}, headers={'cookie': 'token='+token}).status_code == 403
        assert web.post('/api/refresh', json={}, headers=[('x-gtbms-token', token), ('x-gtbms-token', token)]).status_code == 403
        assert web.post('/api/refresh', json={}, headers={'X-GTBMS-Token': token}).status_code == 200


@pytest.mark.parametrize(('path', 'body'), [('/api/switch', {'mode': 'unknown'}),
    ('/api/profile', {'mode': 'local_area', 'wlan_profile': 'Home', 'interface_id': WIFI}),
    ('/api/refresh', {'password': PRIVATE}), ('/api/hotspot-settings', {'command': PRIVATE}),
    ('/api/profile', {'mode': 'home_router', 'wlan_profile': 'Home', 'interface_id': PRIVATE})])
def test_invalid_body_is_fixed_error_without_echoing_secret(setup, path, body):
    with client(setup) as web:
        web.headers['X-GTBMS-Token'] = web.get('/api/session').json()['token']
        response = web.post(path, json=body)
        assert response.status_code == 400
        assert response.json() == {'detail': 'invalid_request'}
        assert PRIVATE not in response.text


def test_large_request_body_is_rejected(setup):
    with client(setup) as web:
        web.headers['X-GTBMS-Token'] = web.get('/api/session').json()['token']
        assert web.post('/api/refresh', content=b'x'*(network.MAX_BODY+1)).status_code == 413


async def test_chunked_body_guard_enforces_limit_and_no_route_side_effects(setup):
    app = network.create_network_app(manager=setup.manager)
    headers = [(b'host', b'127.0.0.1:8767'), (b'x-gtbms-token', app.state.csrf.encode())]
    chunks = [dict(type='http.request', body=b'x'*network.MAX_BODY, more_body=True),
              dict(type='http.request', body=b'x', more_body=False)]
    output = []
    async def receive():
        return chunks.pop(0)
    async def send(message):
        output.append(message)
    await app(dict(type='http', asgi={'version': '3.0'}, method='POST', path='/api/refresh',
                   raw_path=b'/api/refresh', query_string=b'', headers=headers,
                   client=('127.0.0.1', 3210), server=('127.0.0.1', 8767), scheme='http', http_version='1.1'), receive, send)
    assert output[0]['status'] == 413
    assert setup.trace == []


@pytest.mark.parametrize('value', [b'9'*5000, b'\xb2', b'-1', b'+2', b'2.0'])
async def test_malformed_length_header_cannot_raise_or_reach_route(setup, value):
    app = network.create_network_app(manager=setup.manager)
    output = []
    async def receive():
        return dict(type='http.request', body=b'{}', more_body=False)
    async def send(message):
        output.append(message)
    await app(dict(type='http', asgi={'version': '3.0'}, method='POST', path='/api/refresh',
        raw_path=b'/api/refresh', query_string=b'', headers=[(b'host', b'127.0.0.1:8767'),
        (b'x-gtbms-token', app.state.csrf.encode()), (b'content-length', value)],
        client=('127.0.0.1', 3210), server=('127.0.0.1', 8767), scheme='http', http_version='1.1'), receive, send)
    assert output[0]['status'] == 413
    assert setup.trace == []


def test_http_switch_returns_202_and_does_not_expose_private_settings(setup):
    with client(setup) as web:
        web.headers['X-GTBMS-Token'] = web.get('/api/session').json()['token']
        response = web.post('/api/switch', json={'mode': 'mobile_router'})
        assert response.status_code == 202
        assert 'operation' in response.json()
        assert KEY not in response.text
        # The request starts a task; committed state is obtained separately.
        assert web.get('/api/state').status_code == 200


async def test_main_health_has_fixed_loopback_route_no_proxy_no_redirect_and_bounded_body(monkeypatch):
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, amount):
            calls.append(('read', amount))
            return b'{"ok":true}'
    class Opener:
        def open(self, url, timeout):
            calls.append(('open', url, timeout))
            return Response()
    def build(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], network.NoRedirect)
        return Opener()
    monkeypatch.setattr(network.urllib.request, 'build_opener', build)
    assert await network.main_health(8765)
    assert calls == [('open', 'http://127.0.0.1:8765/api/health', 3), ('read', 4097)]


def test_cli_serve_binds_only_fixed_loopback_manager_and_honors_root(setup, monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setattr(sys, 'argv', ['gtbms.network', 'serve', '--root', str(setup.root)])
    monkeypatch.setattr(network, 'create_network_app', lambda root: calls.append(root) or 'fake-app')
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: calls.append((app, kwargs)))
    network.main()
    assert calls[0] == setup.root
    assert calls[1][0] == 'fake-app'
    assert calls[1][1]['host'] == '127.0.0.1'
    assert calls[1][1]['port'] == 8767
    assert calls[1][1]['proxy_headers'] is False
    assert calls[1][1]['access_log'] is False


def test_default_platform_receives_explicit_root(tmp_path, monkeypatch):
    import gtbms.network_platform
    captured = []
    monkeypatch.setattr(gtbms.network_platform, 'NetworkPlatform', lambda root: captured.append(root) or object())
    network.NetworkManager(tmp_path)
    assert captured == [tmp_path]


@pytest.mark.parametrize('code', sorted(network.OPTIONAL_HOTSPOT_ERRORS))
async def test_offline_router_lan_works_without_optional_hotspot_api(setup, code):
    setup.platform.data['errors'] = [code]
    setup.platform.data['hotspot']['state'] = 'unknown'
    await setup.manager.save_profile(network.SaveProfile(mode='mobile_router', wlan_profile='Travel', interface_id=WIFI))
    await setup.manager.start()
    await switch(setup)
    assert setup.manager.operation['state'] == 'succeeded'
    assert setup.manager.operation['warnings'] == ['hotspot_state_unconfirmed']
    assert '停止状態は未確認' in setup.manager.operation['message']
    assert setup.manager.inventory['errors'] == [code]
    assert not [entry for entry in setup.trace if entry[0] == 'stop_hotspot']
    assert setup.files.load_mobile().host == '192.168.70.10'


@pytest.mark.parametrize('code', ['adapter_inventory_unavailable', 'network_category_inventory_unavailable',
    'wlan_inventory_unavailable', 'ipv4_inventory_unavailable', 'wlan_profiles_5', PRIVATE])
async def test_essential_or_unknown_inventory_error_still_blocks_changes(setup, code):
    setup.platform.data['errors'] = [code]
    before = setup.files.snapshot()
    with pytest.raises(network.NetworkError, match='profile_not_available'):
        await setup.manager.save_profile(network.SaveProfile(mode='home_router', wlan_profile='Home', interface_id=WIFI))
    await switch(setup)
    assert setup.manager.operation['state'] == 'failed'
    assert setup.files.snapshot() == before
    assert not any(entry[0] in ('connect', 'firewall', 'start_hotspot', 'viewer_stop') for entry in setup.trace)


async def test_router_switch_stops_hotspot_only_when_confirmed_on(setup):
    setup.platform.data['hotspot']['state'] = 'on'
    await setup.manager.start()
    await switch(setup)
    operations = [entry[0] for entry in setup.trace]
    assert operations.index('stop_hotspot') < operations.index('connect')
    assert setup.manager.operation['state'] == 'succeeded'


@pytest.mark.parametrize('code', ['hotspot_operation_timeout', 'windows_operation_timeout', 'hotspot_capability_blocked'])
async def test_platform_codes_remain_actionable_and_late_operations_require_recheck(setup, monkeypatch, code):
    from gtbms.network_platform import NetworkPlatformError
    await setup.manager.start()
    async def fail():
        raise NetworkPlatformError(code)
    monkeypatch.setattr(setup.platform, 'start_hotspot', fail)
    await switch(setup, 'local_area')
    assert setup.manager.last_error == code
    assert setup.manager.operation['state'] == 'recovery_required'


async def test_inventory_time_is_actual_inspection_time_not_every_state_get(setup):
    assert setup.manager.state()['inventory_checked_at'] is None
    await setup.manager.inspect()
    observed = setup.manager.state()['inventory_checked_at']
    assert observed.endswith('+00:00')
    for _ in range(3):
        assert setup.manager.state()['inventory_checked_at'] == observed
    setup.platform.errors['inspect'] = True
    with pytest.raises(network.NetworkError):
        await setup.manager.inspect()
    assert setup.manager.state()['inventory_checked_at'] == observed


@pytest.mark.parametrize('changed', ['profile', 'guid', 'ip', 'category', 'skip_as_source', 'disconnected'])
async def test_refresh_revalidates_active_mode_without_changing_network_or_stopping_viewer(setup, changed):
    await setup.manager.start()
    assert setup.manager.state()['active_mode'] == 'home_router'
    before = setup.files.snapshot()
    setup.trace.clear()
    row = setup.platform.data['addresses'][0]
    interface = setup.platform.data['wifi_interfaces'][0]
    if changed == 'profile':
        interface['current_profile'] = 'Travel'
    elif changed == 'guid':
        row['adapter_guid'] = OTHER
    elif changed == 'ip':
        row['ip_address'] = '192.168.50.11'
    elif changed == 'category':
        row['network_category'] = 'Public'
    elif changed == 'skip_as_source':
        row['skip_as_source'] = True
    else:
        interface['state'] = 'disconnected'
    await setup.manager.refresh()
    assert setup.manager.active_verified is True  # Historical startup alone is insufficient.
    assert setup.manager.state()['active_mode'] == 'unknown'
    assert setup.manager.settings.active_mode == 'home_router'
    assert setup.trace == [('inspect',)]
    assert setup.files.snapshot() == before
    assert setup.viewer.running


async def test_failed_reinspection_does_not_claim_cached_active_mode(setup):
    await setup.manager.start()
    observed = setup.manager.inventory_checked_at
    setup.platform.errors['inspect'] = True
    with pytest.raises(network.NetworkError):
        await setup.manager.refresh()
    assert setup.manager.inventory_checked_at == observed
    assert setup.manager.state()['active_mode'] == 'unknown'
    assert setup.viewer.running
    setup.platform.errors.clear()
    await setup.manager.refresh()
    assert setup.manager.state()['active_mode'] == 'home_router'


async def test_hotspot_off_in_latest_inventory_removes_local_active_claim_without_operation(setup):
    await setup.manager.start()
    await switch(setup, 'local_area')
    assert setup.manager.state()['active_mode'] == 'local_area'
    setup.trace.clear()
    setup.platform.data['hotspot']['state'] = 'off'
    await setup.manager.inspect()
    assert setup.manager.state()['active_mode'] == 'unknown'
    assert setup.trace == [('inspect',)]
    assert setup.viewer.running


@pytest.mark.parametrize('site', ['same-site', 'cross-site'])
def test_top_level_navigation_from_main_is_allowed_but_apis_and_frames_are_not(setup, site):
    with client(setup) as web:
        headers = {'sec-fetch-site': site, 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'document'}
        assert web.get('/', headers=headers).status_code == 200
        assert web.get('/api/session', headers=headers).status_code == 403
        assert web.get('/connection', headers=headers).status_code == 403
        assert web.get('/', headers={**headers, 'sec-fetch-dest': 'iframe'}).status_code == 403
        assert web.get('/', headers={**headers, 'origin': 'http://127.0.0.1:8765'}).status_code == 403
        assert web.get('/', headers={**headers, 'sec-fetch-mode': 'cors'}).status_code == 403


def test_connection_page_is_never_served_when_viewer_stopped(setup):
    with client(setup) as web:
        setup.viewer.running = False
        assert web.get('/connection').status_code == 409


@pytest.mark.parametrize('portfield', ['port', 'main_port'])
async def test_manager_never_probes_alternate_monitor_or_viewer_ports(setup, portfield):
    conf = asdict(setup.files.load_mobile())
    conf[portfield] = 9001
    setup.files.atomic(setup.files.path('mobile.local.json'), json.dumps(conf).encode())
    await setup.manager.start()
    assert setup.manager.last_error == 'viewer_ports_invalid'
    assert not [entry for entry in setup.trace if entry[0] in ('health', 'viewer_start')]
    await switch(setup)
    assert setup.manager.last_error == 'viewer_ports_invalid'
    assert not [entry for entry in setup.trace if entry[0] in ('health', 'connect')]


@pytest.mark.parametrize('failure', [None, 'occupied', 'startup'])
async def test_owned_viewer_only_binds_exact_address_and_closes_its_own_socket(monkeypatch, failure):
    import uvicorn
    events = []
    class Socket:
        def setsockopt(self, *args): events.append('exclusive')
        def bind(self, value):
            events.append(('bind', value))
            if failure == 'occupied':
                raise OSError(PRIVATE)
        def listen(self, count): events.append('listen')
        def setblocking(self, value): assert value is False
        def close(self): events.append('close')
    class Server:
        def __init__(self, config):
            self.started = self.should_exit = False
        async def serve(self, sockets):
            assert len(sockets) == 1
            if failure == 'startup':
                raise RuntimeError(PRIVATE)
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)
    monkeypatch.setattr(network.socket, 'socket', lambda *args: Socket())
    monkeypatch.setattr(MobileSettings, 'require_local_interface', lambda self: events.append('interface_check'))
    monkeypatch.setattr(uvicorn, 'Config', lambda app, **kwargs: kwargs)
    monkeypatch.setattr(uvicorn, 'Server', Server)
    monkeypatch.setattr(network, 'create_mobile_app', lambda settings: object())
    viewer = network.OwnedViewer()
    if failure:
        with pytest.raises(network.NetworkError):
            await viewer.start(MobileSettings('192.168.50.10', token=KEY))
    else:
        await viewer.start(MobileSettings('192.168.50.10', token=KEY))
        assert viewer.running
        await viewer.stop()
    assert events[0] == 'interface_check'
    assert ('bind', ('192.168.50.10', 8766)) in events
    assert events.count('close') == 1
    assert not viewer.running
