"""Opt-in LAN read-only viewer. No BLE, database, admin API or arbitrary proxy."""
from __future__ import annotations

import argparse
import asyncio
import copy
import html
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import time
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

import psutil
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .editions import Edition, EditionError, load_edition
from .models import parse_time, protection_labels

PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
STATIC = Path(__file__).parent / 'static'
DEFAULT_SETTINGS = Path('mobile.local.json')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
MAX_RESPONSE = 1_048_576
REFRESH_INTERVAL_SECONDS = 0.5
MIN_REFRESH_DELAY_SECONDS = 0.05


@dataclass
class MobileSettings:
    host: str
    prefix_length: int = 24
    port: int = 8766
    main_port: int = 8765
    token: str = ''

    @property
    def network(self):
        return ipaddress.ip_network(f'{self.host}/{self.prefix_length}', strict=False)

    def validate(self):
        address = ipaddress.IPv4Address(self.host)
        if not any(address in network for network in PRIVATE):
            raise ValueError('A specific RFC1918 IPv4 address is required')
        for name in ('prefix_length', 'port', 'main_port'):
            if type(getattr(self, name)) is not int:
                raise ValueError(f'{name} must be an integer')
        if not 0 <= self.prefix_length <= 32 or not any(self.network.subnet_of(n) for n in PRIVATE):
            raise ValueError('The permitted subnet must stay within RFC1918')
        if self.prefix_length < 31 and address in (self.network.network_address, self.network.broadcast_address):
            raise ValueError('A host address is required')
        if not 1024 <= self.port <= 65535 or not 1024 <= self.main_port <= 65535 or self.port == self.main_port:
            raise ValueError('Use distinct, unprivileged viewer and monitor ports')
        if not isinstance(self.token, str) or not TOKEN.fullmatch(self.token):
            raise ValueError('A generated 256-bit connection key is required')
        return self

    def require_local_interface(self):
        addresses = {item.address: item.netmask for items in psutil.net_if_addrs().values()
                     for item in items if item.family == socket.AF_INET}
        if self.host not in addresses:
            raise ValueError('Configured IPv4 is not assigned to this Surface; run init again')
        if self.network != ipaddress.ip_network(f'{self.host}/{addresses[self.host]}', strict=False):
            raise ValueError('Configured subnet does not match the local interface')

    @classmethod
    def load(cls, path=DEFAULT_SETTINGS):
        return cls(**json.loads(Path(path).read_text(encoding='utf-8-sig'))).validate()


