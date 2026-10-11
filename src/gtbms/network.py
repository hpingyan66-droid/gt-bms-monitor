"""Loopback-only network mode manager. Owns only its viewer, never the BMS."""
from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import ipaddress
import json
import os
import re
from pathlib import Path
import secrets
import socket
import time
from typing import Literal
import urllib.request
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .editions import Edition, load_edition
from .mobile import MobileSettings, NoRedirect, connection_page, create_mobile_app

MODES = ('mobile_router', 'home_router', 'local_area')
MANAGER_PORT = 8767
MAX_BODY = 16_384
STATIC = Path(__file__).parent / 'static'
Mode = Literal['mobile_router', 'home_router', 'local_area']
# The Windows boundary emits the transition kind only while its recorded
# Off-to-On adapter observation still matches the live hotspot and adapter.
LOCAL_ADAPTER_KINDS = frozenset(('wifi_direct_virtual_adapter', 'hotspot_transition_adapter'))
OPTIONAL_HOTSPOT_ERRORS = frozenset(('hotspot_no_upstream_profile', 'hotspot_api_unavailable', 'hotspot_status_unavailable'))
INVENTORY_ERRORS = OPTIONAL_HOTSPOT_ERRORS | frozenset(('adapter_inventory_unavailable',
    'network_category_inventory_unavailable', 'wlan_inventory_unavailable', 'ipv4_inventory_unavailable'))


class NetworkError(Exception):
    """Only fixed application codes, never platform exception messages."""
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def error_code(error, fallback):
    from .network_platform import NetworkPlatformError
    return error.code if isinstance(error, (NetworkError, NetworkPlatformError)) else fallback


def essential_errors(inventory):
    return [code for code in inventory['errors'] if code not in OPTIONAL_HOTSPOT_ERRORS]


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def guid(value):
    return str(UUID(value.strip('{}')))


class RouterProfile(Input):
    wlan_profile: str = Field(min_length=1, max_length=255)
    interface_id: str = Field(min_length=1, max_length=64)

    @field_validator('wlan_profile')
    @classmethod
    def valid_name(cls, value):
        if any(ord(char) < 32 for char in value):
            raise ValueError('invalid_profile')
        return value

    @field_validator('interface_id')
    @classmethod
    def valid_guid(cls, value):
        return guid(value)


class Profiles(Input):
    mobile_router: RouterProfile | None = None
    home_router: RouterProfile | None = None
    local_area: Input = Field(default_factory=Input)


class NetworkSettings(Input):
    profiles: Profiles = Field(default_factory=Profiles)
    active_mode: Literal['unknown', 'mobile_router', 'home_router', 'local_area'] = 'unknown'


class SaveProfile(RouterProfile):
    mode: Literal['mobile_router', 'home_router']


class Switch(Input):
    mode: Mode


class PrivateFiles:
    """Atomic local settings and credential-bearing backups under ignored logs/."""
    NAMES = ('network.local.json', 'mobile.local.json', 'mobile-connect.local.html')

    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, name):
        if name not in self.NAMES:
            raise NetworkError('invalid_private_path')
        return self.root / name

    @staticmethod
    def atomic(path, content):
        path = Path(path)
        temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self, names=NAMES):
        return {name: self.path(name).read_bytes() if self.path(name).exists() else None for name in names}

    def backup(self, snapshot):
        directory = self.root / 'logs' / 'network-backups' / uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        for name, content in snapshot.items():
            self.path(name)
            if content is not None:
                self.atomic(directory / name, content)
        self.atomic(directory / 'manifest.json', json.dumps(
            {'present': [name for name, content in snapshot.items() if content is not None]},
            separators=(',', ':')).encode())

    def restore(self, snapshot):
        for name, content in snapshot.items():
            if content is None:
                self.path(name).unlink(missing_ok=True)
            else:
                self.atomic(self.path(name), content)

    def save_network(self, settings):
        self.atomic(self.path('network.local.json'), settings.model_dump_json(indent=2).encode())

    def load_network(self):
        path = self.path('network.local.json')
        try:
            return NetworkSettings.model_validate_json(path.read_bytes()) if path.exists() else NetworkSettings()
        except Exception:
            raise NetworkError('network_configuration_invalid') from None

    def load_mobile(self):
        path = self.path('mobile.local.json')
        try:
            return MobileSettings.load(path) if path.exists() else None
        except Exception:
            raise NetworkError('viewer_configuration_invalid') from None


