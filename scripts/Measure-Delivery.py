"""Read-only timing comparison on Surface. Does not measure the phone's Wi-Fi hop.

No adapter switching, BLE connection, BMS command, configuration write or raw
measurement export. Radio/route/motor labels are explicitly operator-declared.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import urllib.request


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * q) - 1))]


def summary(values):
    return dict(count=len(values), p50=percentile(values, .5), p95=percentile(values, .95),
                maximum=max(values) if values else None)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, key=None):
    started = time.monotonic()
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key} if key else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=3) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError('response size')
            value = json.loads(raw)
        state = value.get('state')
        if state not in ('connected', 'connecting', 'reconnecting', 'stopped', 'stale', 'invalid', 'surface_offline'):
            state = 'invalid'
        return dict(ok=True, state=state, fresh=value.get('fresh') is True,
                    received=value.get('received_at', value.get('last_success_at')),
                    done=time.monotonic(), rtt_ms=(time.monotonic() - started) * 1000)
    except Exception as exc:
        return dict(ok=False, error_type=type(exc).__name__, done=time.monotonic(),
                    rtt_ms=(time.monotonic() - started) * 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--interval', type=float, default=.25)
    parser.add_argument('--radio', choices=['internal', 'external', 'unknown'], default='unknown')
    parser.add_argument('--route', choices=['router', 'hotspot', 'bluetooth', 'unknown'], default='unknown')
    parser.add_argument('--motor', choices=['stopped', 'running', 'unknown'], default='unknown')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not (math.isfinite(args.seconds) and 5 <= args.seconds <= 120 and
            math.isfinite(args.interval) and .1 <= args.interval <= 2):
        parser.error('seconds must be 5..120 and interval .1..2')
    if args.output.exists():
        parser.error('output exists; select a new path to preserve comparison history')
    root = Path(__file__).resolve().parents[1]
    import sys
    sys.path.insert(0, str(root / 'src'))
    from gtbms.mobile import MobileSettings
    settings = MobileSettings.load(root / 'mobile.local.json')
    settings.require_local_interface()
    started_wall = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    observations = []
    seen = {'main': {}, 'viewer': {}}
    first_sample = {'main': None, 'viewer': None}
    last = {'main': None, 'viewer': None}
    intervals = {'main': [], 'viewer': []}
    with ThreadPoolExecutor(max_workers=2) as pool:
        while time.monotonic() - started < args.seconds:
            cycle = time.monotonic()
            futures = {
                'main': pool.submit(fetch, f'http://127.0.0.1:{settings.main_port}/api/status'),
                'viewer': pool.submit(fetch, f'http://{settings.host}:{settings.port}/api/view', settings.token),
            }
            item = {'elapsed_ms': (cycle - started) * 1000}
            for name, future in futures.items():
                result = future.result()
                sample = result.pop('received', None)
                done = result.pop('done')
                is_new = bool(result.get('fresh') and isinstance(sample, str) and sample not in seen[name])
                result['new_sample'] = is_new
                if is_new:
                    seen[name][sample] = done
                    if first_sample[name] is None:
                        first_sample[name] = sample
                    if last[name] is not None:
                        intervals[name].append((done - last[name]) * 1000)
                    last[name] = done
                item[name] = result
            observations.append(item)
            time.sleep(max(.01, args.interval - (time.monotonic() - cycle)))
    # Initial cached samples have unknown publication times, so exclude them.
    common = seen['main'].keys() & seen['viewer'].keys()
    eligible = [sample for sample in common if sample not in first_sample.values()]
    lags = [(seen['viewer'][sample] - seen['main'][sample]) * 1000 for sample in eligible]
    report = {
        'schema': 'gtbms.surface-delivery.v1', 'started_at_utc': started_wall,
        'finished_at_utc': datetime.now(timezone.utc).isoformat(),
        'labels': {'source': 'operator_declared', 'radio': args.radio, 'route': args.route, 'motor': args.motor},
        'scope': 'Surface local API observation; excludes phone radio hop and phone rendering',
        'sampling_interval_ms': args.interval * 1000,
        'interpretation': 'Observed first-appearance lag, not calibrated one-way latency. Scheduling and request duration add uncertainty; negatives are retained. Initial cache and unmatched samples excluded from lag.',
        'observed_relay_lag_ms': summary(lags),
        'main_new_samples': len(seen['main']), 'viewer_new_samples': len(seen['viewer']),
        'main_samples_not_seen_in_viewer': len(seen['main'].keys() - seen['viewer'].keys()),
        'note_unmatched': 'May include observation-window boundaries; not a BMS packet-loss rate.',
        'http_rtt_ms': {name: summary([row[name]['rtt_ms'] for row in observations if row[name]['ok']]) for name in seen},
        'observed_update_interval_ms': {name: summary(values) for name, values in intervals.items()},
        'errors': {name: sum(not row[name]['ok'] for row in observations) for name in seen},
        'observations': observations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({k: report[k] for k in ['schema', 'observed_relay_lag_ms', 'http_rtt_ms', 'errors']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
