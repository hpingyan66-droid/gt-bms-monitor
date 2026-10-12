#!/usr/bin/env python3
"""Run a disposable Android CI test with an ephemeral TEST-ONLY signing key.

No production/recovery signing key is needed, changed or uploaded. CI APKs are
never published. The public release fingerprint and build signing guard remain
unchanged. The original APK is an immutable, verified regression fixture.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[2]
sdk = Path(os.environ['ANDROID_HOME'])
bt = sdk / 'build-tools/35.0.0'
work = Path(tempfile.mkdtemp(prefix='gtbms-ci-', dir=os.environ.get('RUNNER_TEMP')))
adb = str(sdk / 'platform-tools/adb')


def run(*cmd, **kwargs):
    return subprocess.run([str(x) for x in cmd], check=True, **kwargs)


run(sys.executable, root / 'recovery/build_recovery.py', '--android-jar', sdk / 'platforms/android-35/android.jar',
    '--build-tools', bt, '--unsigned', '--tests', '--output', work / 'unsigned.apk')
result = max((root / 'build/recovery').glob('*/build-result.json'), key=lambda p: p.stat().st_mtime)
built = Path(json.loads(result.read_text())['build_directory'])
# Explicit ephemeral test setup; not a fallback in the distribution builder.
run('keytool', '-genkeypair', '-keystore', work / 'ci-test.p12', '-storetype', 'PKCS12',
    '-alias', 'ci-test-only', '-storepass', 'android', '-keypass', 'android', '-keyalg', 'RSA',
    '-keysize', '2048', '-validity', '2', '-dname', 'CN=Disposable CI test only')
signer = ['java', '-jar', str(bt / 'lib/apksigner.jar')]
for src, dst in [(work / 'unsigned.apk', work / 'recovery.apk'),
                 (built / 'recovery-tests-unsigned.apk', work / 'tests.apk')]:
    run(*signer, 'sign', '--ks', work / 'ci-test.p12', '--ks-key-alias', 'ci-test-only',
        '--ks-pass', 'pass:android', '--out', dst, src)
    run(*signer, 'verify', dst)
(work / 'test-manifest.json').write_text(json.dumps({'package': 'jp.k4159191.gtbms.recovery',
    'apk_sha256': hashlib.sha256((work / 'recovery.apk').read_bytes()).hexdigest()}))
run(sdk / 'cmdline-tools/latest/bin/avdmanager', 'create', 'avd', '--name', 'gtbms_recovery_ci',
    '--package', 'system-images;android-35;aosp_atd;x86_64', input=b'no\n')
run(adb, 'start-server')
with (work / 'emulator.log').open('w') as log:
    emu = subprocess.Popen([str(sdk / 'emulator/emulator'), '-avd', 'gtbms_recovery_ci', '-no-window',
        '-no-audio', '-no-boot-anim', '-no-snapshot', '-gpu', 'swiftshader_indirect', '-cores', '2',
        '-memory', '2048', '-port', '5554', '-no-metrics'], stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(90):
            if emu.poll() is not None:
                raise RuntimeError('Emulator exited before boot')
            state = subprocess.run([adb, '-s', 'emulator-5554', 'shell', 'getprop', 'sys.boot_completed'],
                                   capture_output=True, timeout=15)
            if state.stdout.strip() == b'1':
                break
            time.sleep(3)
        else:
            raise RuntimeError('Emulator boot timeout')
        run(sys.executable, root / 'recovery/tests/run_emulator.py', '--adb', adb,
            '--original-apk', root / 'recovery/tests/fixtures/GT-BMS-Android-0.2.0-debug.apk',
            '--recovery-apk', work / 'recovery.apk', '--tests-apk', work / 'tests.apk',
            '--manifest', work / 'test-manifest.json', '--work', work / 'e2e', timeout=240)
        print((work / 'e2e/e2e-result.json').read_text(), flush=True)
    except BaseException:
        print((work / 'emulator.log').read_text()[-16000:], flush=True)
        raise
    finally:
        emu.terminate()
        try:
            emu.wait(timeout=15)
        except subprocess.TimeoutExpired:
            emu.kill()
            emu.wait()