def empty_inventory():
    return dict(wifi_profiles=[], wifi_interfaces=[], addresses=[], hotspot=dict(state='unknown', ssid=None), errors=[])


def clean_inventory(source):
    """Return only known local inventory fields; never relay credentials/errors."""
    if not isinstance(source, dict):
        raise NetworkError('inventory_invalid')
    result = empty_inventory()
    try:
        for item in source.get('wifi_profiles', []):
            profile = RouterProfile(wlan_profile=item['name'], interface_id=item['interface_id'])
            result['wifi_profiles'].append(dict(name=profile.wlan_profile, interface_id=profile.interface_id,
                                                interface_name=str(item.get('interface_name', ''))[:256]))
        for item in source.get('wifi_interfaces', []):
            state = item.get('state')
            result['wifi_interfaces'].append(dict(interface_id=guid(item['interface_id']),
                interface_name=str(item.get('interface_name', ''))[:256],
                state=state if state in ('connected', 'disconnected', 'not_ready', 'associating',
                                         'discovering', 'authenticating', 'disconnecting') else 'unknown',
                current_profile=item.get('current_profile') if isinstance(item.get('current_profile'), str) else None))
        for item in source.get('addresses', []):
            address = ipaddress.IPv4Address(item['ip_address'])
            prefix, index = item['prefix_length'], item['interface_index']
            if type(prefix) is not int or not 0 <= prefix <= 32 or type(index) is not int or index < 1:
                raise ValueError
            result['addresses'].append(dict(ip_address=str(address), prefix_length=prefix, interface_index=index,
                interface_alias=str(item.get('interface_alias', ''))[:256], adapter_guid=guid(item['adapter_guid']),
                network_category=item.get('network_category') if item.get('network_category') in ('Private', 'Public') else 'Unknown',
                adapter_status='Up' if item.get('adapter_status') == 'Up' else 'Down',
                address_state='Preferred' if item.get('address_state') == 'Preferred' else 'Other',
                skip_as_source=item.get('skip_as_source', False) is not False,
                candidate_kind=item.get('candidate_kind') if item.get('candidate_kind') in LOCAL_ADAPTER_KINDS else 'private_ipv4'))
        hotspot = source.get('hotspot', {})
        result['hotspot'] = dict(state=hotspot.get('state') if hotspot.get('state') in ('on', 'off') else 'unknown',
                                ssid=hotspot.get('ssid') if isinstance(hotspot.get('ssid'), str) else None)
        for code in source.get('errors', []):
            known = isinstance(code, str) and (code in INVENTORY_ERRORS or
                re.fullmatch(r'wlan_(?:interfaces|current_connection|profiles|open)_[0-9]{1,10}', code))
            safe = code if known else 'inventory_incomplete'
            if safe not in result['errors']:
                result['errors'].append(safe)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise NetworkError('inventory_invalid') from None
    return result


async def main_health(port):
    def read():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=3) as response:
            raw = response.read(4097)
            return len(raw) <= 4096 and json.loads(raw).get('ok') is True
    try:
        return await asyncio.to_thread(read)
    except Exception:
        return False


class OwnedViewer:
    """Only a socket/server created by this object can be stopped here."""
    def __init__(self):
        self.server = self.task = self.socket = self.settings = None

    @property
    def running(self):
        return self.task is not None and not self.task.done() and self.server.started

    async def start(self, settings):
        import uvicorn
        if self.running:
            raise NetworkError('viewer_already_running')
        settings.require_local_interface()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((settings.host, settings.port))
            listener.listen(128)
            listener.setblocking(False)
        except OSError:
            listener.close()
            raise NetworkError('viewer_port_in_use', 409) from None

        class NestedServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                yield  # The outer loopback manager owns process signal handling.

        try:
            self.socket = listener
            self.settings = settings
            self.server = NestedServer(uvicorn.Config(create_mobile_app(settings), host=settings.host,
                port=settings.port, access_log=False, log_level='warning', proxy_headers=False,
                workers=1, ws='none', timeout_graceful_shutdown=3))
            self.task = asyncio.create_task(self.server.serve(sockets=[listener]))
            deadline = time.monotonic() + 5
            while not self.server.started:
                if self.task.done() or time.monotonic() >= deadline:
                    raise NetworkError('viewer_start_failed')
                await asyncio.sleep(.01)
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except (TimeoutError, asyncio.CancelledError):
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            except Exception:
                pass
        if self.socket is not None:
            self.socket.close()
        self.server = self.task = self.socket = self.settings = None