def atomic_private_write(path: Path, text: str):
    path = Path(path)
    temp = None
    try:
        with NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                prefix='.mobile-', suffix='.tmp', delete=False) as stream:
            temp = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def connection_page(settings: MobileSettings, path=Path('mobile-connect.local.html')):
    """Only a local file contains the pairing key; never an HTTP response."""
    settings.validate()
    base = f'http://{settings.host}:{settings.port}/'
    link = base + '#key=' + settings.token
    qr = '<p>QRを生成するには requirements-mobile.txt を導入してください。URLと接続キーでも接続できます。</p>'
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
        qr = qrcode.make(link, image_factory=SvgPathImage, border=4).to_string(encoding='unicode')
    except ImportError:
        pass
    document = f'''<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>GT Battery · スマホ接続</title><style>
body{{font-family:system-ui,sans-serif;background:#101819;color:#edf5f1;max-width:680px;margin:32px auto;padding:24px;line-height:1.8}}
.qr{{background:white;padding:20px;width:min(78vw,330px);margin:24px 0}}svg{{width:100%;height:auto;display:block}}
a{{color:#c4f66b;overflow-wrap:anywhere}}code{{display:block;overflow-wrap:anywhere;background:#21302c;padding:12px}}
</style><h1>スマホでバッテリーを見る</h1><p>スマホをSurfaceが案内する同じWi-Fiへ接続し、停車中にカメラでQRを読み取ってください。</p>
<div class="qr">{qr}</div><p><a href="{html.escape(link, quote=True)}" rel="noreferrer">この端末で閲覧画面を開く</a></p>
<p>QRを使わない場合のURL：<a href="{base}">{base}</a><br>接続キー：</p><code>{settings.token}</code>
<p>このファイルとQRは接続キーを含みます。共有・クラウド保存しないでください。閲覧は同じWi-Fi内だけで、BMS記録はSurfaceが続けます。</p>
<p>スマホの画面ロック中は表示更新が止まります。必要に応じて停車中に自動ロック設定を調整してください。</p></html>'''
    atomic_private_write(Path(path), document)
    return Path(path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_local_status(port: int):
    # Ignore HTTP_PROXY and forbid redirects: this is not a general-purpose proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f'http://127.0.0.1:{port}/api/status', timeout=3) as response:
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError('Status response exceeds limit')
        return json.loads(data, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON')))


def number(value, minimum=-1e6, maximum=1e6, nullable=False):
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError('Invalid numeric status')
    return value


def project_snapshot(source: dict, *, wall_time=None):
    """Strict allowlist. Nothing from GPS, trips, identity, settings or logs."""
    if not isinstance(source, dict) or source.get('mode') not in ('ble', 'mock'):
        raise ValueError('Invalid upstream mode')
    poll = number(source.get('poll_seconds'), 1, 10)
    ttl = max(6, poll * 3)
    state = source.get('state')
    if state not in ('connected', 'connecting', 'reconnecting', 'stopped') or type(source.get('fresh')) is not bool:
        raise ValueError('Invalid upstream state')
    received = source.get('last_success_at')
    if received is not None:
        received = parse_time(received).isoformat()
    age = number(source.get('age_seconds'), 0, 1e12, nullable=True)
    result = dict(schema=1, mode=source['mode'], state=state, fresh=False,
                  received_at=received, age_seconds=age, poll_seconds=poll,
                  stale_after_seconds=ttl, measurement=None, prediction=None, alerts=[])
    if not source['fresh'] or state != 'connected' or source.get('measurement') is None:
        if state == 'connected':
            result['state'] = 'stale'
        return result
    if received is None or age is None:
        raise ValueError('Fresh status missing measurement time')
    wall_age = (time.time() if wall_time is None else wall_time) - parse_time(received).timestamp()
    if wall_age < -5:
        raise ValueError('Measurement time is in the future')
    result['age_seconds'] = age = max(age, wall_age)
    if age > ttl:
        result['state'] = 'stale'
        return result
    sample = source['measurement']
    if not isinstance(sample, dict) or parse_time(sample['received_at']).isoformat() != received:
        raise ValueError('Measurement timestamp mismatch')
    code = sample.get('protection_code')
    if type(code) is not int or not 0 <= code <= 65535:
        raise ValueError('Invalid protection status')
    result['alerts'] = protection_labels(code)
    flags = sample.get('quality_flags')
    if not isinstance(flags, list):
        raise ValueError('Missing data quality')
    if flags:
        result.update(state='invalid', alerts=result['alerts'] + ['測定品質の確認が必要です'])
        return result
    values = {name: number(sample.get(name), *limits, nullable=name in ('current_a', 'power_w'))
              for name, limits in {'voltage_v': (0, 200), 'raw_current_a': (-327.68, 327.67),
                  'current_a': (-327.68, 327.68), 'power_w': (-100000, 100000),
                  'soc_pct': (0, 100), 'remaining_ah': (0, 1000), 'cell_delta_v': (0, 5)}.items()}
    for name, bounds, maximum in (('cell_voltages_v', (0, 5), 32), ('temperatures_c', (-60, 150), 16)):
        values[name] = sample.get(name)
        if not isinstance(values[name], list) or len(values[name]) > maximum:
            raise ValueError('Invalid sensor list')
        values[name] = [number(item, *bounds) for item in values[name]]
    for name in ('charge_mos', 'discharge_mos'):
        if type(sample.get(name)) is not bool:
            raise ValueError('Invalid MOS status')
        values[name] = sample[name]
    direction = sample.get('current_direction')
    if direction not in ('unconfirmed', 'charge_positive', 'discharge_positive'):
        raise ValueError('Invalid current direction')
    values.update(current_direction=direction, quality_flags=[], alarms=result['alerts'])
    if direction == 'unconfirmed':
        values.update(current_a=None, power_w=None)
    prediction = source.get('prediction')
    projected_prediction = None
    if isinstance(prediction, dict) and direction != 'unconfirmed':
        projected_prediction = {name: number(prediction.get(name), 0, 100000, nullable=True)
                                for name in ('remaining_range_km', 'safe_remaining_range_km', 'full_range_km')}
        # Do not relay arbitrary strings from upstream data.
        reliability = prediction.get('reliability')
        projected_prediction['reliability'] = reliability if reliability in ('低', '中', '高', 'データ不足') else '参考値'
    result.update(fresh=True, measurement=values, prediction=projected_prediction)
    return result


class SnapshotFeed:
    """One shared local polling loop, independent of viewer count/auth failures."""
    def __init__(self, port: int, *, fetcher=None, clock=time.monotonic):
        self.port, self.fetcher, self.clock = port, fetcher or fetch_local_status, clock
        self.snapshot = dict(schema=1, mode='unknown', state='surface_offline', fresh=False,
                             received_at=None, age_seconds=None, poll_seconds=2,
                             stale_after_seconds=6, measurement=None, prediction=None, alerts=[])
        self.updated = None
        self.completed = None
        self.upstream_rtt_ms = None

    async def refresh(self):
        started = self.clock()
        try:
            source = await asyncio.to_thread(self.fetcher, self.port)
            fetched = self.clock()
            snapshot = project_snapshot(source)
            self.snapshot, self.updated = snapshot, started
            self.completed = self.clock()
            self.upstream_rtt_ms = self.milliseconds(fetched - started)
        except Exception:
            # Never retain exception strings, private upstream payloads or old values.
            self.snapshot.update(state='surface_offline', fresh=False, measurement=None, prediction=None, alerts=[])
            self.completed = self.upstream_rtt_ms = None

    async def run(self):
        while True:
            started = self.clock()
            await self.refresh()
            # One request at a time; subtract its duration without a catch-up burst.
            elapsed = max(0, self.clock() - started)
            await asyncio.sleep(max(MIN_REFRESH_DELAY_SECONDS, REFRESH_INTERVAL_SECONDS - elapsed))

    @staticmethod
    def milliseconds(seconds):
        value = seconds * 1000
        return max(0, value) if math.isfinite(value) else None

    def view(self):
        result = copy.deepcopy(self.snapshot)
        now = self.clock()
        elapsed = max(0, now - self.updated) if self.updated is not None else 0
        # Delivery timings are generated here, never copied from upstream data.
        # Freshness still counts from request start, including the full RTT.
        result['delivery'] = dict(
            relay_age_ms=self.milliseconds(now - self.completed) if self.completed is not None else None,
            upstream_rtt_ms=self.upstream_rtt_ms,
            refresh_interval_ms=REFRESH_INTERVAL_SECONDS * 1000)
        if result['age_seconds'] is not None:
            result['age_seconds'] += elapsed
        if elapsed > 6 or (result['age_seconds'] is not None and result['age_seconds'] > result['stale_after_seconds']):
            result.update(state='stale' if result['state'] == 'connected' else result['state'],
                          fresh=False, measurement=None, prediction=None, alerts=[])
        return result


def create_mobile_app(settings: MobileSettings, *, feed=None, edition: Edition | None = None):
    edition = load_edition() if edition is None else edition
    if not isinstance(edition, Edition):
        raise TypeError('edition must be an Edition')
    if not edition.can_manage_network:
        raise EditionError('edition_network_restricted')
    settings.validate()
    feed = feed or SnapshotFeed(settings.main_port)
    origin = f'http://{settings.host}:{settings.port}'

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(feed.run())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.feed = feed

    @app.middleware('http')
    async def protect(request: Request, call_next):
        try:
            peer = ipaddress.ip_address(request.client.host)
            allowed_peer = peer in settings.network
        except (ValueError, AttributeError):
            allowed_peer = False
        host_ok = request.headers.get('host') == f'{settings.host}:{settings.port}'
        origin_ok = request.headers.get('origin') in (None, origin)
        if not allowed_peer or not host_ok or not origin_ok:
            response = JSONResponse({'detail': 'Local network access only'}, status_code=403)
        elif request.method != 'GET':
            response = JSONResponse({'detail': 'Read only'}, status_code=405)
        elif request.url.path == '/api/view' and not secrets.compare_digest(
                request.headers.get('authorization', '').encode('utf-8'), ('Bearer ' + settings.token).encode('ascii')):
            response = JSONResponse({'detail': 'Connection key required'}, status_code=401)
        else:
            response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                                 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get('/')
    async def index():
        return FileResponse(STATIC/'mobile.html')

    @app.get('/static/mobile.css')
    async def css():
        return FileResponse(STATIC/'mobile.css')

    @app.get('/static/mobile.js')
    async def js():
        return FileResponse(STATIC/'mobile.js')

    @app.get('/api/view')
    async def view():
        return feed.view()

    return app


def main():
    parser = argparse.ArgumentParser(description='GT BMS opt-in local network read-only viewer')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--host', required=True)
    init.add_argument('--prefix-length', required=True, type=int)
    init.add_argument('--port', default=8766, type=int)
    init.add_argument('--main-port', default=8765, type=int)
    init.add_argument('--rotate-key', action='store_true')
    sub.add_parser('share')
    sub.add_parser('serve')
    args = parser.parse_args()
    try:
        edition = load_edition()
    except EditionError:
        parser.error('application_edition_invalid')
    if not edition.can_manage_network:
        parser.error('edition_network_restricted')
    try:
        if args.command == 'init':
            old = MobileSettings.load() if DEFAULT_SETTINGS.exists() else None
            token = old.token if old and not args.rotate_key else secrets.token_urlsafe(32)
            settings = MobileSettings(args.host, args.prefix_length, args.port, args.main_port, token).validate()
            settings.require_local_interface()
            atomic_private_write(DEFAULT_SETTINGS, json.dumps(asdict(settings), indent=2))
            connection_page(settings)
            print('Created mobile.local.json and mobile-connect.local.html. Connection key is shown only in the local HTML.')
        else:
            settings = MobileSettings.load()
            settings.require_local_interface()
            if args.command == 'share':
                connection_page(settings)
                print('Updated mobile-connect.local.html')
            else:
                import uvicorn
                uvicorn.run(create_mobile_app(settings, edition=edition), host=settings.host, port=settings.port,
                            access_log=False, log_level='warning', proxy_headers=False,
                            workers=1, limit_concurrency=32, ws='none')
    except (ValueError, OSError) as exc:
        # Do not echo malformed configuration contents or any credentials.
        print(f'Mobile setup failed: {type(exc).__name__}. Check local IPv4/subnet, ports and mobile.local.json.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
