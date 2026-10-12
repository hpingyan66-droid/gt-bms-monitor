#!/usr/bin/env python3
"""Build a dependency-free debug APK using installed Android SDK tools + JDK 17.
Does not install SDKs or change system settings. Also runs host-side core tests.
"""
import argparse, hashlib, os, pathlib, shutil, subprocess, zipfile, xml.etree.ElementTree as ET

root=pathlib.Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--android-jar',required=True,type=pathlib.Path)
parser.add_argument('--build-tools',required=True,type=pathlib.Path)
parser.add_argument('--keystore',required=True,type=pathlib.Path,help='Existing original debug keystore used for 0.2.0; never regenerated')
args=parser.parse_args()
key=args.keystore.resolve()
if not key.is_file(): raise SystemExit('Original signing keystore is required for an in-place update. No replacement key was created.')
java=shutil.which('java')
if not java: raise SystemExit('JDK 17 is required')
cert=subprocess.run([java,'-m','java.base/sun.security.tools.keytool.Main','-exportcert','-keystore',str(key),'-storepass','android','-alias','androiddebugkey'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
if cert.returncode: raise SystemExit('Could not read the original debug signing certificate; no APK built.')
if hashlib.sha256(cert.stdout).hexdigest()!='3436a806f9a3f8d1eec53e1a35378db5a5a07d5229aabf32d312ea028af745da':
    raise SystemExit('Signing certificate differs from 0.2.0; an in-place update is not possible with this key.')
out=root/'build'/'manual';out.mkdir(parents=True,exist_ok=True)
for name in ('tests','generated','classes','dex'):
    path=out/name
    if path.exists(): shutil.rmtree(path)
sdkjar=args.android_jar.resolve();bt=args.build_tools.resolve()
def run(*cmd):
    print('RUN',str(cmd[0]),flush=True)
    subprocess.run([str(x) for x in cmd],cwd=root,check=True)
def tool(name):return bt/(name+('.bat' if os.name=='nt' and name in ['d8','apksigner'] else '.exe' if os.name=='nt' else ''))
core=sorted((root/'app/src/main/java').rglob('core/*.java'))
tests=out/'tests';tests.mkdir(exist_ok=True)
run(java,'-m','jdk.compiler/com.sun.tools.javac.Main','--release','8','-encoding','UTF-8','-d',tests,*core,*sorted((root/'tests').glob('*.java')))
run(java,'-cp',tests,'CoreTests')
run(java,'-cp',tests,'TripCoreTests')
gen=out/'generated';gen.mkdir(exist_ok=True)
classes=out/'classes';classes.mkdir(exist_ok=True)
run(tool('aapt2'),'compile','--dir',root/'app/src/main/res','-o',out/'resources.zip')
ET.register_namespace('android','http://schemas.android.com/apk/res/android')
manifest=ET.parse(root/'app/src/main/AndroidManifest.xml');manifest.getroot().set('package','jp.k4159191.gtbms.prototype')
manifest.getroot().find('application').set('{http://schemas.android.com/apk/res/android}debuggable','true')
manifest.write(out/'AndroidManifest.xml',encoding='utf-8',xml_declaration=True)
run(tool('aapt2'),'link','-o',out/'resources.apk','-I',sdkjar,'--manifest',out/'AndroidManifest.xml','--java',gen,'-A',root/'app/src/main/assets',out/'resources.zip')
sources=sorted((root/'app/src/main/java').rglob('*.java'))+sorted(gen.rglob('*.java'))
bootclasspath=str(sdkjar)+os.pathsep+str(bt/'core-lambda-stubs.jar')
run(java,'-m','jdk.compiler/com.sun.tools.javac.Main','-encoding','UTF-8','-source','8','-target','8','-bootclasspath',bootclasspath,'-d',classes,*sources)
jar=out/'classes.jar'
with zipfile.ZipFile(jar,'w') as z:
    for f in classes.rglob('*.class'):z.write(f,f.relative_to(classes))
dex=out/'dex';dex.mkdir(exist_ok=True)
run(java,'-cp',bt/'lib/d8.jar','com.android.tools.r8.D8','--min-api','26','--lib',sdkjar,'--output',dex,jar)
unsigned=out/'unsigned.apk';shutil.copy2(out/'resources.apk',unsigned)
with zipfile.ZipFile(unsigned,'a',zipfile.ZIP_DEFLATED) as z:
    for f in dex.glob('*.dex'):z.write(f,f.name)
run(tool('zipalign'),'-f','4',unsigned,out/'aligned.apk')
version=manifest.getroot().get('{http://schemas.android.com/apk/res/android}versionName').replace('-prototype','')
apk=out/('GT-BMS-Android-'+version+'-debug.apk')
run(tool('apksigner'),'sign','--ks',key,'--ks-key-alias','androiddebugkey','--ks-pass','pass:android','--key-pass','pass:android','--out',apk,out/'aligned.apk')
run(tool('apksigner'),'verify','--verbose',apk)
run(tool('aapt2'),'dump','badging',apk)
print('BUILT',apk)