MESSAGES = {
    'idle': '切替待ちです。', 'validate': '保存済みの接続先を確認しています。',
    'check_main': 'BMS監視プログラムの動作を確認しています。',
    'stop_viewer': 'スマホ閲覧を一時停止しています。BMS記録は続きます。',
    'stop_hotspot': 'ホットスポットを停止しています。', 'connect': '保存済みWi-Fiへ接続しています。',
    'start_hotspot': 'ローカルエリア用のホットスポットを開始しています。',
    'inspect': '接続先とIPアドレスの確定を待っています。',
    'firewall': '限定した受信規則を確認しています。必要な場合はWindowsの管理者承認に応答してください。',
    'start_viewer': 'スマホ閲覧を開始しています。', 'commit': '成功した接続モードを保存しています。',
    'rollback': '以前の閲覧設定の復旧を試みています。', 'complete': '接続モードを切り替えました。',
    'failed': '切替に失敗しました。以前の設定を保持しています。',
    'recovery_required': '切替に失敗しました。ネットワークの状態を確認して復旧してください。',
}


class NetworkManager:
    def __init__(self, root, *, platform=None, viewer=None, health_check=None,
                 connect_timeout=45, poll_interval=.5, clock=time.monotonic, sleep=asyncio.sleep):
        if platform is None:
            from .network_platform import NetworkPlatform
            platform = NetworkPlatform(root)
        self.platform, self.viewer = platform, viewer or OwnedViewer()
        self.files = PrivateFiles(root)
        self.settings = self.files.load_network()
        self.inventory = empty_inventory()
        self.inventory_valid = False
        self.inventory_checked_at = None
        self.last_error = None
        self.active_verified = False
        self.external_viewer = False
        self.mobile_settings = None
        self.task = None
        self.lock = asyncio.Lock()
        self.health_check = health_check or main_health
        self.connect_timeout, self.poll_interval = connect_timeout, poll_interval
        self.clock, self.sleep = clock, sleep
        self.operation = dict(state='idle', phase='idle', message=MESSAGES['idle'], mode=None)

    def phase(self, phase, state='running'):
        self.operation.update(state=state, phase=phase, message=MESSAGES[phase])

    async def inspect(self):
        self.inventory_valid = False
        try:
            self.inventory = clean_inventory(await self.platform.inspect())
        except Exception as exc:
            raise NetworkError(error_code(exc, 'inventory_failed')) from None
        self.inventory_checked_at = datetime.now(timezone.utc).isoformat()
        self.inventory_valid = True
        return self.inventory

    async def start(self):
        try:
            await self.inspect()
            self.mobile_settings = self.files.load_mobile()
            if self.mobile_settings is not None:
                if self.mobile_settings.port != 8766 or self.mobile_settings.main_port != 8765:
                    raise NetworkError('viewer_ports_invalid')
                if not await self.health_check(self.mobile_settings.main_port):
                    raise NetworkError('main_unavailable')
                if self.settings.active_mode != 'unknown':
                    selected = self.select_address(self.settings.active_mode, self.profile(self.settings.active_mode))
                    if selected is None or (selected['ip_address'], selected['prefix_length']) != (
                            self.mobile_settings.host, self.mobile_settings.prefix_length):
                        raise NetworkError('previous_network_unavailable')
                    self.active_verified = True
                await self.viewer.start(self.mobile_settings)
        except NetworkError as exc:
            self.external_viewer = exc.code == 'viewer_port_in_use'
            self.last_error = exc.code
        except Exception as exc:
            self.last_error = error_code(exc, 'manager_start_failed')

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.viewer.stop()

    def state(self):
        running = self.viewer.running
        settings = self.mobile_settings
        mode = self.settings.active_mode
        selected = self.select_address(mode, self.profile(mode)) if mode in MODES else None
        current_matches = (self.inventory_valid and selected is not None and settings is not None and
            (selected['ip_address'], selected['prefix_length']) == (settings.host, settings.prefix_length))
        return dict(profiles=self.settings.profiles.model_dump(), inventory=copy.deepcopy(self.inventory),
            inventory_checked_at=self.inventory_checked_at,
            active_mode=mode if self.active_verified and current_matches else 'unknown',
            viewer=dict(state='running' if running else 'external' if self.external_viewer else 'stopped',
                        url=f'http://{settings.host}:{settings.port}/' if running and settings else None),
            operation=copy.deepcopy(self.operation), last_error=self.last_error)

    def profile(self, mode):
        return getattr(self.settings.profiles, mode) if mode != 'local_area' else None

    def profile_available(self, profile):
        return profile is not None and any(item['name'] == profile.wlan_profile and
            item['interface_id'] == profile.interface_id for item in self.inventory['wifi_profiles'])

    async def save_profile(self, request):
        if self.lock.locked():
            raise NetworkError('operation_busy', 409)
        async with self.lock:
            await self.inspect()
            profile = RouterProfile(wlan_profile=request.wlan_profile, interface_id=request.interface_id)
            if essential_errors(self.inventory) or not self.profile_available(profile):
                raise NetworkError('profile_not_available')
            updated = self.settings.model_copy(deep=True)
            setattr(updated.profiles, request.mode, profile)
            try:
                self.files.backup(self.files.snapshot(('network.local.json',)))
                self.files.save_network(updated)
            except Exception:
                raise NetworkError('configuration_write_failed') from None
            self.settings = updated

    async def refresh(self):
        if self.lock.locked():
            raise NetworkError('operation_busy', 409)
        async with self.lock:
            await self.inspect()

    async def hotspot_settings(self):
        if self.lock.locked():
            raise NetworkError('operation_busy', 409)
        async with self.lock:
            try:
                await self.platform.open_hotspot_settings()
            except Exception as exc:
                raise NetworkError(error_code(exc, 'hotspot_settings_failed')) from None

    def select_address(self, mode, profile=None, *, inventory=None):
        inventory = self.inventory if inventory is None else inventory
        if essential_errors(inventory):
            return None
        if mode == 'local_area':
            if inventory['hotspot']['state'] != 'on':
                return None
        else:
            if profile is None:
                return None
            connected = [item for item in inventory['wifi_interfaces'] if
                item['interface_id'] == profile.interface_id and item['state'] == 'connected' and
                item['current_profile'] == profile.wlan_profile]
            if len(connected) != 1:
                return None
        candidates = []
        for item in inventory['addresses']:
            if item['adapter_status'] != 'Up' or item['address_state'] != 'Preferred' or item.get('skip_as_source', False):
                continue
            if mode == 'local_area':
                if item['candidate_kind'] not in LOCAL_ADAPTER_KINDS:
                    continue
            elif item['adapter_guid'] != profile.interface_id or item['network_category'] != 'Private':
                continue
            try:
                MobileSettings(item['ip_address'], item['prefix_length'], token='A' * 43).validate()
            except ValueError:
                continue
            if sum(address['ip_address'] == item['ip_address'] for address in inventory['addresses']) != 1:
                continue
            candidates.append(item)
        return candidates[0] if len(candidates) == 1 else None

    async def wait_address(self, mode, profile):
        deadline = self.clock() + self.connect_timeout
        previous = None
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise NetworkError('connection_not_ready')
            try:
                await asyncio.wait_for(self.inspect(), remaining)
            except TimeoutError:
                raise NetworkError('connection_not_ready') from None
            selected = self.select_address(mode, profile)
            identity = None if selected is None else (selected['adapter_guid'], selected['ip_address'],
                selected['prefix_length'], selected['interface_index'], mode,
                profile.wlan_profile if profile is not None else None)
            if selected is not None and identity == previous:
                return selected
            previous = identity
            if self.clock() >= deadline:
                raise NetworkError('connection_not_ready')
            await self.sleep(min(self.poll_interval, max(0, deadline - self.clock())))

    async def begin_switch(self, mode):
        if mode not in MODES:
            raise NetworkError('invalid_mode')
        if self.lock.locked():
            raise NetworkError('operation_busy', 409)
        if self.external_viewer:
            raise NetworkError('external_viewer_running', 409)
        await self.lock.acquire()
        self.operation = dict(state='running', phase='validate', message=MESSAGES['validate'], mode=mode, warnings=[])
        self.last_error = None
        self.task = asyncio.create_task(self._switch(mode))

    def previous_address(self, old_settings, old_mobile, before):
        """Require original interface/profile identity, even if another SSID reused its IP."""
        if before is None or essential_errors(before):
            raise NetworkError('previous_network_unavailable')
        mode = old_settings.active_mode
        profile = getattr(old_settings.profiles, mode) if mode in ('mobile_router', 'home_router') else None
        if mode == 'unknown':
            originals = [item for item in before['addresses'] if item['ip_address'] == old_mobile.host and
                         item['prefix_length'] == old_mobile.prefix_length]
            if len(originals) != 1:
                raise NetworkError('previous_network_unavailable')
            original = originals[0]
            if original['candidate_kind'] in LOCAL_ADAPTER_KINDS:
                mode = 'local_area'
            else:
                interfaces = [item for item in before['wifi_interfaces'] if item['interface_id'] == original['adapter_guid'] and
                              item['state'] == 'connected' and item['current_profile'] is not None]
                if len(interfaces) != 1:
                    raise NetworkError('previous_network_unavailable')
                profile = RouterProfile(wlan_profile=interfaces[0]['current_profile'], interface_id=original['adapter_guid'])
                mode = 'home_router'  # Validation only; never commit this inferred label.
        original = self.select_address(mode, profile, inventory=before)
        current = self.select_address(mode, profile)
        if original is None or current is None:
            raise NetworkError('previous_network_unavailable')
        for item in (original, current):
            if (item['ip_address'], item['prefix_length']) != (old_mobile.host, old_mobile.prefix_length):
                raise NetworkError('previous_network_unavailable')
        if original['adapter_guid'] != current['adapter_guid']:
            raise NetworkError('previous_network_unavailable')
        return current, mode == 'local_area'

    async def rollback(self, snapshot, old_settings, old_mobile, was_running, changed_network, old_inventory):
        self.phase('rollback')
        recovered = True
        try:
            await self.viewer.stop()
        except Exception:
            recovered = False
        try:
            self.files.restore(snapshot)
        except Exception:
            recovered = False
        self.settings, self.mobile_settings = old_settings, old_mobile
        self.active_verified = False
        if was_running and old_mobile is not None:
            try:
                await self.inspect()
                address, local_area = self.previous_address(old_settings, old_mobile, old_inventory)
                await self.platform.ensure_firewall(old_mobile, address['interface_index'], local_area)
                await self.viewer.start(old_mobile)
                self.active_verified = True
            except Exception:
                recovered = False
        state = 'recovery_required' if changed_network or not recovered else 'failed'
        self.phase(state, state)

    async def _switch(self, mode):
        old_settings = self.settings.model_copy(deep=True)
        old_mobile = self.mobile_settings
        was_running = self.viewer.running
        snapshot = None
        old_inventory = None
        changed_network = False
        failure = 'switch_failed'
        try:
            await self.inspect()
            old_inventory = copy.deepcopy(self.inventory)
            profile = self.profile(mode)
            if essential_errors(self.inventory) or (mode != 'local_area' and not self.profile_available(profile)):
                raise NetworkError('profile_not_available')
            self.phase('check_main')
            mobile = self.files.load_mobile()
            if mobile is not None and (mobile.port != 8766 or mobile.main_port != 8765):
                raise NetworkError('viewer_ports_invalid')
            if not await self.health_check(8765):
                raise NetworkError('main_unavailable')
            token = mobile.token if mobile else secrets.token_urlsafe(32)
            try:
                before = self.files.snapshot()
                self.files.backup(before)
            except Exception:
                raise NetworkError('configuration_backup_failed') from None
            snapshot = before
            self.phase('stop_viewer')
            await self.viewer.stop()
            if mode == 'local_area':
                self.phase('start_hotspot')
                changed_network = True
                await self.platform.start_hotspot()
            else:
                already_connected = self.inventory['hotspot']['state'] == 'off' and self.select_address(mode, profile) is not None
                if self.inventory['hotspot']['state'] == 'on':
                    self.phase('stop_hotspot')
                    changed_network = True
                    await self.platform.stop_hotspot()
                elif self.inventory['hotspot']['state'] == 'unknown':
                    self.operation['warnings'].append('hotspot_state_unconfirmed')
                if not already_connected:
                    self.phase('connect')
                    changed_network = True
                    await self.platform.connect(profile.wlan_profile, profile.interface_id)
            self.phase('inspect')
            address = await self.wait_address(mode, profile)
            updated_mobile = MobileSettings(address['ip_address'], address['prefix_length'], token=token).validate()
            self.phase('firewall')
            await self.platform.ensure_firewall(updated_mobile, address['interface_index'], mode == 'local_area')
            self.phase('start_viewer')
            await self.viewer.start(updated_mobile)
            self.phase('commit')
            updated = self.settings.model_copy(deep=True)
            updated.active_mode = mode
            from dataclasses import asdict
            self.files.atomic(self.files.path('mobile.local.json'), json.dumps(asdict(updated_mobile), indent=2).encode())
            connection_page(updated_mobile, self.files.path('mobile-connect.local.html'))
            self.files.save_network(updated)
            self.settings, self.mobile_settings = updated, updated_mobile
            self.active_verified = True
            self.phase('complete', 'succeeded')
            if self.operation['warnings']:
                self.operation['message'] += ' ホットスポットの停止状態は未確認です。'
        except asyncio.CancelledError:
            failure = 'operation_interrupted'
            if snapshot is not None:
                await self.rollback(snapshot, old_settings, old_mobile, was_running, changed_network, old_inventory)
            else:
                self.phase('failed', 'failed')
            self.last_error = failure
        except Exception as exc:
            phase_codes = {'firewall': 'firewall_failed', 'connect': 'wifi_connect_failed',
                'start_hotspot': 'hotspot_start_failed', 'stop_hotspot': 'hotspot_stop_failed',
                'start_viewer': 'viewer_start_failed', 'commit': 'configuration_write_failed'}
            failure = error_code(exc, phase_codes.get(self.operation['phase'], 'switch_failed'))
            if snapshot is not None:
                await self.rollback(snapshot, old_settings, old_mobile, was_running, changed_network, old_inventory)
            else:
                self.phase('failed', 'failed')
            self.last_error = failure
        finally:
            self.lock.release()


