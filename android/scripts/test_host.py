#!/usr/bin/env python3
"""Run pure-Java tests plus the production TripProfile against limited host API stubs.
No SDK, emulator, Bluetooth, Android UI, or SQLite validation is claimed.
"""
import pathlib, shutil, subprocess
root=pathlib.Path(__file__).resolve().parents[1]
java=shutil.which('java')
if not java: raise SystemExit('JDK 17 required')
out=root/'build'/'host-tests';out.mkdir(parents=True,exist_ok=True)
core=sorted((root/'app/src/main/java').rglob('core/*.java'))
profile=root/'app/src/main/java/jp/k4159191/gtbms/prototype/TripProfile.java'
sources=core+sorted((root/'tests').glob('*.java'))+sorted((root/'tests/host').rglob('*.java'))+[profile]
subprocess.run([java,'-m','jdk.compiler/com.sun.tools.javac.Main','--release','8','-encoding','UTF-8','-d',str(out),*[str(s) for s in sources]],cwd=root,check=True)
for test in ('CoreTests','TripCoreTests','jp.k4159191.gtbms.prototype.ProfileMigrationTests'):
    subprocess.run([java,'-cp',str(out),test],cwd=root,check=True)
subprocess.run([java,'-m','jdk.compiler/com.sun.tools.javac.Main','-d',str(out),str(root/'tests/tools/ParseSources.java')],cwd=root,check=True)
app_sources=sorted((root/'app/src').rglob('*.java'))
subprocess.run([java,'-cp',str(out),'ParseSources',*[str(s) for s in app_sources]],cwd=root,check=True)
