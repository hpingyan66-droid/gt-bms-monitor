"""Synthetic emulator fixtures. Never contains real journeys or BMS addresses."""
from pathlib import Path
import json
import sqlite3

MODEL_KEY = 'synthetic-recovery-model'
MODEL = json.dumps({'schema': 1, 'bins': [
    {'meters': 1234.5, 'wh': 20.25, 'seconds': 300, 'mean': 16.4, 'variance': 2.5, 'windows': 8},
    {'meters': 0, 'wh': 0, 'seconds': 0, 'mean': 0, 'variance': 0, 'windows': 0},
    {'meters': 0, 'wh': 0, 'seconds': 0, 'mean': 0, 'variance': 0, 'windows': 0},
    {'meters': 0, 'wh': 0, 'seconds': 0, 'mean': 0, 'variance': 0, 'windows': 0}]}, separators=(',', ':'))


def seed(root, wal=False):
    d = Path(root) / 'databases'
    d.mkdir(parents=True)
    h = sqlite3.connect(d / 'gtbms-android.db')
    t = sqlite3.connect(d / 'gtbms-trips.db')
    for db in (h, t):
        if wal:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('PRAGMA user_version=1')
    h.executescript('''CREATE TABLE samples (id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT NOT NULL, source TEXT NOT NULL, session TEXT NOT NULL,
        device_key TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX samples_session ON samples(session,id);''')
    for i in range(3):
        payload = json.dumps({'voltage': 60.2, 'current': -2.5 + i, 'soc': 78,
                              'marker': '合成データ・復旧試験', 'cells': [3.75] * 16}, ensure_ascii=False)
        h.execute('INSERT INTO samples VALUES(?,?,?,?,?,?)',
                  (i + 1, '2026-10-12T00:00:00Z', 'ble', 'synthetic-session', 'synthetic-device', payload))
    t.executescript('''CREATE TABLE trips(id TEXT PRIMARY KEY,started INTEGER,ended INTEGER,
        state TEXT,profile TEXT,meters REAL DEFAULT 0,estimated REAL DEFAULT 0,wh REAL DEFAULT 0,
        regen REAL DEFAULT 0,covered REAL DEFAULT 0,uncovered REAL DEFAULT 0,summary TEXT);
        CREATE TABLE points(trip TEXT,seq INTEGER,wall INTEGER,motion TEXT,telemetry TEXT,PRIMARY KEY(trip,seq));
        CREATE TABLE models(key TEXT PRIMARY KEY,payload TEXT);''')
    t.execute('INSERT INTO trips VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
              ('synthetic-trip', 1000, 2000, 'finished', '{}', 1234.5, 234.5, 20.25, 1.75, 300, 15, '{}'))
    for i in range(2):
        t.execute('INSERT INTO points VALUES(?,?,?,?,?)', ('synthetic-trip', i, 1000 + i, '{"source":"test"}', '{"current":-2.5}'))
    t.execute('INSERT INTO models VALUES(?,?)', (MODEL_KEY, MODEL))
    for db in (h, t):
        db.commit()
    p = Path(root) / 'shared_prefs'
    p.mkdir()
    (p / 'settings.xml').write_text('<?xml version="1.0" encoding="utf-8"?><map><int name="cells" value="16"/><string name="write_mode">auto</string><string name="recovery_marker">合成設定・保持確認</string></map>', encoding='utf-8')
    (p / 'trip_profile.xml').write_text('<?xml version="1.0" encoding="utf-8"?><map><string name="last_model_key">' + MODEL_KEY + '</string><string name="reserve">12.5</string><int name="revision" value="7"/><boolean name="mounted" value="false"/></map>', encoding='utf-8')
    f = Path(root) / 'files'
    f.mkdir()
    (f / 'synthetic-note.txt').write_text('合成補助ファイル\n', encoding='utf-8')
    if wal:
        return h, t  # Keep open so the tests exercise real, uncheckpointed WAL data.
    for db in (h, t):
        db.close()
    return ()
