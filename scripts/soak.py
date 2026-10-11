"""Accelerated synthetic day simulation. Does NOT replace a hardware soak test."""
import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import time

import psutil

from gtbms.analysis import energy_summary
from gtbms.config import Config
from gtbms.models import stamp, utcnow
from gtbms.storage import Store
from gtbms.transport import MockTransport


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--hours',type=float,default=24)
    parser.add_argument('--output',type=Path,default=Path('artifacts/soak'))
    args=parser.parse_args()
    if not 0 < args.hours <= 168:
        parser.error('hours must be >0 and <=168')
    args.output.mkdir(parents=True,exist_ok=True)
    config=Config(data_dir=str(args.output/'data'), backup_dir=str(args.output/'backups'),mock_disconnect_every=500)
    store=Store(config)
    transport=MockTransport(config)
    await transport.connect()
    start=utcnow()-timedelta(hours=args.hours)
    wall=time.monotonic()
    count=int(args.hours*3600/config.poll_seconds)
    saved,disconnects=0,0
    process=psutil.Process()
    peak=process.memory_info().rss
    try:
        for k in range(count):
            try:
                s=await transport.read()
            except ConnectionError:
                disconnects+=1
                store.gap(stamp(start+timedelta(seconds=(k-1)*2)),stamp(start+timedelta(seconds=(k+1)*2)),'synthetic_disconnect')
                await transport.close();await transport.connect()
                continue
            s.received_at=stamp(start+timedelta(seconds=k*2))
            s.monotonic_s=1000+k*2
            store.save_sample(s)
            saved+=1
            if k%5000==0:
                peak=max(peak,process.memory_info().rss)
                print(f'{k}/{count} synthetic samples',flush=True)
        backup=store.backup()
        result={'source':'synthetic_accelerated','simulated_hours':args.hours,'wall_seconds':time.monotonic()-wall,
                'attempted':count,'saved':saved,'synthetic_disconnects':disconnects,'integrity':store.integrity(),
                'peak_memory_mb':peak/1048576,'database_bytes':store.path.stat().st_size,
                'backup':backup.name,'hardware_soak_verified':False}
        (args.output/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2),flush=True)
    finally:
        await transport.close();store.close()


if __name__=='__main__':asyncio.run(main())
