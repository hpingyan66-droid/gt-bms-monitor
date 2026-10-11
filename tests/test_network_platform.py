import asyncio
import json
from pathlib import Path

import pytest

from gtbms.mobile import MobileSettings
from gtbms.network_platform import NetworkPlatform, NetworkPlatformError


class FakeProcess:
    def __init__(self, output, code=0):
        self.output, self.returncode = output, code
        self.input = None
        self.killed = False

    async def communicate(self, data=None):
        self.input = data
        return self.output, b'private error text must not escape'

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
async def test_saved_profile_is_stdin_not_shell(monkeypatch, tmp_path):
    calls = []
    process = FakeProcess(b'{"ok":true,"code":"connect_requested"}')
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process
    monkeypatch.setattr('gtbms.network_platform.sys.platform', 'win32')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    profile = 'saved "name" & echo $(unexpected)'
    await NetworkPlatform(tmp_path).connect(profile, '12345678-1234-1234-1234-123456789012')
    assert profile not in str(calls)
    assert json.loads(process.input)['profile'] == profile
    assert calls[0][0][-2:] == ('-Action','connect')
    assert calls[0][1]['cwd'] == str(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('output', [b'private error text', b'[]', b'{"ok":false,"code":"SSID password=private"}'])
async def test_bad_windows_output_is_redacted(monkeypatch, tmp_path, output):
    async def spawn(*args, **kwargs):
        return FakeProcess(output)
    monkeypatch.setattr('gtbms.network_platform.sys.platform', 'win32')
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(NetworkPlatformError) as error:
        await NetworkPlatform(tmp_path).inspect()
    assert 'private' not in str(error.value)
    assert 'SSID' not in str(error.value)


@pytest.mark.asyncio
async def test_firewall_request_has_expiry_and_no_token(tmp_path, monkeypatch):
    platform = NetworkPlatform(tmp_path)
    settings = MobileSettings('192.168.10.1', token='A'*43)
    seen=[]
    async def run(script, args, **kwargs):
        path=tmp_path/'logs/network-requests'/(args[-1]+'.json')
        data=json.loads(path.read_text())
        seen.append(data)
        assert settings.token not in path.read_text()
        assert data['local_area'] is True and data['interface_index']==42
        assert data['expires_at']
        assert kwargs['timeout']==80
        return {'ok':True,'code':'firewall_ready'}
    monkeypatch.setattr(platform,'_run',run)
    await platform.ensure_firewall(settings,42,True)
    assert len(seen)==1
    assert not list((tmp_path/'logs/network-requests').glob('*.json'))


@pytest.mark.asyncio
async def test_failed_firewall_invalidates_late_request(tmp_path, monkeypatch):
    platform=NetworkPlatform(tmp_path)
    async def run(*args,**kwargs):
        raise NetworkPlatformError('windows_operation_timeout')
    monkeypatch.setattr(platform,'_run',run)
    with pytest.raises(NetworkPlatformError):
        await platform.ensure_firewall(MobileSettings('192.168.10.1',token='A'*43),42)
    assert not list((tmp_path/'logs/network-requests').glob('*.json'))


@pytest.mark.asyncio
@pytest.mark.parametrize('profile,interface', [('bad\nprofile','12345678-1234-1234-1234-123456789012'),('x','; echo a')])
async def test_invalid_saved_profile_never_launches(tmp_path, monkeypatch, profile, interface):
    platform=NetworkPlatform(tmp_path)
    async def forbidden(*args, **kwargs):
        pytest.fail('must not launch Windows operation')
    monkeypatch.setattr(platform,'_run',forbidden)
    with pytest.raises(NetworkPlatformError):
        await platform.connect(profile,interface)