class LocalGuard:
    def __init__(self, app, token, port, edition):
        self.app, self.token, self.port = app, token, port
        self.edition = edition

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        if scope['type'] == 'websocket':
            return await send(dict(type='websocket.close', code=1008))
        headers = {}
        for name, value in scope.get('headers', []):
            headers.setdefault(name.lower(), []).append(value)
        def one(name):
            values = headers.get(name, [])
            return values[0].decode('latin1') if len(values) == 1 else None
        host, origin = one(b'host'), one(b'origin')
        try:
            local = ipaddress.ip_address(scope['client'][0]).is_loopback
        except (ValueError, TypeError, IndexError):
            local = False
        allowed_hosts = (f'127.0.0.1:{self.port}', f'localhost:{self.port}')
        navigation = (scope['method'] == 'GET' and scope['path'] == '/' and b'origin' not in headers and
                      one(b'sec-fetch-mode') == 'navigate' and one(b'sec-fetch-dest') == 'document')
        fetch_valid = one(b'sec-fetch-site') in (None, 'none', 'same-origin') or navigation
        invalid = (not local or host not in allowed_hosts or
            (b'origin' in headers and origin != f'http://{host}') or
            not fetch_valid or any(len(headers.get(name, [])) > 1 for name in
                (b'sec-fetch-site', b'sec-fetch-mode', b'sec-fetch-dest')))
        error, status = ('local_access_only', 403) if invalid else (None, 200)
        method = scope['method']
        if not error and not self.edition.can_manage_network and (
                method in ('POST', 'PUT', 'PATCH', 'DELETE') or scope['path'] == '/connection'):
            error, status = 'edition_network_restricted', 403
        if not error and method not in ('GET', 'POST'):
            error, status = 'method_not_allowed', 405
        if not error and method == 'POST':
            supplied = one(b'x-gtbms-token') or ''
            if not secrets.compare_digest(supplied.encode(), self.token.encode()):
                error, status = 'session_required', 403
        body = b''
        if not error:
            length = one(b'content-length')
            if b'content-length' in headers and (length is None or not re.fullmatch(r'[0-9]{1,6}', length) or int(length) > MAX_BODY):
                error, status = 'request_too_large', 413
            else:
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    body += message.get('body', b'')
                    if len(body) > MAX_BODY:
                        error, status = 'request_too_large', 413
                        break
                    if not message.get('more_body', False):
                        break
        async def protected_send(message):
            if message['type'] == 'http.response.start':
                style = "'unsafe-inline'" if scope['path'] == '/connection' else "'self'"
                extra = {'cache-control': 'no-store', 'referrer-policy': 'no-referrer',
                    'x-content-type-options': 'nosniff', 'x-frame-options': 'DENY',
                    'content-security-policy': f"default-src 'self'; script-src 'self'; style-src {style}; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}
                message = dict(message, headers=list(message.get('headers', [])) +
                               [(key.encode(), value.encode()) for key, value in extra.items()])
            await send(message)
        if error:
            return await JSONResponse({'detail': error}, status_code=status)(scope, receive, protected_send)
        delivered = False
        async def body_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return dict(type='http.request', body=body, more_body=False)
        await self.app(scope, body_receive, protected_send)


def create_network_app(root=None, *, manager=None, port=MANAGER_PORT, edition: Edition | None = None):
    edition = load_edition() if edition is None else edition
    if not isinstance(edition, Edition):
        raise TypeError('edition must be an Edition')
    manager = manager or NetworkManager(root or Path.cwd())
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        if edition.can_manage_network:
            await manager.start()
        try:
            yield
        finally:
            if edition.can_manage_network:
                await manager.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager, app.state.csrf = manager, token
    app.state.edition = edition
    app.add_middleware(LocalGuard, token=token, port=port, edition=edition)

    @app.exception_handler(NetworkError)
    async def known_error(request, exc):
        return JSONResponse({'detail': exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({'detail': 'invalid_request'}, status_code=400)

    @app.get('/')
    async def index():
        return FileResponse(STATIC / 'network.html')

    @app.get('/static/network.js')
    async def javascript():
        return FileResponse(STATIC / 'network.js')

    @app.get('/static/network.css')
    async def stylesheet():
        return FileResponse(STATIC / 'network.css')

    @app.get('/api/session')
    async def session():
        return {'token': token, 'edition': edition.to_dict()}

    @app.get('/api/health')
    async def health():
        return {'ok': True, 'application': 'gtbms-network', 'version': 1}

    @app.get('/api/state')
    async def state():
        return {**manager.state(), 'edition': edition.to_dict()}

    @app.get('/connection')
    async def connection():
        if not manager.viewer.running or not manager.files.path('mobile-connect.local.html').exists():
            raise NetworkError('viewer_not_running', 409)
        return FileResponse(manager.files.path('mobile-connect.local.html'))

    @app.post('/api/profile')
    async def profile(body: SaveProfile):
        await manager.save_profile(body)
        return manager.state()

    @app.post('/api/switch', status_code=202)
    async def switch(body: Switch):
        await manager.begin_switch(body.mode)
        return {'operation': copy.deepcopy(manager.operation)}

    @app.post('/api/refresh')
    async def refresh(body: Input):
        await manager.refresh()
        return manager.state()

    @app.post('/api/hotspot-settings')
    async def hotspot_settings(body: Input):
        await manager.hotspot_settings()
        return {'ok': True}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('serve',))
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        import uvicorn
        uvicorn.run(create_network_app(args.root), host='127.0.0.1', port=MANAGER_PORT,
                    access_log=False, log_level='warning', proxy_headers=False, workers=1, ws='none')
    except Exception:
        print('Network manager startup failed. Check local configuration and port availability.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
