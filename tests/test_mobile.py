"""LAN viewer boundaries and freshness using synthetic data; no network/BLE."""
import asyncio
from datetime import timedelta
import ipaddress
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import urllib.request

from fastapi.testclient import TestClient
import pytest

from conftest import sample
from gtbms import mobile
from gtbms.models import stamp, utcnow


KEY = 'A' * 43
ORIGIN = 'http://192.168.50.10:8766'
PRIVATE_MARKER = 'never-forward-private-address-gps-token-or-error'


def settings(**changes):
    values = dict(host='192.168.50.10', prefix_length=24, token=KEY)
    values.update(changes)
    return mobile.MobileSettings(**values)


def status(**changes):
    measurement = sample(source='ble', battery_id='private-battery').to_dict()
    values = dict(mode='ble', state='connected', fresh=True, poll_seconds=2,
                  age_seconds=0, last_success_at=measurement['received_at'],
                  measurement=measurement,
                  prediction=dict(remaining_range_km=80, safe_remaining_range_km=60,
                                  full_range_km=100, reliability='低'))
    values.update(changes)
    return values


def client_for(feed=None, *, peer='192.168.50.2', base_url=ORIGIN):
    return TestClient(mobile.create_mobile_app(settings(), feed=feed),
                      client=(peer, 1234), base_url=base_url)


def local_interfaces(host='192.168.50.10', netmask='255.255.255.0'):
    return {'Wi-Fi': [SimpleNamespace(family=socket.AF_INET, address=host, netmask=netmask)]}


@pytest.mark.parametrize('host', ['0.0.0.0', '127.0.0.1', '169.254.12.3',
                                 '100.64.0.1', '192.0.0.1', '198.18.0.1',
                                 '192.0.2.1', '8.8.8.8', '224.0.0.1',
                                 '::1', 'fc00::1', 'localhost'])
def test_settings_require_specific_rfc1918_ipv4(host):
    with pytest.raises(ValueError):
        settings(host=host).validate()


@pytest.mark.parametrize(('host', 'prefix'), [('10.1.2.3', 8), ('172.16.0.1', 12),
                                           ('172.31.255.254', 16), ('192.168.50.10', 24),
                                           ('192.168.50.10', 32)])
def test_settings_accept_explicit_private_host_and_contained_subnet(host, prefix):
    conf = settings(host=host, prefix_length=prefix).validate()
    assert ipaddress.ip_address(host) in conf.network


@pytest.mark.parametrize('changes', [dict(prefix_length=0), dict(prefix_length=15),
    dict(prefix_length=-1), dict(prefix_length=33), dict(prefix_length=True),
    dict(prefix_length='24'), dict(port=80), dict(port=65536), dict(port=True),
    dict(port=8765), dict(main_port=443), dict(main_port='8765'),
    dict(host='192.168.50.0'), dict(host='192.168.50.255')])
def test_settings_reject_broad_network_invalid_host_or_ports(changes):
    with pytest.raises(ValueError):
        settings(**changes).validate()


@pytest.mark.parametrize('token', ['', 'A' * 42, 'A' * 44, 'A' * 42 + '!',
                                 'A' * 42 + '\n', 'あ' * 43, None, 123])
def test_settings_validate_generated_key_shape(token):
    with pytest.raises(ValueError):
        settings(token=token).validate()


def test_settings_validate_actual_local_interface_and_netmask(monkeypatch):
    monkeypatch.setattr(mobile.psutil, 'net_if_addrs', local_interfaces)
    settings().require_local_interface()
    with pytest.raises(ValueError, match='not assigned'):
        settings(host='192.168.50.11').require_local_interface()
    with pytest.raises(ValueError, match='does not match'):
        settings(prefix_length=25).require_local_interface()


