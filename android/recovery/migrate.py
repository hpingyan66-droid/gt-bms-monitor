#!/usr/bin/env python3
"""Copy the original debug app's data to an EMPTY, separate recovery app.

No root, uninstall, clear-data, writes to the original app, or CSV reconstruction.
The original process is stopped to get a consistent database/WAL snapshot.
Python 3.9+ and Android platform-tools (adb) are required on the user's computer.
"""
import argparse
import base64
from contextlib import closing
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET

OLD = 'jp.k4159191.gtbms.prototype'
NEW = 'jp.k4159191.gtbms.recovery'
ORIGINAL_APKS = {
    'bbb39f42bc8c8ad83ff3d18baa3683b1e5b41011b351560868a29c6d16d519a0': '0.2.0',
    '5e4ba410769e911a6e66fcbfbaa3a35773823b9d43aebfb1588b0fbbfe514e38': '0.1.1',
}
TOP = {'databases', 'shared_prefs', 'files'}
WINDOWS_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {x + str(n) for x in ('COM', 'LPT') for n in range(1, 10)}
MAX_BYTES = 2 * 1024**3
SCHEMAS = {
    'gtbms-android.db': {
        'samples': [('id', 'INTEGER', 1), ('received_at', 'TEXT', 0), ('source', 'TEXT', 0),
                    ('session', 'TEXT', 0), ('device_key', 'TEXT', 0), ('payload', 'TEXT', 0)],
    },
    'gtbms-trips.db': {
        'trips': [('id', 'TEXT', 1), ('started', 'INTEGER', 0), ('ended', 'INTEGER', 0),
                  ('state', 'TEXT', 0), ('profile', 'TEXT', 0), ('meters', 'REAL', 0),
                  ('estimated', 'REAL', 0), ('wh', 'REAL', 0), ('regen', 'REAL', 0),
                  ('covered', 'REAL', 0), ('uncovered', 'REAL', 0), ('summary', 'TEXT', 0)],
        'points': [('trip', 'TEXT', 1), ('seq', 'INTEGER', 2), ('wall', 'INTEGER', 0),
                   ('motion', 'TEXT', 0), ('telemetry', 'TEXT', 0)],
        'models': [('key', 'TEXT', 1), ('payload', 'TEXT', 0)],
    },
}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def file_manifest(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob('*')) if p.is_file()}


def unpack(archive, destination):
    """Extract only app data, without tarfile.extract or following any links."""
    destination.mkdir(parents=True, exist_ok=False)
    if archive.stat().st_size == 0:
        return {}
    seen, total = set(), 0
    with tarfile.open(archive, 'r:') as tar:
        for n, m in enumerate(tar):
            name = m.name.rstrip('/')
            p = PurePosixPath(name)
            if (n >= 50000 or not name or p.is_absolute() or '..' in p.parts or
                    str(p) != name or p.parts[0] not in TOP or '\\' in name or
                    any(':' in x or x.endswith(('.', ' ')) or x.split('.')[0].upper() in WINDOWS_RESERVED for x in p.parts) or name in seen):
                raise ValueError('Unsafe or duplicate backup entry: ' + name)
            if not (m.isfile() or m.isdir()):
                raise ValueError('Links/special files are not supported: ' + name)
            if m.isfile() and len(p.parts) < 2:
                raise ValueError('Invalid top-level file')
            seen.add(name)
            total += m.size
            if m.size < 0 or total > MAX_BYTES:
                raise ValueError('Backup exceeds the 2 GiB safety limit')
            target = destination.joinpath(*p.parts)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
    return file_manifest(destination)


