import argparse
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate
from fixtures import seed


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, entries):
        path = self.root / 'input.tar'
        with tarfile.open(path, 'w') as tar:
            for name, kind, content in entries:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.SYMTYPE:
                    info.linkname = '/tmp/elsewhere'
                info.size = len(content) if kind == tarfile.REGTYPE else 0
                tar.addfile(info, io.BytesIO(content) if info.size else None)
        return path

    def test_wal_roundtrip_preserves_rows_preferences_files_and_raw_backup(self):
        src = self.root / 'phone'
        opened = seed(src, wal=True)
        try:
            self.assertGreater((src / 'databases/gtbms-android.db-wal').stat().st_size, 0)
            archive = self.root / 'original.tar'
            migrate.make_tar(src, archive)
            original_sha = migrate.sha256(archive)
            original_files = migrate.unpack(archive, self.root / 'raw')
            self.assertEqual(original_files, migrate.file_manifest(src))
            report = migrate.normalize(self.root / 'raw', self.root / 'normalized')
            self.assertEqual(report['gtbms-android.db']['samples']['rows'], 3)
            self.assertEqual(report['gtbms-trips.db']['models']['rows'], 1)
            self.assertEqual(report['gtbms-trips.db']['points']['rows'], 2)
            self.assertEqual(report['gtbms-trips.db']['trips']['rows'], 1)
            self.assertFalse(list((self.root / 'normalized/databases').glob('*-wal')))
            self.assertFalse(list((self.root / 'normalized/databases').glob('*-shm')))
            for prefix in ('shared_prefs/', 'files/'):
                for name, sha in original_files.items():
                    if name.startswith(prefix):
                        self.assertEqual(migrate.sha256(self.root / 'normalized' / name), sha)
            restored = self.root / 'restored.tar'
            migrate.make_tar(self.root / 'normalized', restored)
            readback = migrate.unpack(restored, self.root / 'readback')
            self.assertEqual(readback, migrate.file_manifest(self.root / 'normalized'))
            self.assertEqual(migrate.sha256(archive), original_sha)
            self.assertEqual(migrate.file_manifest(src), original_files)
        finally:
            for db in opened:
                db.close()

    def test_rejects_traversal(self):
        archive = self.archive([('files/../../outside', tarfile.REGTYPE, b'bad')])
        with self.assertRaises(ValueError):
            migrate.unpack(archive, self.root / 'out')
        self.assertFalse((self.root / 'outside').exists())

    def test_rejects_links(self):
        archive = self.archive([('files/link', tarfile.SYMTYPE, b'')])
        with self.assertRaises(ValueError):
            migrate.unpack(archive, self.root / 'out')

    def test_rejects_windows_device_names(self):
        archive = self.archive([('files/CON.txt', tarfile.REGTYPE, b'bad')])
        with self.assertRaises(ValueError):
            migrate.unpack(archive, self.root / 'out')

    def test_rejects_duplicate_files(self):
        archive = self.archive([('files/a', tarfile.REGTYPE, b'a'), ('files/a', tarfile.REGTYPE, b'b')])
        with self.assertRaises(ValueError):
            migrate.unpack(archive, self.root / 'out')

    def test_rejects_out_of_scope_top_level(self):
        archive = self.archive([('cache/anything', tarfile.REGTYPE, b'a')])
        with self.assertRaises(ValueError):
            migrate.unpack(archive, self.root / 'out')

    def test_rejects_oversized_archive_before_writing(self):
        archive = self.root / 'oversized.tar'
        info = tarfile.TarInfo('files/too-big')
        info.size = migrate.MAX_BYTES + 1
        archive.write_bytes(info.tobuf() + b'\0' * 1024)
        with self.assertRaises(ValueError):
            migrate.unpack(archive, self.root / 'out')

    def test_rejects_unexpected_schema(self):
        seed(self.root / 'raw')
        with closing(sqlite3.connect(self.root / 'raw/databases/gtbms-android.db')) as db:
            db.execute('CREATE TABLE unexpected(value TEXT)')
        with self.assertRaisesRegex(ValueError, 'tables'):
            migrate.normalize(self.root / 'raw', self.root / 'normalized')

    def test_rejects_corrupt_database(self):
        seed(self.root / 'raw')
        (self.root / 'raw/databases/gtbms-android.db').write_bytes(b'corrupt')
        with self.assertRaises(sqlite3.DatabaseError):
            migrate.normalize(self.root / 'raw', self.root / 'normalized')

    def test_rejects_orphan_wal(self):
        (self.root / 'raw/databases').mkdir(parents=True)
        (self.root / 'raw/databases/gtbms-android.db-wal').write_bytes(b'wal')
        with self.assertRaisesRegex(ValueError, 'without'):
            migrate.normalize(self.root / 'raw', self.root / 'normalized')

    def test_refuses_nonempty_target_without_writing_to_phone(self):
        archive = self.archive([('shared_prefs/settings.xml', tarfile.REGTYPE, b'<map/>')])
        class ReadOnlyPhone:
            def snapshot(self, package, target):
                self.package = package
                target.write_bytes(archive.read_bytes())
        phone = ReadOnlyPhone()
        with self.assertRaisesRegex(ValueError, 'already has data'):
            migrate.require_empty(phone, self.root, 'target')
        self.assertEqual(phone.package, migrate.NEW)

    def test_wrong_apk_hash_stops_before_adb(self):
        apk = self.root / 'app.apk'
        apk.write_bytes(b'wrong')
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'package': migrate.NEW, 'apk_sha256': '0' * 64}))
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            migrate.migrate(argparse.Namespace(apk=apk, manifest=manifest))

    def test_wrong_package_stops_before_adb(self):
        apk = self.root / 'app.apk'
        apk.write_bytes(b'data')
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'package': migrate.OLD, 'apk_sha256': migrate.sha256(apk)}))
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            migrate.migrate(argparse.Namespace(apk=apk, manifest=manifest))

    def test_install_detection_requires_exact_package(self):
        adb = object.__new__(migrate.Adb)
        adb.shell = lambda *args: ''
        self.assertFalse(adb.installed(migrate.NEW))
        adb.shell = lambda *args: 'package:' + migrate.NEW + '.tests'
        self.assertFalse(adb.installed(migrate.NEW))
        adb.shell = lambda *args: 'package:' + migrate.NEW + '\npackage:' + migrate.NEW + '.tests'
        self.assertTrue(adb.installed(migrate.NEW))


if __name__ == '__main__':
    unittest.main()