def test_settings_load_utf8_bom_and_validate(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(vars(settings())), encoding='utf-8-sig')
    assert mobile.MobileSettings.load(path) == settings()
    path.write_text(json.dumps(vars(settings(host='0.0.0.0'))), encoding='utf-8')
    with pytest.raises(ValueError):
        mobile.MobileSettings.load(path)


def test_projection_is_allowlist_with_no_identity_position_or_arbitrary_strings():
    upstream = status()
    upstream.update(gps={'latitude': PRIVATE_MARKER}, settings={'token': PRIVATE_MARKER},
                    trips=[PRIVATE_MARKER], last_error=PRIVATE_MARKER, logs=[PRIVATE_MARKER])
    upstream['measurement'].update(id=PRIVATE_MARKER, battery_id=PRIVATE_MARKER,
        connection_id=PRIVATE_MARKER, source=PRIVATE_MARKER, secret=PRIVATE_MARKER,
        alarms=[PRIVATE_MARKER], bms_temperature_c=PRIVATE_MARKER)
    upstream['prediction'].update(reliability=PRIVATE_MARKER, training_data=PRIVATE_MARKER)
    projected = mobile.project_snapshot(upstream)
    assert PRIVATE_MARKER not in json.dumps(projected)
    assert set(projected) == {'schema', 'mode', 'state', 'fresh', 'received_at',
        'age_seconds', 'poll_seconds', 'stale_after_seconds', 'measurement', 'prediction', 'alerts'}
    assert projected['fresh'] is True
    assert projected['measurement']['voltage_v'] == 60
    assert projected['prediction']['reliability'] == '参考値'
    assert 'id' not in projected['measurement']
    assert 'source' not in projected['measurement']


def test_unconfirmed_current_never_publishes_energy_or_prediction():
    upstream = status()
    upstream['measurement'].update(current_direction='unconfirmed', current_a=10, power_w=600)
    projected = mobile.project_snapshot(upstream)
    assert projected['fresh'] is True
    assert projected['measurement']['raw_current_a'] == -10
    assert projected['measurement']['current_a'] is None
    assert projected['measurement']['power_w'] is None
    assert projected['prediction'] is None


def test_protections_are_decoded_locally_quality_flags_never_forwarded():
    upstream = status()
    upstream['measurement'].update(protection_code=0x8001, quality_flags=[PRIVATE_MARKER])
    projected = mobile.project_snapshot(upstream)
    assert projected['state'] == 'invalid' and projected['fresh'] is False
    assert projected['measurement'] is None and projected['prediction'] is None
    assert projected['alerts'] == ['セル過電圧', '未定義保護ビット', '測定品質の確認が必要です']
    assert PRIVATE_MARKER not in json.dumps(projected)


@pytest.mark.parametrize(('state', 'fresh', 'expected'), [
    ('connected', False, 'stale'), ('connecting', True, 'connecting'),
    ('reconnecting', False, 'reconnecting'), ('stopped', True, 'stopped')])
def test_nonfresh_upstream_never_retains_last_measurement(state, fresh, expected):
    projected = mobile.project_snapshot(status(state=state, fresh=fresh))
    assert projected['state'] == expected and projected['fresh'] is False
    assert projected['measurement'] is None and projected['prediction'] is None


@pytest.mark.parametrize(('field', 'value'), [('mode', 'injected'), ('mode', None),
    ('state', 'unknown-private-error'), ('fresh', 'true'), ('fresh', 1),
    ('poll_seconds', 0), ('poll_seconds', 11), ('poll_seconds', True),
    ('poll_seconds', float('nan')), ('age_seconds', -1),
    ('age_seconds', float('inf')), ('age_seconds', True)])
def test_projection_rejects_invalid_top_level_values(field, value):
    with pytest.raises(ValueError):
        mobile.project_snapshot(status(**{field: value}))


