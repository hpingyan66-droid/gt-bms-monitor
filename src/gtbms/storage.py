from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .config import Config
from .models import Sample, parse_time, stamp, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS batteries(id TEXT PRIMARY KEY, source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, battery_id TEXT NOT NULL REFERENCES batteries(id),
 received_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS samples_time ON samples(battery_id, received_at);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, battery_id TEXT NOT NULL, at TEXT NOT NULL,
 kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_time ON events(battery_id, at);
CREATE TABLE IF NOT EXISTS gaps(id INTEGER PRIMARY KEY, battery_id TEXT NOT NULL,
 started_at TEXT NOT NULL, ended_at TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trips(id TEXT PRIMARY KEY, battery_id TEXT NOT NULL REFERENCES batteries(id),
 started_at TEXT NOT NULL, ended_at TEXT, payload TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_trip ON trips(battery_id) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS gps(id TEXT PRIMARY KEY, trip_id TEXT NOT NULL REFERENCES trips(id),
 measured_at TEXT NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS gps_time ON gps(trip_id, measured_at);
CREATE TABLE IF NOT EXISTS predictions(id TEXT PRIMARY KEY, battery_id TEXT NOT NULL,
 at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, config: Config):
        self.config = config
        Path(config.data_dir).mkdir(parents=True, exist_ok=True)
        self.path = Path(config.data_dir)/"gtbms.sqlite3"
        self.lock = threading.RLock()
        self.backup_lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=10000")
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 1:
            raise ValueError("Database schema newer than this application")
        self.db.executescript(SCHEMA)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO batteries VALUES(?,?,?)", (config.key, config.mode, stamp()))

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    def enqueue(self, db, item_id: str, kind: str, payload: dict):
        if self.config.sync_enabled:
            db.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,0,?)",
                       (item_id, kind, json.dumps(payload, allow_nan=False), stamp()))

    def save_sample(self, sample: Sample):
        payload = sample.to_dict()
        with self.transaction() as db:
            db.execute("INSERT INTO samples VALUES(?,?,?,?)",
                       (sample.id, sample.battery_id, sample.received_at, json.dumps(payload, allow_nan=False)))
            self.enqueue(db, sample.id, "sample", payload)

    def event(self, kind: str, detail: str = "", at: str | None = None):
        # Callers supply fixed messages/error class names, never BLE addresses or GPS.
        with self.transaction() as db:
            db.execute("INSERT INTO events(battery_id,at,kind,detail) VALUES(?,?,?,?)",
                       (self.config.key, at or stamp(), kind, detail))

    def gap(self, start: str, end: str, reason: str):
        with self.transaction() as db:
            db.execute("INSERT INTO gaps(battery_id,started_at,ended_at,reason) VALUES(?,?,?,?)",
                       (self.config.key, start, end, reason))

    def samples(self, *, start: str | None = None, end: str | None = None,
                limit: int | None = None, after: str | None = None) -> list[dict]:
        sql, params = "SELECT payload FROM samples WHERE battery_id=?", [self.config.key]
        for key, op, value in (("received_at", ">=", start), ("received_at", "<=", end), ("received_at", ">", after)):
            if value:
                sql += f" AND {key}{op}?"
                params.append(stamp(parse_time(value)))
        sql += " ORDER BY received_at " + ("DESC" if limit and not after else "ASC")
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.lock:
            rows = self.db.execute(sql, params).fetchall()
        result = [json.loads(row[0]) for row in rows]
        return list(reversed(result)) if limit and not after else result

    def latest(self) -> dict | None:
        rows = self.samples(limit=1)
        return rows[0] if rows else None

    def events(self, limit: int = 100) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE battery_id=? ORDER BY id DESC LIMIT ?",
                                                   (self.config.key, limit))]

    def gaps(self, limit: int = 100) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM gaps WHERE battery_id=? ORDER BY id DESC LIMIT ?",
                                                   (self.config.key, limit))]

    def trips(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT payload FROM trips WHERE battery_id=? ORDER BY started_at DESC LIMIT ?",
                                   (self.config.key, limit)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def trip(self, trip_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT payload FROM trips WHERE id=? AND battery_id=?", (trip_id, self.config.key)).fetchone()
        if not row:
            raise ValueError("Trip not found")
        return json.loads(row[0])

    def open_trip(self) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT payload FROM trips WHERE battery_id=? AND ended_at IS NULL", (self.config.key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_trip(self, trip: dict):
        trip["updated_at"] = stamp()
        trip["revision_id"] = str(uuid4())
        with self.transaction() as db:
            db.execute("INSERT INTO trips VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended_at=excluded.ended_at,payload=excluded.payload",
                       (trip["id"], self.config.key, trip["started_at"], trip.get("ended_at"), json.dumps(trip, allow_nan=False)))
            if trip.get("ended_at"):
                # Corrections are distinct immutable sync events for the same trip entity.
                self.enqueue(db, trip["revision_id"], "trip", trip)

    def gps_points(self, trip_id: str) -> list[dict]:
        self.trip(trip_id)
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM gps WHERE trip_id=? ORDER BY measured_at", (trip_id,))]

    def save_gps(self, trip_id: str, point: dict):
        with self.transaction() as db:
            trip = self.trip(trip_id)
            if trip.get("ended_at"):
                raise ValueError("GPS recording is closed for this trip")
            measured = parse_time(point["measured_at"])
            if measured < parse_time(trip["started_at"]):
                raise ValueError("GPS point outside trip")
            point = dict(point, id=str(uuid4()), trip_id=trip_id, measured_at=stamp(measured), received_at=stamp())
            cursor = db.execute("INSERT OR IGNORE INTO gps VALUES(?,?,?,?,?)",
                                (point["id"], trip_id, point["measured_at"], point["received_at"], json.dumps(point, allow_nan=False)))
            if cursor.rowcount and self.config.sync_include_gps:
                self.enqueue(db, point["id"], "gps", point)

    def prediction(self, payload: dict):
        with self.transaction() as db:
            db.execute("INSERT INTO predictions VALUES(?,?,?,?)", (str(uuid4()), self.config.key, stamp(), json.dumps(payload, allow_nan=False)))

    def predictions(self, limit=100):
        with self.lock:
            return [dict(at=r[0], **json.loads(r[1])) for r in self.db.execute(
                "SELECT at,payload FROM predictions WHERE battery_id=? ORDER BY at DESC LIMIT ?", (self.config.key, limit))]

    def integrity(self) -> list[str]:
        with self.lock:
            result = [r[0] for r in self.db.execute("PRAGMA integrity_check")]
            fk = list(self.db.execute("PRAGMA foreign_key_check"))
        return result + (["foreign_key_error"] if fk else [])

    @contextmanager
    def export_snapshot(self, dataset: str, start: str | None, end: str):
        """Independent read snapshot: bounded iteration without missing timestamp ties.

        Re-running the iterator uses the same snapshot, so CSV can discover all
        columns from older and newer record shapes without buffering telemetry.
        """
        table, timecol = {"samples": ("samples", "received_at"), "trips": ("trips", "started_at"),
                          "events": ("events", "at"), "predictions": ("predictions", "at"),
                          "gaps": ("gaps", "started_at"), "gps": ("gps", "measured_at")}[dataset]
        prefix = "gps." if dataset == "gps" else ""
        if dataset == "gps":
            sql = "SELECT gps.* FROM gps JOIN trips ON trips.id=gps.trip_id WHERE trips.battery_id=?"
        else:
            sql = f"SELECT * FROM {table} WHERE battery_id=?"
        params = [self.config.key, end]
        sql += f" AND {prefix}{timecol}<=?"
        if start:
            sql += f" AND {prefix}{timecol}>=?"
            params.append(start)
        sql += f" ORDER BY {prefix}{timecol}, {prefix}id"
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True,
                                     check_same_thread=False, timeout=10)) as reader:
            reader.row_factory = sqlite3.Row
            reader.execute("BEGIN")
            # Acquire the snapshot before yielding the factory (not on a later pass).
            reader.execute("SELECT COUNT(*) FROM batteries").fetchone()
            def rows():
                cursor = reader.execute(sql, params)
                try:
                    while batch := cursor.fetchmany(1000):
                        for row in batch:
                            if "payload" in row.keys():
                                payload = json.loads(row["payload"])
                                if dataset == "predictions":
                                    payload = dict(id=row["id"], at=row["at"], **payload)
                                yield payload
                            else:
                                yield dict(row)
                finally:
                    cursor.close()
            yield rows

    def backup(self) -> Path:
        with self.backup_lock:
            return self._backup()

    def _backup(self) -> Path:
        root = Path(self.config.backup_dir)
        root.mkdir(parents=True, exist_ok=True)
        target = root/f"gtbms-{utcnow():%Y%m%dT%H%M%S%fZ}.sqlite3"
        temp = target.with_suffix(".tmp")
        # A dedicated connection avoids holding the live writer lock during a
        # growing database's backup and integrity check.
        with closing(sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True, timeout=10)) as source, closing(sqlite3.connect(temp)) as dest:
            source.backup(dest, pages=256)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity failed")
        temp.replace(target)
        # Resolve each exact target within configured backup directory before removal.
        for old in sorted(root.glob("gtbms-*.sqlite3"))[:-self.config.backup_keep]:
            if old.resolve().parent != root.resolve():
                raise ValueError("Unsafe backup target")
            old.unlink()
        return target

    def prune(self):
        if not self.config.retention_days:
            return
        cutoff = stamp(utcnow()-timedelta(days=self.config.retention_days))
        with self.transaction() as db:
            # Retain all samples supporting an unfinished trip. Never prune outbox.
            db.execute("DELETE FROM samples WHERE received_at<? AND NOT EXISTS (SELECT 1 FROM trips WHERE trips.battery_id=samples.battery_id AND ended_at IS NULL AND started_at<=samples.received_at)", (cutoff,))
            for table, column in (("events", "at"), ("gaps", "ended_at"), ("predictions", "at")):
                db.execute(f"DELETE FROM {table} WHERE {column}<?", (cutoff,))
            db.execute("DELETE FROM gps WHERE measured_at<? AND trip_id IN (SELECT id FROM trips WHERE ended_at IS NOT NULL)", (cutoff,))

    def close(self):
        with self.lock:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.close()
