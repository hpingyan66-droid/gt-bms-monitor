import pytest
from gtbms.config import Config
from gtbms.analysis import RideService
from gtbms.sync import SyncWorker
from conftest import sample


@pytest.mark.parametrize('changes',[{'poll_seconds':0},{'poll_seconds':11},{'poll_seconds':float('nan')},{'measured_usable_wh':-1},{'reserve_fraction':1},{'expected_cells':16.5},{'backup_keep':1.5},{'mode':'wrong'},{'current_direction':'wrong'},{'sync_enabled':True,'sync_url':'http://example.invalid'}])
def test_invalid_config(changes):
    with pytest.raises(ValueError):Config(**changes).validate()


@pytest.mark.parametrize('name', ['pair', 'autostart_monitor', 'gps_enabled', 'sync_enabled', 'sync_include_gps'])
@pytest.mark.parametrize('value', ['false', 'true', 0, 1, None])
def test_config_requires_explicit_boolean_opt_in(name, value):
    with pytest.raises(ValueError, match=f'{name} must be a JSON boolean'):
        Config(**{name: value}).validate()


@pytest.mark.parametrize('name', [
    'poll_seconds', 'request_timeout_seconds', 'nominal_voltage_v', 'configured_capacity_ah',
    'max_gap_seconds', 'stale_seconds', 'backup_hours', 'reserve_fraction', 'measured_usable_wh',
    'expected_cells', 'port', 'retention_days', 'backup_keep', 'mock_disconnect_every',
])
def test_config_rejects_boolean_numeric_values(name):
    with pytest.raises(ValueError):
        Config(**{name: True}).validate()


@pytest.mark.parametrize('changes', [
    {'expected_cells': '16'}, {'reserve_fraction': '0.15'}, {'measured_usable_wh': '2900'},
    {'retention_days': None}, {'battery_id': 1}, {'ble_address': []}, {'data_dir': ' '},
    {'backup_dir': None}, {'sync_url': 0}, {'reserve_fraction': float('nan')},
    {'measured_usable_wh': float('inf')},
])
def test_config_bad_json_types_raise_value_error(changes):
    with pytest.raises(ValueError):
        Config(**changes).validate()


def test_config_atomic_save_concurrent_writers(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Barrier, Lock

    path = tmp_path / 'config.local.json'
    original_replace = Path.replace
    barrier, rename_lock = Barrier(2), Lock()

    def simultaneous_replace(self, target):
        # Both writes finish before either rename. A shared temporary filename
        # would cause the second rename to fail or overwrite the first payload.
        barrier.wait(timeout=5)
        with rename_lock:
            return original_replace(self, target)

    monkeypatch.setattr(Path, 'replace', simultaneous_replace)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(Config(battery_id=f'battery-{n}', poll_seconds=n).save, path)
                   for n in (3, 8)]
        for future in futures:
            future.result(timeout=10)
    stored = Config.load(path)
    assert stored.battery_id == f'battery-{stored.poll_seconds}'
    assert list(tmp_path.iterdir()) == [path]


def test_config_failed_replace_keeps_original_and_removes_temporary_file(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / 'config.local.json'
    Config(poll_seconds=3).save(path)
    original = path.read_bytes()

    def fail_replace(self, target):
        raise PermissionError('simulated locked configuration')

    monkeypatch.setattr(Path, 'replace', fail_replace)
    with pytest.raises(PermissionError):
        Config(poll_seconds=8).save(path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_doctor_distinguishes_arm_windows_from_x64_python(monkeypatch):
    from gtbms import cli

    monkeypatch.setattr(cli.platform, 'machine', lambda: 'ARM64')
    monkeypatch.setattr(cli.sysconfig, 'get_platform', lambda: 'win-amd64')
    report = cli.environment_report()
    assert report['os_arch'] == 'ARM64'
    assert report['python_arch'] == 'amd64'
    assert report['python_platform'] == 'win-amd64'


def test_slow_poll_has_suitable_staleness_and_gap():
    config=Config(poll_seconds=10).validate()
    assert config.max_gap_seconds>=30
    assert config.stale_seconds>=30


def test_disconnected_start_soc_not_read_from_history(store):
    store.save_sample(sample())
    rides=RideService(store,lambda:None)
    assert rides.start()['start_soc_pct'] is None


def test_trip_correction_sync_has_new_id(store,config):
    config.sync_enabled=True
    rides=RideService(store)
    rides.start();trip=rides.stop(5)
    corrected=rides.set_distance(trip['id'],10)
    assert corrected['revision_id']!=trip['revision_id']
    rows=store.db.execute("SELECT id FROM outbox WHERE kind='trip'").fetchall()
    assert len(rows)==2


def test_gps_not_sent_when_opt_in_removed(store,config):
    config.sync_enabled=True;config.sync_include_gps=True
    from gtbms.models import stamp
    rides=RideService(store);trip=rides.start()
    store.save_gps(trip['id'],{'latitude':35,'longitude':139,'measured_at':stamp()})
    config.sync_include_gps=False
    def must_not_send(*args):raise AssertionError('GPS must remain local')
    worker=SyncWorker(store,must_not_send);worker.once()
    assert worker.state=='idle'


def test_gps_and_gap_export(client):
    store=client.app.state.store
    from gtbms.models import stamp
    trip=client.post('/api/trips/start',json={}).json()
    store.save_gps(trip['id'],{'latitude':35,'longitude':139,'measured_at':stamp()})
    store.gap(stamp(),stamp(),'test')
    assert client.get('/api/export/json?dataset=gps').json()[0]['latitude']==35
    assert client.get('/api/export/json?dataset=gaps').json()[0]['reason']=='test'
    assert client.get('/api/trips/'+trip['id']+'/gps').status_code==200