@pytest.mark.parametrize(('field', 'value'), [
    ('voltage_v', -1), ('voltage_v', 201), ('voltage_v', '65'),
    ('voltage_v', float('nan')), ('voltage_v', float('inf')), ('voltage_v', True),
    ('raw_current_a', -327.69), ('raw_current_a', 327.68),
    ('soc_pct', -0.1), ('soc_pct', 101), ('remaining_ah', -1),
    ('cell_delta_v', 5.1), ('current_a', '10'), ('power_w', 100001),
    ('protection_code', True), ('protection_code', 65536), ('protection_code', -1),
    ('quality_flags', None), ('current_direction', 'injected'),
    ('charge_mos', 1), ('discharge_mos', None),
    ('cell_voltages_v', [3.7] * 33), ('cell_voltages_v', [6]),
    ('cell_voltages_v', [float('nan')]), ('cell_voltages_v', '3.7'),
    ('temperatures_c', [25] * 17), ('temperatures_c', [-61]),
    ('temperatures_c', [151]), ('temperatures_c', [True]),
])
def test_projection_rejects_invalid_sensor_fields(field, value):
    upstream = status()
    upstream['measurement'][field] = value
    with pytest.raises(ValueError):
        mobile.project_snapshot(upstream)


@pytest.mark.parametrize('value', [-1, 100001, '80', True, float('nan'), float('inf')])
def test_prediction_numbers_validated(value):
    upstream = status()
    upstream['prediction']['remaining_range_km'] = value
    with pytest.raises(ValueError):
        mobile.project_snapshot(upstream)


def test_projection_checks_both_reported_age_and_wall_timestamp():
    upstream = status()
    timestamp = utcnow()
    upstream['last_success_at'] = upstream['measurement']['received_at'] = stamp(timestamp)
    now = timestamp.timestamp()
    assert mobile.project_snapshot(upstream, wall_time=now + 5)['fresh'] is True
    old = mobile.project_snapshot(upstream, wall_time=now + 7)
    assert old['state'] == 'stale' and old['measurement'] is None
    upstream['age_seconds'] = 7
    assert mobile.project_snapshot(upstream, wall_time=now)['fresh'] is False
    upstream['age_seconds'] = 0
    with pytest.raises(ValueError, match='future'):
        mobile.project_snapshot(upstream, wall_time=now - 6)


@pytest.mark.parametrize('change', ['missing_age', 'missing_received', 'mismatched', 'naive', 'malformed'])
def test_fresh_status_requires_consistent_timezone_aware_time(change):
    upstream = status()
    if change == 'missing_age':
        upstream['age_seconds'] = None
    elif change == 'missing_received':
        upstream['last_success_at'] = None
    elif change == 'mismatched':
        upstream['measurement']['received_at'] = stamp(utcnow() - timedelta(seconds=1))
    elif change == 'naive':
        upstream['last_success_at'] = '2026-10-11T00:00:00'
    else:
        upstream['last_success_at'] = PRIVATE_MARKER
    with pytest.raises(ValueError):
        mobile.project_snapshot(upstream)


async def test_snapshot_feed_stales_monotonically_and_returns_independent_copies():
    now = [100.0]
    feed = mobile.SnapshotFeed(8765, fetcher=lambda _: status(), clock=lambda: now[0])
    await feed.refresh()
    first = feed.view()
    assert first['fresh'] is True
    first['measurement']['cell_voltages_v'].append(999)
    assert 999 not in feed.view()['measurement']['cell_voltages_v']
    now[0] += 7
    stale = feed.view()
    assert stale['state'] == 'stale' and stale['fresh'] is False
    assert stale['measurement'] is None and stale['prediction'] is None


async def test_snapshot_feed_clears_private_failure_and_recovers():
    responses = [status(), ValueError(PRIVATE_MARKER), status()]
    def fetcher(port):
        assert port == 8765
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    feed = mobile.SnapshotFeed(8765, fetcher=fetcher)
    await feed.refresh()
    assert feed.view()['fresh'] is True
    await feed.refresh()
    failed = feed.view()
    assert failed['state'] == 'surface_offline'
    assert not failed['fresh'] and failed['measurement'] is None and failed['prediction'] is None
    assert PRIVATE_MARKER not in json.dumps(failed)
    await feed.refresh()
    assert feed.view()['fresh'] is True


