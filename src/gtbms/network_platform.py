"""Windows networking boundary: fixed scripts, no shell interpolation or secrets."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID, uuid4

from .mobile import atomic_private_write


class NetworkPlatformError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,80}', code) else 'network_operation_failed'
        super().__init__(self.code)


class NetworkPlatform:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or Path.cwd()).resolve()
        self.powershell = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                              'System32/WindowsPowerShell/v1.0/powershell.exe')

    async def _run(self, script, arguments, payload=None, timeout=45):
        if sys.platform != 'win32':
            raise NetworkPlatformError('windows_required')
        process = await asyncio.create_subprocess_exec(
            self.powershell, '-NoProfile', '-NonInteractive', '-File',
            str(self.root/'scripts'/script), *arguments,
            cwd=str(self.root), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            output, _ = await asyncio.wait_for(process.communicate(
                json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else b''), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise NetworkPlatformError('windows_operation_timeout') from None
        try:
            if len(output) > 1_048_576:
                raise ValueError()
            result = json.loads(output.decode('utf-8-sig'))
            if not isinstance(result, dict):
                raise ValueError()
        except (UnicodeError, ValueError):
            raise NetworkPlatformError('invalid_windows_response') from None
        if process.returncode or result.get('ok') is not True:
            raise NetworkPlatformError(result.get('code', 'windows_operation_failed'))
        return result

    async def inspect(self):
        result = await self._run('Network-Control.ps1', ['-Action', 'inspect'])
        inventory = result.get('inventory')
        if not isinstance(inventory, dict):
            raise NetworkPlatformError('invalid_network_inventory')
        return inventory

    async def connect(self, wlan_profile, interface_id):
        if not isinstance(wlan_profile, str) or not 1 <= len(wlan_profile) <= 255 or any(ord(c) < 32 for c in wlan_profile):
            raise NetworkPlatformError('invalid_wifi_profile')
        try:
            interface_id = str(UUID(interface_id.strip('{}')))
        except (ValueError, AttributeError):
            raise NetworkPlatformError('invalid_wifi_interface') from None
        return await self._run('Network-Control.ps1', ['-Action', 'connect'],
                               {'profile': wlan_profile, 'interface_id': interface_id})

    async def start_hotspot(self):
        # First-time adapter identification can require bounded stop/start and
        # an address observation; this runs in the manager's background task.
        return await self._run('Network-Control.ps1', ['-Action', 'start_hotspot'], timeout=110)

    async def stop_hotspot(self):
        return await self._run('Network-Control.ps1', ['-Action', 'stop_hotspot'], timeout=55)

    async def open_hotspot_settings(self):
        return await self._run('Network-Control.ps1', ['-Action', 'open_settings'])

    async def ensure_firewall(self, settings, interface_index, local_area=False):
        settings.validate()
        if type(interface_index) is not int or interface_index <= 0 or settings.port != 8766:
            raise NetworkPlatformError('invalid_firewall_request')
        request_id = str(uuid4())
        directory = self.root/'logs/network-requests'
        directory.mkdir(parents=True, exist_ok=True)
        request_path = directory/(request_id+'.json')
        # The request never contains the viewer token or credentials.
        payload = {k:v for k,v in asdict(settings).items() if k in ('host','prefix_length','port')}
        payload.update(interface_index=interface_index, local_area=bool(local_area),
                       expires_at=(datetime.now(timezone.utc)+timedelta(seconds=55)).isoformat())
        atomic_private_write(request_path, json.dumps(payload))
        try:
            return await self._run('Network-Firewall.ps1', ['-RequestId', request_id], timeout=80)
        finally:
            # A delayed UAC grant must not apply an expired request after rollback.
            request_path.unlink(missing_ok=True)
