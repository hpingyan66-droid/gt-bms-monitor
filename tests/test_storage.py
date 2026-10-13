import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from conftest import sample,timeline
from gtbms.config import Config
from gtbms.models import stamp,utcnow
from gtbms.storage import Store,_exclusive_file_lock
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


def test_backups_and_rotation(store,config,monkeypatch):
    config.backup_keep=2
    store.save_sample(sample())
    fixed=datetime(2026,1,2,3,4,5,678901,tzinfo=timezone.utc)
    identifiers=iter((UUID(int=3),UUID(int=2),UUID(int=1)))
    monkeypatch.setattr("gtbms.storage.utcnow",lambda:fixed)
    monkeypatch.setattr("gtbms.storage.uuid4",lambda:next(identifiers))
    paths=[]
    for _ in range(3):
        paths.append(store.backup())
    path=paths[-1]
    assert len(set(paths))==3
    assert path.is_file()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]==1
    assert set(path.parent.glob("*.sqlite3"))==set(paths[-2:])
    assert not paths[0].exists()


def test_backup_lock_serializes_processes(tmp_path):
    backup_dir=tmp_path/"shared-backups"
    start=tmp_path/"start"
    worker="""
import sys,time
from pathlib import Path
from gtbms.config import Config
from gtbms.storage import Store
data_dir,backup_dir,ready,start=sys.argv[1:]
store=Store(Config(data_dir=data_dir,backup_dir=backup_dir,backup_keep=2,autostart_monitor=False))
try:
    Path(ready).touch()
    while not Path(start).exists():
        time.sleep(0.01)
    print(store.backup().resolve(),flush=True)
finally:
    store.close()
"""
    processes=[]
    try:
        for index in range(2):
            processes.append(subprocess.Popen(
                [sys.executable,"-c",worker,str(tmp_path/f"data-{index}"),str(backup_dir),
                 str(tmp_path/f"ready-{index}"),str(start)],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True))
        deadline=time.monotonic()+10
        while not all((tmp_path/f"ready-{index}").exists() for index in range(2)):
            if any(process.poll() is not None for process in processes) or time.monotonic() >= deadline:
                pytest.fail("Backup worker failed to reach the start barrier")
            time.sleep(0.01)
        start.touch()
        results=[process.communicate(timeout=30) for process in processes]
    finally:
        start.touch(exist_ok=True)
        for process in processes:
            if process.poll() is None:
                process.kill()
            if process.stdout and not process.stdout.closed:
                process.communicate()
    for process,(stdout,stderr) in zip(processes,results):
        assert process.returncode==0,stderr
    paths=[Path(stdout.strip()) for stdout,_ in results]
    assert len(set(paths))==2
    assert {int(path.stem.rsplit("-",2)[1]) for path in paths}=={1,2}
    assert set(backup_dir.glob("gtbms-*.sqlite3"))==set(paths)
    for path in paths:
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"


def test_backup_file_lock_blocks_another_process(tmp_path):
    lock=tmp_path/"backup.lock"
    worker="""
import sys,time
from pathlib import Path
from gtbms.storage import _exclusive_file_lock
lock,ready,entered,release=sys.argv[1:]
Path(ready).touch()
with _exclusive_file_lock(Path(lock)):
    Path(entered).touch()
    while not Path(release).exists():
        time.sleep(0.01)
"""
    markers={name:tmp_path/name for name in
             ("ready-1","entered-1","release-1","ready-2","entered-2","release-2")}
    processes=[]
    def wait_for(path,process):
        deadline=time.monotonic()+10
        while not path.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                pytest.fail(f"Backup lock worker did not create {path.name}")
            time.sleep(0.01)
    try:
        first=subprocess.Popen(
            [sys.executable,"-c",worker,str(lock),str(markers["ready-1"]),
             str(markers["entered-1"]),str(markers["release-1"])],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        processes.append(first)
        wait_for(markers["entered-1"],first)
        second=subprocess.Popen(
            [sys.executable,"-c",worker,str(lock),str(markers["ready-2"]),
             str(markers["entered-2"]),str(markers["release-2"])],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        processes.append(second)
        wait_for(markers["ready-2"],second)
        time.sleep(0.5)
        assert not markers["entered-2"].exists()
        markers["release-1"].touch()
        wait_for(markers["entered-2"],second)
        markers["release-2"].touch()
        results=[process.communicate(timeout=10) for process in processes]
    finally:
        markers["release-1"].touch(exist_ok=True)
        markers["release-2"].touch(exist_ok=True)
        for process in processes:
            if process.poll() is None:
                process.kill()
            if process.stdout and not process.stdout.closed:
                process.communicate()
    for process,(_,stderr) in zip(processes,results):
        assert process.returncode==0,stderr


def test_backup_file_lock_releases_after_error(tmp_path):
    lock=tmp_path/"backup.lock"
    with pytest.raises(RuntimeError):
        with _exclusive_file_lock(lock):
            raise RuntimeError("synthetic failure")
    with _exclusive_file_lock(lock):
        pass


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