async def test_snapshot_feed_counts_slow_fetch_towards_staleness():
    now = [100.0]
    def slow_fetch(_):
        now[0] += 7
        return status()
    feed = mobile.SnapshotFeed(8765, fetcher=slow_fetch, clock=lambda: now[0])
    await feed.refresh()
    assert feed.view()['fresh'] is False


async def test_delivery_timings_separate_fetch_and_cached_age_without_extending_freshness(monkeypatch):
    now = [100.0]
    upstream = status(age_seconds=1)
    upstream['delivery'] = dict(relay_age_ms=PRIVATE_MARKER, upstream_rtt_ms=PRIVATE_MARKER,
                               refresh_interval_ms=PRIVATE_MARKER, token=KEY)
    def fetcher(_):
        now[0] += .2
        return upstream
    project = mobile.project_snapshot
    def slow_project(source):
        now[0] += .05
        return project(source)
    monkeypatch.setattr(mobile, 'project_snapshot', slow_project)
    feed = mobile.SnapshotFeed(8765, fetcher=fetcher, clock=lambda: now[0])
    assert feed.view()['delivery'] == dict(relay_age_ms=None, upstream_rtt_ms=None,
                                         refresh_interval_ms=500)
    await feed.refresh()
    now[0] += .3
    view = feed.view()
    assert view['delivery'] == pytest.approx(dict(relay_age_ms=300, upstream_rtt_ms=200,
                                                refresh_interval_ms=500))
    assert view['age_seconds'] == pytest.approx(1.55)
    assert PRIVATE_MARKER not in json.dumps(view) and KEY not in json.dumps(view)
    # A caller cannot alter shared delivery state through its response object.
    view['delivery']['upstream_rtt_ms'] = 999
    assert feed.view()['delivery']['upstream_rtt_ms'] == pytest.approx(200)
    now[0] += 5
    stale = feed.view()
    assert not stale['fresh'] and stale['measurement'] is None
    assert stale['delivery']['relay_age_ms'] == pytest.approx(5300)


async def test_delivery_metrics_clear_on_failure_and_recover():
    now = [100.0]
    responses = [status(), RuntimeError(PRIVATE_MARKER), status()]
    def fetcher(_):
        now[0] += .1
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    feed = mobile.SnapshotFeed(8765, fetcher=fetcher, clock=lambda: now[0])
    await feed.refresh()
    assert feed.view()['delivery']['upstream_rtt_ms'] == pytest.approx(100)
    await feed.refresh()
    failed = feed.view()
    assert not failed['fresh'] and failed['measurement'] is None
    assert failed['delivery'] == dict(relay_age_ms=None, upstream_rtt_ms=None,
                                     refresh_interval_ms=500)
    assert PRIVATE_MARKER not in json.dumps(failed)
    await feed.refresh()
    assert feed.view()['fresh']
    assert feed.view()['delivery']['upstream_rtt_ms'] == pytest.approx(100)


@pytest.mark.parametrize('elapsed', [-1, 0, .1, float('nan'), float('inf'), float('-inf'), 1e308])
def test_delivery_durations_are_null_or_finite_nonnegative_json(elapsed):
    value = mobile.SnapshotFeed.milliseconds(elapsed)
    json.dumps(value, allow_nan=False)
    assert value is None or value >= 0
    if elapsed in (-1, 0):
        assert value == 0


@pytest.mark.parametrize(('read_seconds', 'wait_seconds'), [
    (0, .5), (.2, .3), (.48, .05), (.5, .05), (.9, .05)])
