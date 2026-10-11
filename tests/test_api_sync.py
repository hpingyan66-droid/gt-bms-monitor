import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from conftest import sample,timeline
from gtbms.app import create_app
from gtbms.sync import SyncWorker


def test_dashboard_and_status_offline(client):
    assert client.get('/').status_code==200
    assert 'バッテリー' in client.get('/').text
    status=client.get('/api/status').json()
    assert status['measurement'] is None
    assert status['prediction']['remaining_range_km'] is None
    assert status['sync']['state']=='disabled'
    assert client.get('/static/app.js').status_code==200


def test_write_requires_token_and_same_origin(client):
    assert client.post('/api/monitor/start',json={},headers={'X-GTBMS-Token':''}).status_code==403
    assert client.post('/api/monitor/start',json={},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/status',headers={'Host':'evil.example'}).status_code==400


def test_input_validation(client):
    assert client.post('/api/settings/interval',json={'seconds':.1}).status_code==422
    assert client.post('/api/trips/stop',json={'distance_km':-1}).status_code==422
    assert client.post('/api/trips/start',json={'unexpected':'value'}).status_code==422


def test_export_has_explicit_source_and_units(client):
    store=client.app.state.store
    for s in timeline(3):store.save_sample(s)
    data=client.get('/api/export/json').json()
    assert len(data)==3
    assert all(row['source']=='mock' for row in data)
    csv_response=client.get('/api/export/csv')
    rows=list(csv.DictReader(io.StringIO(csv_response.text.lstrip('\ufeff'))))
    assert len(rows)==3
    assert 'raw_current_a' in rows[0] and 'bms_measured_at' in rows[0]


def test_export_more_than_one_page(client):
    store=client.app.state.store
    for s in timeline(1010):store.save_sample(s)
    data=client.get('/api/export/json').json()
    assert len(data)==1010
    assert len({row['id'] for row in data})==1010


def test_export_preserves_identical_timestamps(client):
    from gtbms.models import utcnow
    at = utcnow()
    for _ in range(1005):
        client.app.state.store.save_sample(sample(at))
    rows = client.get('/api/export/json').json()
    assert len(rows) == len({row['id'] for row in rows}) == 1005


def test_csv_preserves_newer_record_fields(client):
    store = client.app.state.store
    store.save_trip({'id':'a', 'started_at':'2026-01-01T00:00:00+00:00', 'ended_at':'2026-01-01T00:01:00+00:00'})
    store.save_trip({'id':'b', 'started_at':'2026-01-02T00:00:00+00:00', 'ended_at':'2026-01-02T00:01:00+00:00', 'new_metric':42})
    response = client.get('/api/export/csv?dataset=trips')
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip('\ufeff'))))
    assert rows[0]['new_metric'] == ''
    assert rows[1]['new_metric'] == '42'


def test_interval_change_during_ride_is_rejected(client):
    client.post('/api/trips/start', json={}).raise_for_status()
    response = client.post('/api/settings/interval', json={'seconds':10})
    assert response.status_code == 409
    assert client.get('/api/status').json()['poll_seconds'] == 2


def test_interval_save_failure_keeps_running_settings(config, tmp_path, monkeypatch):
    from gtbms.config import Config
    def fail(*args):
        raise OSError('test disk failure')
    monkeypatch.setattr(Config, 'save', fail)
    app = create_app(config, tmp_path/'config.json')
    with TestClient(app, base_url='http://127.0.0.1:8765', raise_server_exceptions=False) as session:
        token = session.get('/api/session').json()['token']
        result = session.post('/api/settings/interval', json={'seconds':10}, headers={'X-GTBMS-Token':token})
        assert result.status_code == 500
        assert config.poll_seconds == 2
        assert config.max_gap_seconds == 10


def test_trip_and_distance_api(client):
    response=client.post('/api/trips/start',json={'ambient_c':20})
    assert response.status_code==200
    trip=response.json()
    assert client.post('/api/trips/stop',json={'distance_km':2.5}).status_code==200
    assert client.post('/api/trips/'+trip['id']+'/distance',json={'distance_km':3}).json()['distance_km']==3
    assert client.get('/api/trips').json()[0]['distance_source']=='manual'


def test_diagnostics_backup_and_gps_disabled(client):
    assert client.get('/api/diagnostics').json()['integrity']==['ok']
    assert client.post('/api/backup',json={}).json()['integrity']=='ok'
    assert client.post('/api/trips/id/gps',json={'measured_at':'2026-01-01T00:00:00Z','latitude':35,'longitude':139}).status_code==409


def test_websocket_current_state(client):
    with client.websocket_connect('ws://127.0.0.1:8765/ws') as ws:
        assert ws.receive_json()['measurement'] is None


def test_sync_disabled_sends_nothing(store):
    def fail(*args):raise AssertionError('must not transmit')
    worker=SyncWorker(store,fail);worker.once();assert worker.state=='disabled'


def test_sync_retry_and_dedup_ack(store,config):
    config.sync_enabled=True;config.sync_url='https://example.invalid/sync'
    s=sample();store.save_sample(s)
    calls=[]
    def sender(url,body):
        calls.append(body)
        if len(calls)==1:raise ConnectionError()
        return {'acknowledged_ids':[s.id]}
    worker=SyncWorker(store,sender)
    worker.once();assert worker.state=='offline'
    assert store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==1
    with store.transaction() as db:db.execute("UPDATE outbox SET next_attempt='2000-01-01'")
    worker.once();assert worker.state=='online'
    assert calls[0]['items'][0]['id']==calls[1]['items'][0]['id']
    assert store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_bad_ack_keeps_unsent_data(store,config):
    config.sync_enabled=True;store.save_sample(sample())
    worker=SyncWorker(store,lambda *args:{'acknowledged_ids':['unknown-id']})
    worker.once()
    assert worker.state=='offline'
    assert store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==1
