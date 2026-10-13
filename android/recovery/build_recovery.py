#!/usr/bin/env python3
"""Build a separately installed recovery app; never generate or replace a key.

The original app/package and its signing guard are unchanged. With --unsigned,
this is a compile-only CI check. Signed outputs require the pinned certificate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

PACKAGE = 'jp.k4159191.gtbms.recovery'
NAMESPACE = 'jp.k4159191.gtbms.prototype'
ALIAS = 'gtbms-recovery'
VERSION = '0.2.1-recovery.1'
ANDROID = '{http://schemas.android.com/apk/res/android}'


def build(args):
    root = Path(__file__).resolve().parents[1]
    # Python file operations and child tools must use the same output location,
    # even though the child tools run from the Android project directory.
    args.output = args.output.resolve()
    java = shutil.which('java')
    if not java:
        raise RuntimeError('JDK 17 is required')
    bt, sdk = args.build_tools.resolve(), args.android_jar.resolve()
    aapt = bt / ('aapt2.exe' if os.name == 'nt' else 'aapt2')
    align = bt / ('zipalign.exe' if os.name == 'nt' else 'zipalign')
    for p in [sdk, aapt, align, bt / 'core-lambda-stubs.jar', bt / 'lib/d8.jar', bt / 'lib/apksigner.jar']:
        if not p.is_file():
            raise RuntimeError('Missing SDK component: ' + str(p))
    if args.output.exists():
        raise RuntimeError('Output already exists; choose a new filename')
    signer = [java, '-jar', str(bt / 'lib/apksigner.jar')]
    cert = None
    if not args.unsigned:
        if not args.keystore or not args.password_file:
            raise RuntimeError('Existing keystore and password file are required')
        pinned = json.loads((Path(__file__).parent / 'signing-certificate.json').read_text())
        result = subprocess.run([java, '-m', 'java.base/sun.security.tools.keytool.Main',
            '-exportcert', '-alias', ALIAS, '-keystore', str(args.keystore.resolve()),
            '-storepass:file', str(args.password_file.resolve())], capture_output=True, check=True)
        cert = hashlib.sha256(result.stdout).hexdigest()
        if cert != pinned['certificate_sha256']:
            raise RuntimeError('Recovery certificate mismatch; build stopped')
    parent = root / 'build/recovery'
    parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='build-', dir=parent))

    def run(*cmd):
        subprocess.run([str(x) for x in cmd], cwd=root, check=True)

    gen, classes, dex, test_classes, test_dex = [out / x for x in ('generated', 'classes', 'dex', 'test-classes', 'test-dex')]
    for d in (gen, classes, dex, test_classes, test_dex):
        d.mkdir()
    ET.register_namespace('android', ANDROID[1:-1])
    manifest = ET.parse(root / 'app/src/main/AndroidManifest.xml')
    m = manifest.getroot()
    m.set('package', PACKAGE)
    m.set(ANDROID + 'versionCode', '1')
    m.set(ANDROID + 'versionName', VERSION)
    app = m.find('application')
    app.set(ANDROID + 'label', 'GT BMS 復旧版')
    app.set(ANDROID + 'debuggable', 'true')
    for item in app:
        name = item.get(ANDROID + 'name', '')
        if name.startswith('.'):
            item.set(ANDROID + 'name', NAMESPACE + name)
    manifest.write(out / 'AndroidManifest.xml', encoding='utf-8', xml_declaration=True)
    run(aapt, 'compile', '--dir', root / 'app/src/main/res', '-o', out / 'resources.zip')
    run(aapt, 'link', '-o', out / 'unsigned.apk', '-I', sdk,
        '--manifest', out / 'AndroidManifest.xml', '--java', gen, '--custom-package', NAMESPACE,
        '-A', root / 'app/src/main/assets', out / 'resources.zip')
    compiler = [java, '-m', 'jdk.compiler/com.sun.tools.javac.Main', '-encoding', 'UTF-8',
        '-source', '8', '-target', '8', '-bootclasspath', str(sdk) + os.pathsep + str(bt / 'core-lambda-stubs.jar')]
    run(*compiler, '-d', classes, *sorted((root / 'app/src/main/java').rglob('*.java')), *sorted(gen.rglob('*.java')))
    with zipfile.ZipFile(out / 'classes.jar', 'w') as z:
        for p in sorted(classes.rglob('*.class')):
            z.write(p, p.relative_to(classes))
    run(java, '-cp', bt / 'lib/d8.jar', 'com.android.tools.r8.D8', '--min-api', '26',
        '--lib', sdk, '--output', dex, out / 'classes.jar')
    with zipfile.ZipFile(out / 'unsigned.apk', 'a', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(dex.glob('*.dex')):
            z.write(p, p.name)
    run(align, '4', out / 'unsigned.apk', out / 'aligned.apk')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.unsigned:
        shutil.copy2(out / 'aligned.apk', args.output)
    else:
        run(*signer, 'sign', '--ks', args.keystore.resolve(), '--ks-key-alias', ALIAS,
            '--ks-pass', 'file:' + str(args.password_file.resolve()),
            '--out', args.output, out / 'aligned.apk')
        run(*signer, 'verify', '--verbose', args.output)
    run(aapt, 'dump', 'badging', args.output)
    if args.tests:
        tm = ET.Element('manifest', {'package': PACKAGE + '.tests'})
        ET.SubElement(tm, 'uses-sdk', {ANDROID + 'minSdkVersion': '26', ANDROID + 'targetSdkVersion': '35'})
        ET.SubElement(tm, 'application', {ANDROID + 'debuggable': 'true', ANDROID + 'label': 'GT BMS Recovery Tests'})
        ET.SubElement(tm, 'instrumentation', {ANDROID + 'name': NAMESPACE + '.RecoveryDataTests', ANDROID + 'targetPackage': PACKAGE})
        ET.ElementTree(tm).write(out / 'test-manifest.xml', encoding='utf-8', xml_declaration=True)
        run(aapt, 'link', '-o', out / 'test-unsigned.apk', '-I', sdk, '--manifest', out / 'test-manifest.xml')
        run(*compiler, '-cp', classes, '-d', test_classes,
            *sorted((root / 'app/src/androidTest').rglob('*.java')),
            Path(__file__).parent / 'tests/RecoveryDataTests.java')
        with zipfile.ZipFile(out / 'test-classes.jar', 'w') as z:
            for p in sorted(test_classes.rglob('*.class')):
                z.write(p, p.relative_to(test_classes))
        run(java, '-cp', bt / 'lib/d8.jar', 'com.android.tools.r8.D8', '--min-api', '26',
            '--lib', sdk, '--classpath', out / 'classes.jar', '--output', test_dex, out / 'test-classes.jar')
        with zipfile.ZipFile(out / 'test-unsigned.apk', 'a', zipfile.ZIP_DEFLATED) as z:
            for p in sorted(test_dex.glob('*.dex')):
                z.write(p, p.name)
        run(align, '4', out / 'test-unsigned.apk', out / 'test-aligned.apk')
        if args.unsigned:
            shutil.copy2(out / 'test-aligned.apk', out / 'recovery-tests-unsigned.apk')
        else:
            run(*signer, 'sign', '--ks', args.keystore.resolve(), '--ks-key-alias', ALIAS,
                '--ks-pass', 'file:' + str(args.password_file.resolve()),
                '--out', out / 'recovery-tests.apk', out / 'test-aligned.apk')
            run(*signer, 'verify', out / 'recovery-tests.apk')
    result = {'package': PACKAGE, 'version': VERSION, 'apk': str(args.output.resolve()),
        'sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(), 'certificate_sha256': cert,
        'signed': not args.unsigned, 'build_directory': str(out)}
    (out / 'build-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--android-jar', required=True, type=Path)
    p.add_argument('--build-tools', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--unsigned', action='store_true')
    p.add_argument('--keystore', type=Path)
    p.add_argument('--password-file', type=Path)
    p.add_argument('--tests', action='store_true')
    build(p.parse_args())