def validate_db(db, expected):
    db.execute('PRAGMA query_only=ON')
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('SQLite integrity check failed')
    if db.execute('PRAGMA user_version').fetchone()[0] != 1:
        raise ValueError('Unsupported database schema version')
    objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    if any(t not in ('table', 'index') for t, _ in objects):
        raise ValueError('Unexpected database trigger/view')
    if {name for t, name in objects if t == 'table'} != set(expected):
        raise ValueError('Unexpected database tables')
    result = {}
    for table, columns in expected.items():
        actual = [(r[1], r[2].upper(), r[5]) for r in db.execute('PRAGMA table_info(' + table + ')')]
        if actual != columns:
            raise ValueError('Unexpected columns in ' + table)
        h, count = hashlib.sha256(), 0
        for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid'):
            values = [{'blob': base64.b64encode(v).decode()} if isinstance(v, bytes) else v for v in row]
            h.update((json.dumps(values, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
            count += 1
        result[table] = {'rows': count, 'content_sha256': h.hexdigest()}
    return result


def normalize(raw, normalized):
    """Consolidate WAL using SQLite backup on a LOCAL copy, never on the phone."""
    normalized.mkdir(exist_ok=False)
    for directory in ('shared_prefs', 'files'):
        if (raw / directory).exists():
            shutil.copytree(raw / directory, normalized / directory)
    for p in (normalized / 'shared_prefs').rglob('*') if (normalized / 'shared_prefs').exists() else []:
        if p.is_file():
            if p.suffix not in ('.xml', '.bak') or ET.parse(p).getroot().tag != 'map':
                raise ValueError('Unsupported preferences file: ' + p.name)
    reports = {}
    dbdir = raw / 'databases'
    if dbdir.exists():
        allowed = {name + suffix for name in SCHEMAS for suffix in ('', '-wal', '-shm', '-journal')}
        if any(p.name not in allowed or not p.is_file() for p in dbdir.iterdir()):
            raise ValueError('Unexpected database files; raw backup preserved')
        for p in dbdir.iterdir():
            base = next((name for name in SCHEMAS if p.name.startswith(name)), None)
            if base and not (dbdir / base).is_file():
                raise ValueError('Database sidecar without its main database')
        (normalized / 'databases').mkdir()
        for name, expected in SCHEMAS.items():
            source = dbdir / name
            if not source.exists():
                continue
            uri = 'file:' + urllib.parse.quote(source.resolve().as_posix(), safe='/:') + '?mode=rw'
            with closing(sqlite3.connect(uri, uri=True)) as db:
                before = validate_db(db, expected)
                with closing(sqlite3.connect(normalized / 'databases' / name)) as dest:
                    db.backup(dest)
                    after = validate_db(dest, expected)
                    if before != after:
                        raise ValueError('SQLite backup content mismatch')
                reports[name] = after
    return reports


def make_tar(root, output):
    with tarfile.open(output, 'w', format=tarfile.USTAR_FORMAT) as tar:
        for p in sorted(root.rglob('*')):
            info = tar.gettarinfo(str(p), p.relative_to(root).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o700 if p.is_dir() else 0o600
            if p.is_file():
                with p.open('rb') as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)


class Adb:
    def __init__(self, executable, serial=None):
        self.executable = executable
        out = subprocess.run([executable, 'devices'], check=True, capture_output=True, timeout=20).stdout.decode()
        entries = [x.split()[:2] for x in out.splitlines()[1:] if x.strip()]
        if serial:
            if [serial, 'device'] not in entries:
                raise ValueError('Selected device is not authorized/online')
        elif len(entries) == 1 and entries[0][1] == 'device':
            serial = entries[0][0]
        else:
            raise ValueError('Connect exactly one authorized phone (or use --serial)')
        self.prefix = [executable, '-s', serial]

    def run(self, *args, stdin=None, output=None):
        result = subprocess.run(self.prefix + list(args), stdin=stdin,
                                stdout=output or subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if result.returncode:
            raise RuntimeError('ADB failed: ' + result.stderr.decode(errors='replace').strip())
        return result.stdout or b''

    def shell(self, *args):
        return self.run('shell', '-T', shlex.join(args)).decode(errors='replace').strip()

    def installed(self, package):
        # Android pm path returns exit code 1 for an absent app. list packages
        # succeeds with an empty result; compare exactly, since its filter is a substring.
        return 'package:' + package in self.shell('pm', 'list', 'packages', package).splitlines()

    def snapshot(self, package, output):
        script = ('set --; for d in databases shared_prefs files; do '
                  'if [ -d "$d" ]; then set -- "$@" "$d"; fi; done; '
                  'if [ "$#" -gt 0 ]; then tar -cf - "$@"; fi')
        with output.open('xb') as f:
            self.run('exec-out', shlex.join(['run-as', package, 'sh', '-c', script]), output=f)


def require_empty(adb, work, label):
    target = work / (label + '.tar')
    adb.snapshot(NEW, target)
    if unpack(target, work / label):
        raise ValueError('Recovery app already has data. Nothing was overwritten. Do not clear or uninstall it.')


def migrate(args):
    bundle = json.loads(args.manifest.read_text(encoding='utf-8'))
    if bundle.get('package') != NEW or sha256(args.apk) != bundle.get('apk_sha256'):
        raise ValueError('Recovery APK/manifest mismatch')
    adb = Adb(str(args.adb), args.serial)
    if not adb.installed(OLD):
        raise ValueError('Original app is not installed; no changes made')
    args.backup_dir.mkdir(parents=True, exist_ok=False)
    work = args.backup_dir
    print('Backup folder: ' + str(work.resolve()), flush=True)
    if adb.installed(NEW):
        require_empty(adb, work, 'target-before')
        # An unknown app must never receive the user's private data.
        paths = adb.shell('pm', 'path', NEW).splitlines()
        if len(paths) != 1 or not paths[0].startswith('package:/data/app/'):
            raise ValueError('Unsupported recovery APK installation')
        with (work / 'existing-recovery.apk').open('xb') as out:
            adb.run('exec-out', shlex.join(['cat', paths[0][8:]]), output=out)
        if sha256(work / 'existing-recovery.apk') != bundle['apk_sha256']:
            raise ValueError('Existing recovery app differs from this tested bundle')
    paths = adb.shell('pm', 'path', OLD).splitlines()
    if len(paths) != 1 or not re.fullmatch(r'package:/data/app/[A-Za-z0-9_./=+~-]+\.apk', paths[0]):
        raise ValueError('Unsupported original APK installation')
    with (work / 'original.apk').open('xb') as out:
        adb.run('exec-out', shlex.join(['cat', paths[0][8:]]), output=out)
    original_hash = sha256(work / 'original.apk')
    if original_hash not in ORIGINAL_APKS:
        raise ValueError('Original APK is not a verified 0.1.1/0.2.0 build; no app data changed')
    adb.shell('am', 'force-stop', OLD)
    print('Original app stopped. Copying database/WAL and settings...', flush=True)
    raw_tar = work / 'original-data.tar'
    adb.snapshot(OLD, raw_tar)
    original_files = unpack(raw_tar, work / 'raw-copy')
    if not original_files:
        raise ValueError('Original app data is empty; backup retained, migration stopped')
    reports = normalize(work / 'raw-copy', work / 'restore-copy')
    expected = file_manifest(work / 'restore-copy')
    make_tar(work / 'restore-copy', work / 'restore-data.tar')
    if not adb.installed(NEW):
        print('Installing separate recovery app...', flush=True)
        adb.run('install', str(args.apk.resolve()))  # deliberately no -r / downgrade
    adb.shell('am', 'force-stop', NEW)
    require_empty(adb, work, 'target-final-empty-check')
    print('Copying into the empty recovery app...', flush=True)
    with (work / 'restore-data.tar').open('rb') as data:
        adb.run('shell', '-T', shlex.join(['run-as', NEW, 'tar', '-xf', '-']), stdin=data)
    adb.snapshot(NEW, work / 'recovery-readback.tar')
    actual = unpack(work / 'recovery-readback.tar', work / 'readback')
    if actual != expected:
        raise ValueError('Recovery readback mismatch. Original app and backup are retained.')
    adb.snapshot(OLD, work / 'original-after.tar')
    original_after = unpack(work / 'original-after.tar', work / 'original-after')
    if original_after != original_files:
        raise ValueError('Original data changed during migration. Keep both apps and backups; do not proceed.')
    receipt = {'result': 'PASS', 'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'original_version': ORIGINAL_APKS[original_hash], 'original_apk_sha256': original_hash,
               'recovery_apk_sha256': bundle['apk_sha256'], 'original_unchanged': True,
               'all_restored_files_match': True, 'original_data_tar_sha256': sha256(raw_tar),
               'tables': reports, 'files': expected,
               'permissions': 'Grant location, nearby devices and notifications in the recovery app.'}
    (work / 'migration-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('PASS: database rows, learning data, settings and files copied and verified.', flush=True)
    print('Original app/data unchanged. Open GT BMS 復旧版 and grant its permissions.', flush=True)
    print('Do not run both apps against the BMS at the same time.', flush=True)
    return receipt


def main():
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--adb', type=Path, default=Path(shutil.which('adb') or str(here / 'platform-tools' / ('adb.exe' if os.name == 'nt' else 'adb'))))
    p.add_argument('--serial')
    p.add_argument('--apk', type=Path, default=here / 'GT-BMS-Android-0.2.1-recovery.1.apk')
    p.add_argument('--manifest', type=Path, default=here / 'release-manifest.json')
    p.add_argument('--backup-dir', type=Path, default=here / ('phone-backup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')))
    args = p.parse_args()
    try:
        migrate(args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, tarfile.TarError, ET.ParseError, subprocess.SubprocessError) as e:
        print('STOP: ' + str(e), file=sys.stderr)
        print('Keep both apps and any backup folder. Nothing is automatically erased.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
