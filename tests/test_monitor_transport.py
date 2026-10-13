import asyncio
import time
from types import SimpleNamespace

import pytest

from conftest import response, sample
from gtbms.monitor import Monitor,backoff
from gtbms.protocol import RX_UUID,TX_UUID,ProtocolError,validate_frame
from gtbms.storage import Store
from gtbms.transport import JbdTransport,MockTransport


class Scanner:
    @staticmethod
    async def find_device_by_address(*args,**kwargs):return SimpleNamespace(name="configured device")


class Client:
    def __init__(self,device,**kwargs):
        self.is_connected=False
        self.sent=[]
        self.disconnect_callback=kwargs["disconnected_callback"]
        self.services=self
    def get_characteristic(self,uuid):return SimpleNamespace(properties=["notify"] if uuid==RX_UUID else ["write-without-response"])
    async def connect(self):self.is_connected=True
    async def disconnect(self):self.is_connected=False
    async def start_notify(self,uuid,callback):self.callback=callback
    async def write_gatt_char(self,uuid,data,response):
        self.sent.append(bytes(data))
        frame=self.frames[data[2]-3]
        self.callback(None,frame[:4]);self.callback(None,frame[4:])


async def test_ble_client_with_fragmented_notifications(config,frames):
    config.ble_address="local test value";config.mode="ble"
    Client.frames=frames
    transport=JbdTransport(config,client_factory=Client,scanner=Scanner)
    await transport.connect()
    s=await transport.read()
    assert s.raw_current_a==-10
    assert len(transport.client.sent)==2
    assert all(cmd[1]==0xA5 and cmd[2] in (3,4) for cmd in transport.client.sent)
    await transport.close()


async def test_command_rejected_before_write(config,frames):
    config.ble_address="local";Client.frames=frames
    t=JbdTransport(config,client_factory=Client,scanner=Scanner)
    await t.connect()
    with pytest.raises(ProtocolError):await t.query(0xE1)
    assert not t.client.sent
    await t.close()


async def test_timeout_and_pending_cleanup(config,frames):
    class Silent(Client):
        async def write_gatt_char(self,*args,**kwargs):pass
    config.ble_address="local";config.request_timeout_seconds=.01
    t=JbdTransport(config,client_factory=Silent,scanner=Scanner)
    await t.connect()
    with pytest.raises(asyncio.TimeoutError):await t.query(3)
    assert t.pending is None
    await t.close()


async def test_retired_connection_callbacks_cannot_satisfy_new_request(config,frames):
    class Manual(Client):
        async def write_gatt_char(self,uuid,data,response):
            self.sent.append(bytes(data))
        async def disconnect(self):
            await super().disconnect()
            self.disconnect_callback(self)
    config.ble_address="local"
    t=JbdTransport(config,client_factory=Manual,scanner=Scanner)
    disconnects=[]
    t.on_disconnect=lambda:disconnects.append(True)
    await t.connect()
    retired=t.client
    await t.close()
    assert disconnects==[]  # Intentional close is not a connection failure.
    await t.connect()
    request=asyncio.create_task(t.query(3))
    await asyncio.sleep(0)
    retired.callback(None,frames[0])
    retired.callback(None,frames[0][:5])  # Cannot contaminate the new decoder.
    retired.disconnect_callback(retired)
    assert not t.pending.done()
    assert disconnects==[]
    updated=bytearray(validate_frame(frames[0]))
    updated[2:4]=(-1500).to_bytes(2,"big",signed=True)
    current=response(3,updated)
    t.client.callback(None,current)
    assert await request==current
    t.client.disconnect_callback(t.client)
    assert disconnects==[True]
    await t.close()
    assert disconnects==[True]


async def test_absent_address_sends_nothing(config):
    t=JbdTransport(config,client_factory=Client,scanner=Scanner)
    with pytest.raises(ValueError):await t.connect()
    assert t.client is None


async def wait_for_fresh(monitor):
    # Saving through finish_io can exceed a fixed 80 ms on hosted Windows runners.
    async with asyncio.timeout(5):
        while not monitor.snapshot()["fresh"]:
            await asyncio.sleep(.01)


@pytest.mark.parametrize("save_delay",[0,.15])
async def test_stop_and_restart(store,config,monkeypatch,save_delay):
    save_sample=store.save_sample
    def delayed_save(sample):
        time.sleep(save_delay)
        return save_sample(sample)
    monkeypatch.setattr(store,"save_sample",delayed_save)
    m=Monitor(config,store)
    try:
        await m.start();await wait_for_fresh(m)
        assert m.snapshot()["fresh"]
        await m.stop();assert m.snapshot()["measurement"] is None
        await m.start();await wait_for_fresh(m)
        assert m.snapshot()["fresh"]
    finally:
        await m.stop()


async def test_reconnect_and_stale_values(store,config,monkeypatch):
    failed=asyncio.Event()
    resume=asyncio.Event()
    class Flaky(MockTransport):
        async def read(self):
            if self.counter==1:
                self.counter+=1
                raise ConnectionError()
            return await super().read()
        async def close(self):
            if self.counter==2:
                failed.set()
                await resume.wait()
            await super().close()
    monkeypatch.setattr('gtbms.monitor.backoff',lambda attempt:.01)
    config.poll_seconds=.01
    m=Monitor(config,store,Flaky)
    try:
        await m.start()
        await asyncio.wait_for(failed.wait(),5)
        # Hold the reconnect boundary so a slow runner cannot miss this state.
        assert m.state=="reconnecting"
        assert m.snapshot()["measurement"] is None
        resume.set()
        await wait_for_fresh(m)
        assert m.state=="connected"
        assert store.gaps()
    finally:
        resume.set()
        await m.stop()


async def test_idle_ble_disconnect_invalidates_live_data_and_wakes_poll(config,frames,monkeypatch):
    config.mode="ble";config.ble_address="local";config.poll_seconds=10
    store=Store(config)
    Client.frames=frames
    transport=JbdTransport(config,client_factory=Client,scanner=Scanner)
    monkeypatch.setattr('gtbms.monitor.backoff',lambda attempt:.01)
    m=Monitor(config,store,factory=lambda _:transport)
    await m.start()
    try:
        async with asyncio.timeout(1):
            while not m.snapshot()["fresh"]:
                await asyncio.sleep(.01)
        retired=transport.client
        retired.is_connected=False
        retired.disconnect_callback(retired)
        assert m.snapshot()["state"]=="reconnecting"
        assert not m.snapshot()["fresh"]
        assert m.snapshot()["measurement"] is None
        # Reconnect in well under the configured 10 second polling interval.
        async with asyncio.timeout(1):
            while transport.client is retired or not m.snapshot()["fresh"]:
                await asyncio.sleep(.01)
        assert transport.client is not retired
    finally:
        await m.stop()
        store.close()
    assert m.snapshot()["state"]=="stopped"


def test_age_makes_measurement_unavailable(store,config):
    m=Monitor(config,store);m.latest=sample();m.state="connected";m.last_success_monotonic=time.monotonic()-100
    assert not m.snapshot()["fresh"]
    assert m.snapshot()["measurement"] is None


def test_restart_preserves_last_communication_as_historical(store,config):
    s=sample();store.save_sample(s)
    m=Monitor(config,store)
    assert m.last_success_at==s.received_at
    assert m.snapshot()["measurement"] is None


def test_exponential_backoff_is_capped():
    assert .9<=backoff(0)<=1.1
    assert 54<=backoff(100)<=66
