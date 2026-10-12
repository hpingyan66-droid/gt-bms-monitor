#!/usr/bin/env python3
"""Destructive fixture seeding is ONLY allowed on an empty disposable emulator.

This test is not part of the user's migration kit and never accepts a phone.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate
from fixtures import seed


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--adb', required=True)
    p.add_argument('--original-apk', required=True, type=Path)
    p.add_argument('--recovery-apk', required=True, type=Path)
    p.add_argument('--tests-apk', required=True, type=Path)
    p.add_argument('--work', required=True, type=Path)
    p.add_argument('--manifest', required=True, type=Path)
    args = p.parse_args()
    adb = migrate.Adb(args.adb, 'emulator-5554')
    if adb.shell('getprop', 'ro.kernel.qemu') != '1':
        raise RuntimeError('Refusing to seed anything except an Android emulator')
    if adb.installed(migrate.OLD) or adb.installed(migrate.NEW):
        raise RuntimeError('Use a disposable emulator with neither app installed')
    args.work.mkdir(parents=True, exist_ok=False)
    adb.run('install', str(args.original_apk))
    connections = seed(args.work / 'synthetic-data', wal=True)
    try:
        migrate.make_tar(args.work / 'synthetic-data', args.work / 'synthetic-data.tar')
    finally:
        for db in connections:
            db.close()
    with (args.work / 'synthetic-data.tar').open('rb') as data:
        adb.run('shell', '-T', shlex.join(['run-as', migrate.OLD, 'tar', '-xf', '-']), stdin=data)
    result = migrate.migrate(argparse.Namespace(adb=args.adb, serial='emulator-5554',
        manifest=args.manifest, apk=args.recovery_apk, backup_dir=args.work / 'migration'))
    assert result['original_unchanged'] and result['all_restored_files_match']
    adb.run('install', str(args.tests_apk))
    output = adb.shell('am', 'instrument', '-w',
        'jp.k4159191.gtbms.recovery.tests/jp.k4159191.gtbms.prototype.RecoveryDataTests')
    (args.work / 'instrumentation.txt').write_text(output, encoding='utf-8')
    print(output, flush=True)
    if 'PASS:' not in output or 'FAIL:' in output:
        raise AssertionError('Recovery runtime tests did not pass')
    adb.shell('am', 'force-stop', migrate.NEW)
    adb.snapshot(migrate.NEW, args.work / 'target-before-retry.tar')
    before = migrate.unpack(args.work / 'target-before-retry.tar', args.work / 'target-before-retry')
    try:
        migrate.migrate(argparse.Namespace(adb=args.adb, serial='emulator-5554',
            manifest=args.manifest, apk=args.recovery_apk, backup_dir=args.work / 'retry'))
    except ValueError as e:
        if 'already has data' not in str(e):
            raise
    else:
        raise AssertionError('Nonempty destination was not rejected')
    adb.snapshot(migrate.NEW, args.work / 'target-after-retry.tar')
    assert migrate.unpack(args.work / 'target-after-retry.tar', args.work / 'target-after-retry') == before
    # Same-key Android reinstallation is an update continuity check, using only test data.
    adb.run('install', '-r', str(args.recovery_apk))
    adb.snapshot(migrate.NEW, args.work / 'target-after-update.tar')
    assert migrate.unpack(args.work / 'target-after-update.tar', args.work / 'target-after-update') == before
    adb.snapshot(migrate.OLD, args.work / 'original-final.tar')
    original_final = migrate.unpack(args.work / 'original-final.tar', args.work / 'original-final')
    original_start = migrate.unpack(args.work / 'migration/original-data.tar', args.work / 'original-start')
    assert original_final == original_start
    evidence = {'result': 'PASS', 'android_api': adb.shell('getprop', 'ro.build.version.sdk'),
        'device': 'AOSP ATD software emulator', 'synthetic_data_only': True,
        'migration': 'database/WAL, trip/model, preferences and files preserved',
        'runtime': output, 'nonempty_target_refused': True, 'rejected_retry_preserved_target': True,
        'same_key_reinstall_preserved_data': True, 'original_files_unchanged_throughout': True,
        'physical_bms_tested': False, 'windows_usb_tested': False}
    (args.work / 'e2e-result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('PASS: full Android migration, refusal, same-key reinstall, and original preservation', flush=True)


if __name__ == '__main__':
    main()