async def test_feed_uses_start_based_half_second_cadence_with_minimum_wait(monkeypatch, read_seconds, wait_seconds):
    now, starts, waits = [100.0], [], []
    fetching = [False]
    def fetcher(_):
        assert not fetching[0]
        fetching[0] = True
        starts.append(now[0])
        now[0] += read_seconds
        fetching[0] = False
        return status()
    async def sleep(seconds):
        assert not fetching[0]
        waits.append(seconds)
        now[0] += seconds
        if len(waits) == 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(mobile.asyncio, 'sleep', sleep)
    feed = mobile.SnapshotFeed(8765, fetcher=fetcher, clock=lambda: now[0])
    with pytest.raises(asyncio.CancelledError):
        await feed.run()
    assert waits == pytest.approx([wait_seconds] * 3)
    assert starts == pytest.approx([100, 100 + read_seconds + wait_seconds,
                                   100 + 2 * (read_seconds + wait_seconds)])


async def test_slow_pending_fetch_does_not_overlap_or_run_for_each_viewer():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetcher(port):
        calls.append(port)
        entered.set()
        assert release.wait(3)
        return status()
    feed = mobile.SnapshotFeed(8765, fetcher=fetcher)
    task = asyncio.create_task(feed.run())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # Hold one request past the target interval while viewers read the cache.
        await asyncio.sleep(.6)
        for _ in range(20):
            assert not feed.view()['fresh']
        assert calls == [8765]
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('peer', ['192.168.51.2', '192.168.49.255', '10.1.2.3',
                                  '127.0.0.1', '8.8.8.8', '::1', 'not-an-ip'])
def test_api_rejects_peers_outside_configured_subnet(peer):
    response = client_for(peer=peer).get('/api/view', headers={'Authorization': 'Bearer ' + KEY})
    assert response.status_code == 403


@pytest.mark.parametrize('headers', [{'Host': 'localhost:8766'},
    {'Host': 'attacker.example:8766'}, {'Host': '192.168.50.10'},
    {'Origin': 'https://attacker.example'}, {'Origin': 'null'},
    {'Origin': 'http://192.168.50.10:8765'}])
def test_api_rejects_wrong_host_and_cross_origin(headers):
    response = client_for().get('/api/view', headers={**headers, 'Authorization': 'Bearer ' + KEY})
    assert response.status_code == 403


@pytest.mark.parametrize('credential', ['', 'Bearer invalid', 'Basic ' + KEY, 'Bearer ' + KEY + 'x', 'Bearer あ'])
def test_api_requires_exact_bearer_key(credential):
    # ASCII is the HTTP header wire format; test unicode via its valid UTF-8 bytes.
    headers = [(b'authorization', credential.encode('utf-8'))]
    response = client_for().get('/api/view', headers=headers)
    assert response.status_code == 401


def test_query_cookie_and_forwarded_headers_cannot_grant_access():
    client = client_for()
    client.cookies.set('authorization', 'Bearer ' + KEY)
    client.cookies.set('key', KEY)
    for suffix in ('?key=' + KEY, '?token=' + KEY, '?authorization=Bearer%20' + KEY):
        assert client.get('/api/view' + suffix).status_code == 401
    outsider = client_for(peer='8.8.8.8')
    assert outsider.get('/api/view', headers={'Authorization': 'Bearer ' + KEY,
        'X-Forwarded-For': '192.168.50.2', 'X-Forwarded-Host': '192.168.50.10:8766'}).status_code == 403


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'])
def test_all_non_get_methods_rejected_even_with_valid_key(method):
    response = client_for().request(method, '/api/view', headers={'Authorization': 'Bearer ' + KEY})
    assert response.status_code == 405


@pytest.mark.parametrize('path', ['/api/status', '/api/session', '/api/monitor/start',
    '/api/monitor/stop', '/api/settings', '/api/history', '/api/export.csv',
    '/api/export.json', '/api/gps', '/api/trips', '/docs', '/redoc', '/openapi.json',
    '/static/app.js', '/static/index.html', '/mobile.local.json', '/mobile-connect.local.html',
    '/static/../mobile.local.json'])
