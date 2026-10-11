"""Development-only watchdog check. Terminates only this workspace's watchdog child."""
import json
from pathlib import Path
import time
import urllib.request
import psutil

root=Path(__file__).resolve().parent.parent
expected_exe=(root/'.venv/Scripts/python.exe').resolve()
matches=[]
for process in psutil.process_iter(['pid','exe','cmdline','cwd']):
    try:
        info=process.info
        if info['exe'] and Path(info['exe']).resolve()==expected_exe and info['cwd'] and Path(info['cwd']).resolve()==root and 'gtbms.cli' in (info['cmdline'] or []) and 'serve' in info['cmdline']:
            parent=process.parent()
            if parent and any('Watchdog.ps1' in arg for arg in parent.cmdline()):
                matches.append(process)
    except (psutil.AccessDenied,psutil.NoSuchProcess):pass
if len(matches)!=1:
    raise SystemExit('Expected exactly one application child of this workspace Watchdog; no process terminated')
process=matches[0]
with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=5) as response:
    health=json.load(response)
if not health['ok'] or health['mode']!='mock':
    raise SystemExit('Only a healthy mock test server may be terminated')
record={'terminated_pid':process.pid,'terminated_at_unix':time.time(),'source':'owned_watchdog_mock_test'}
process.terminate()
process.wait(timeout=10)
Path('artifacts').mkdir(exist_ok=True)
Path('artifacts/watchdog-crash.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps(record))
