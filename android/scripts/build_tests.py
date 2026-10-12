#!/usr/bin/env python3
"""Build the isolated instrumentation APK after build_local.py, using the same debug key."""
import argparse, pathlib, os, shutil, subprocess, zipfile
p=argparse.ArgumentParser()
p.add_argument('--android-jar',required=True,type=pathlib.Path)
p.add_argument('--build-tools',required=True,type=pathlib.Path)
p.add_argument('--keystore',required=True,type=pathlib.Path)
a=p.parse_args();root=pathlib.Path(__file__).resolve().parents[1];out=root/'build/manual'
bt=a.build_tools.resolve();sdk=a.android_jar.resolve();key=a.keystore.resolve()
java=shutil.which('java')
def run(*args):subprocess.run([str(x) for x in args],check=True)
def tool(name):return bt/(name+('.bat' if os.name=='nt' and name=='apksigner' else '.exe' if os.name=='nt' else ''))
manifest='''<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="jp.k4159191.gtbms.prototype.tests"><uses-sdk android:minSdkVersion="26" android:targetSdkVersion="35"/><application android:debuggable="true" android:label="GT BMS Tests"/><instrumentation android:name="jp.k4159191.gtbms.prototype.SmokeTests" android:targetPackage="jp.k4159191.gtbms.prototype"/><instrumentation android:name="jp.k4159191.gtbms.prototype.PermissionTests" android:targetPackage="jp.k4159191.gtbms.prototype"/><instrumentation android:name="jp.k4159191.gtbms.prototype.TripTests" android:targetPackage="jp.k4159191.gtbms.prototype"/></manifest>'''
(out/'test-manifest.xml').write_text(manifest)
classes=out/'test-classes';dex=out/'test-dex'
for d in (classes,dex):
 if d.exists():shutil.rmtree(d)
 d.mkdir()
run(tool('aapt2'),'link','-o',out/'test-res.apk','-I',sdk,'--manifest',out/'test-manifest.xml')
run(java,'-m','jdk.compiler/com.sun.tools.javac.Main','-source','8','-target','8','-encoding','UTF-8','-bootclasspath',str(sdk)+os.pathsep+str(bt/'core-lambda-stubs.jar'),'-cp',out/'classes','-d',classes,*sorted((root/'app/src/androidTest').rglob('*.java')))
with zipfile.ZipFile(out/'tests.jar','w') as z:
 for f in classes.rglob('*.class'):z.write(f,f.relative_to(classes))
run(java,'-cp',bt/'lib/d8.jar','com.android.tools.r8.D8','--min-api','26','--lib',sdk,'--classpath',out/'classes.jar','--output',dex,out/'tests.jar')
shutil.copy2(out/'test-res.apk',out/'test-unsigned.apk')
with zipfile.ZipFile(out/'test-unsigned.apk','a',zipfile.ZIP_DEFLATED) as z:
 for f in dex.glob('*.dex'):z.write(f,f.name)
run(tool('zipalign'),'-f','4',out/'test-unsigned.apk',out/'test-aligned.apk')
run(tool('apksigner'),'sign','--ks',key,'--ks-pass','pass:android','--out',out/'tests.apk',out/'test-aligned.apk')
print('BUILT',out/'tests.apk')