def test_admin_history_export_gps_settings_and_secret_files_not_served(path):
    response = client_for().get(path, headers={'Authorization': 'Bearer ' + KEY})
    assert response.status_code == 404
    assert KEY not in response.text


async def test_many_clients_and_invalid_keys_never_trigger_extra_upstream_fetch():
    calls = []
    def fetcher(port):
        calls.append(port)
        return status(delivery={'relay_age_ms': PRIVATE_MARKER, 'token': KEY})
    feed = mobile.SnapshotFeed(8765, fetcher=fetcher)
    await feed.refresh()
    first, second = client_for(feed), client_for(feed, peer='192.168.50.3')
    for _ in range(10):
        assert first.get('/api/view').status_code == 401
        for client in (first, second):
            response = client.get('/api/view', headers={'Authorization': 'Bearer ' + KEY, 'Origin': ORIGIN})
            assert response.status_code == 200 and response.json()['fresh'] is True
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.headers['Referrer-Policy'] == 'no-referrer'
            assert response.headers['X-Frame-Options'] == 'DENY'
            assert 'frame-ancestors' in response.headers['Content-Security-Policy']
            assert KEY not in response.text
            assert PRIVATE_MARKER not in response.text
            assert set(response.json()['delivery']) == {'relay_age_ms', 'upstream_rtt_ms', 'refresh_interval_ms'}
            assert response.json()['delivery']['refresh_interval_ms'] == 500
            json.dumps(response.json()['delivery'], allow_nan=False)
    assert calls == [8765]


def test_app_lifespan_starts_only_one_feed_and_cancels_it():
    class Feed:
        started = 0
        stopped = False
        async def run(self):
            self.started += 1
            try:
                await asyncio.Future()
            finally:
                self.stopped = True
        def view(self):
            return {'fresh': False}
    feed = Feed()
    with client_for(feed) as client:
        for _ in range(3):
            assert client.get('/api/view', headers={'Authorization': 'Bearer ' + KEY}).status_code == 200
        assert feed.started == 1
    assert feed.stopped is True


def test_fixed_local_status_fetch_has_no_proxy_redirects_and_bounded_read(monkeypatch):
    observed = {}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            observed['closed'] = True
        def read(self, size):
            observed['read_size'] = size
            return b'{"fresh": false}'
    class Opener:
        def open(self, url, timeout):
            observed.update(url=url, timeout=timeout)
            return Response()
    def build(*handlers):
        observed['handlers'] = handlers
        return Opener()
    monkeypatch.setenv('HTTP_PROXY', 'http://never-contact.invalid:9999')
    monkeypatch.setattr(mobile.urllib.request, 'build_opener', build)
    assert mobile.fetch_local_status(8765) == {'fresh': False}
    assert observed['url'] == 'http://127.0.0.1:8765/api/status'
    assert observed['timeout'] == 3 and observed['read_size'] == mobile.MAX_RESPONSE + 1
    proxy, redirect = observed['handlers']
    assert isinstance(proxy, urllib.request.ProxyHandler) and proxy.proxies == {}
    assert isinstance(redirect, mobile.NoRedirect)
    assert redirect.redirect_request(None, None, 302, '', {}, 'http://external.invalid') is None
    assert observed['closed'] is True


@pytest.mark.parametrize('payload', [b'x' * (mobile.MAX_RESPONSE + 1), b'{"value": NaN}',
                                    b'{"value": Infinity}', b'{"value": -Infinity}', b'invalid-json'],
                         ids=['oversized', 'nan', 'positive-infinity', 'negative-infinity', 'invalid-json'])
