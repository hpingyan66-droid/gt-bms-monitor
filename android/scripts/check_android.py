#!/usr/bin/env python3
"""Compile resources, Android app/test classes and DEX without generating a signing key.

The unsigned APK is a build check, not an installable update or a hardware test.
All output lives in a new temporary directory under build/android-check.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--android-jar', required=True, type=Path)
    parser.add_argument('--build-tools', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    java = shutil.which('java')
    if not java:
        raise SystemExit('JDK 17 is required')
    sdk = args.android_jar.resolve()
    tools = args.build_tools.resolve()
    aapt = tools / ('aapt2.exe' if os.name == 'nt' else 'aapt2')
    for item in (sdk, aapt, tools / 'core-lambda-stubs.jar', tools / 'lib/d8.jar'):
        if not item.is_file():
            raise SystemExit('Required SDK file is missing: ' + str(item))
    parent = root / 'build/android-check'
    parent.mkdir(parents=True, exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix='run-', dir=parent))

    def run(*cmd):
        subprocess.run([str(x) for x in cmd], cwd=root, check=True)

    gen, classes, dex, tests = [out / name for name in ('generated', 'classes', 'dex', 'test-classes')]
    for folder in (gen, classes, dex, tests):
        folder.mkdir()
    ET.register_namespace('android', 'http://schemas.android.com/apk/res/android')
    manifest = ET.parse(root / 'app/src/main/AndroidManifest.xml')
    manifest.getroot().set('package', 'jp.k4159191.gtbms.prototype')
    manifest.write(out / 'AndroidManifest.xml', encoding='utf-8', xml_declaration=True)
    run(aapt, 'compile', '--dir', root / 'app/src/main/res', '-o', out / 'resources.zip')
    run(aapt, 'link', '-o', out / 'unsigned.apk', '-I', sdk,
        '--manifest', out / 'AndroidManifest.xml', '--java', gen,
        '-A', root / 'app/src/main/assets', out / 'resources.zip')
    compiler = [java, '-m', 'jdk.compiler/com.sun.tools.javac.Main',
                '-encoding', 'UTF-8', '-source', '8', '-target', '8',
                '-bootclasspath', str(sdk) + os.pathsep + str(tools / 'core-lambda-stubs.jar')]
    sources = sorted((root / 'app/src/main/java').rglob('*.java')) + sorted(gen.rglob('*.java'))
    run(*compiler, '-d', classes, *sources)
    run(*compiler, '-cp', classes, '-d', tests,
        *sorted((root / 'app/src/androidTest').rglob('*.java')))
    jar = out / 'classes.jar'
    with zipfile.ZipFile(jar, 'w') as archive:
        for item in sorted(classes.rglob('*.class')):
            archive.write(item, item.relative_to(classes))
    run(java, '-cp', tools / 'lib/d8.jar', 'com.android.tools.r8.D8',
        '--min-api', '26', '--lib', sdk, '--output', dex, jar)
    with zipfile.ZipFile(out / 'unsigned.apk', 'a', zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(dex.glob('*.dex')):
            archive.write(item, item.name)
    run(aapt, 'dump', 'badging', out / 'unsigned.apk')
    print('PASS: Android app/test API compilation, resources and DEX. Unsigned build only; no runtime or hardware validation.')


if __name__ == '__main__':
    main()
