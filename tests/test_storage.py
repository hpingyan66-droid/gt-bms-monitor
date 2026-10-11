import json
import sqlite3
from datetime import timedelta

import pytest

from conftest import sample,timeline
from gtbms.config import Config
from gtbms.models import stamp,utcnow
from gtbms.storage import Store
from gtbms.analysis import RideService


def test_wal_and_integrity(store):
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0]=="wal"
    assert store.db.execute("PRAGMA synchronous").fetchone()[0]==2
    assert store.integrity()==["ok"]


def test_persistence_and_utc(store):
    s=sample();store.save_sample(s)
    assert store.latest()["id"]==s.id
    assert store.latest()["received_at"].endswith("+00:00")
    assert store.latest()["bms_measured_at"] is None


def test_battery_source_isolation(store,config):
    store.save_sample(sample())
    config.mode="ble"
    separate=Store(config)
    assert separate.latest() is None
    separate.close()
    config.mode="mock"


def test_backups_and_rotation(store,config):
    config.backup_keep=2
    store.save_sample(sample())
    for _ in range(3):
        path=store.backup()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]==1
    assert len(list(path.parent.glob("*.sqlite3")))==2


def test_sample_and_outbox_atomic(store,config):
    config.sync_enabled=True
    s=sample();store.save_sample(s)
    with pytest.raises(sqlite3.IntegrityError):store.save_sample(s)
    assert store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]==1


def test_retention_preserves_open_trip(store,config):
    config.retention_days=1
    old=utcnow()-timedelta(days=10)
    store.save_sample(sample(old))
    trip={"id":"old-trip","started_at":stamp(old-timedelta(hours=1)),"ended_at":None}
    store.save_trip(trip)
    store.prune()
    assert len(store.samples())==1
    trip["ended_at"]=stamp();store.save_trip(trip)
    store.prune()
    assert not store.samples()


def test_gps_time_bounds_and_dedup(store):
    rides=RideService(store);trip=rides.start()
    point={"measured_at":stamp(),"latitude":35,"longitude":139}
    store.save_gps(trip["id"],point);store.save_gps(trip["id"],point)
    assert len(store.gps_points(trip["id"]))==1
    point["measured_at"]=stamp(utcnow()-timedelta(days=1))
    with pytest.raises(ValueError):store.save_gps(trip["id"],point)


def test_high_volume_writes(store):
    for s in timeline(2000,2):store.save_sample(s)
    assert len(store.samples())==2000
    assert store.integrity()==["ok"]


def test_export_snapshot_is_stable_without_blocking_writes(store):
    store.save_sample(sample())
    with store.export_snapshot('samples', None, stamp(utcnow()+timedelta(hours=1))) as rows:
        assert len(list(rows())) == 1
        store.save_sample(sample())
        assert len(list(rows())) == 1
    assert len(store.samples()) == 2


def test_store_rejects_gps_after_trip_close(store):
    rides = RideService(store)
    trip = rides.start()
    point = {'measured_at':stamp(), 'latitude':35, 'longitude':139}
    rides.stop(1)
    with pytest.raises(ValueError, match='closed'):
        store.save_gps(trip['id'], point)