def test_status_fetch_rejects_oversized_or_nonfinite_json(monkeypatch, payload):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def read(self, _):
            return payload
    monkeypatch.setattr(mobile.urllib.request, 'build_opener',
                        lambda *_: SimpleNamespace(open=lambda *_, **__: Response()))
    with pytest.raises(ValueError):
        mobile.fetch_local_status(8765)


def test_connection_key_written_only_to_local_page_not_stdout_or_http(tmp_path, capsys):
    path = tmp_path / 'mobile-connect.local.html'
    assert mobile.connection_page(settings(), path) == path
    content = path.read_text(encoding='utf-8')
    assert ORIGIN + '/#key=' + KEY in content
    assert '<code>' + KEY + '</code>' in content
    assert 'no-referrer' in content
    assert 'Surfaceが案内する同じWi-Fi' in content
    assert '所有するモバイルWi-Fi' not in content
    assert KEY not in capsys.readouterr().out
    assert client_for().get('/mobile-connect.local.html').status_code == 404
    assert not list(tmp_path.glob('.mobile-*.tmp'))


def test_cli_init_reuses_existing_key_rotates_only_when_requested(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mobile.psutil, 'net_if_addrs', local_interfaces)
    keys = iter([KEY, 'B' * 43])
    monkeypatch.setattr(mobile.secrets, 'token_urlsafe', lambda size: next(keys) if size == 32 else None)
    args = ['gtbms.mobile', 'init', '--host', '192.168.50.10', '--prefix-length', '24']
    monkeypatch.setattr(sys, 'argv', args)
    mobile.main()
    assert mobile.MobileSettings.load().token == KEY
    mobile.main()
    assert mobile.MobileSettings.load().token == KEY
    monkeypatch.setattr(sys, 'argv', args + ['--rotate-key'])
    mobile.main()
    assert mobile.MobileSettings.load().token == 'B' * 43
    page = Path('mobile-connect.local.html').read_text(encoding='utf-8')
    assert 'B' * 43 in page and KEY not in page
    output = capsys.readouterr().out
    assert KEY not in output and 'B' * 43 not in output


def test_cli_share_recreates_page_without_printing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path('mobile.local.json').write_text(json.dumps(vars(settings())), encoding='utf-8')
    monkeypatch.setattr(mobile.psutil, 'net_if_addrs', local_interfaces)
    monkeypatch.setattr(sys, 'argv', ['gtbms.mobile', 'share'])
    mobile.main()
    assert KEY in Path('mobile-connect.local.html').read_text(encoding='utf-8')
    assert KEY not in capsys.readouterr().out


def test_cli_invalid_interface_creates_no_share_or_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mobile.psutil, 'net_if_addrs', lambda: local_interfaces('192.168.10.2'))
    monkeypatch.setattr(sys, 'argv', ['gtbms.mobile', 'init', '--host', '192.168.50.10', '--prefix-length', '24'])
    with pytest.raises(SystemExit) as error:
        mobile.main()
    assert error.value.code == 1
    assert not Path('mobile.local.json').exists()
    assert not Path('mobile-connect.local.html').exists()
    assert 'ValueError' in capsys.readouterr().out


def test_cli_serve_pins_interface_disables_proxy_trust_ws_and_access_logging(tmp_path, monkeypatch):
    import uvicorn
    monkeypatch.chdir(tmp_path)
    Path('mobile.local.json').write_text(json.dumps(vars(settings())), encoding='utf-8')
    monkeypatch.setattr(mobile.psutil, 'net_if_addrs', local_interfaces)
    monkeypatch.setattr(sys, 'argv', ['gtbms.mobile', 'serve'])
    observed = {}
    monkeypatch.setattr(uvicorn, 'run', lambda app, **kwargs: observed.update(kwargs))
    mobile.main()
    assert observed == dict(host='192.168.50.10', port=8766, access_log=False,
                            log_level='warning', proxy_headers=False, workers=1,
                            limit_concurrency=32, ws='none')
