"""The real optional connector runs against a simulated BLE client, never a pack."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("aiobmsble")
from bleak.backends.device import BLEDevice

from gtbms.config import Config
from gtbms.plugin_probe import PluginProbe, auth_request, private_library_logs
from gtbms.protocol import ProtocolError, RX_UUID, TX_UUID, read_request


@pytest.fixture(autouse=True)
def no_unused_os_client(monkeypatch):
    # The upstream constructor normally creates an unconnected placeholder client.
    monkeypatch.setattr("aiobmsble.basebms.BleakClient", lambda *args, **kwargs: None)


class Client:
    def __init__(self, frames, *, auth_status=0, silent=None, corrupt_auth=False, split=False):
        self.frames = frames
        self.auth_status, self.silent = auth_status, silent
        self.corrupt_auth, self.split = corrupt_auth, split
        self.is_connected = False
        self.writes = []
        self.callback = None
        self.closed = False
        self.disconnected_callback = None
        self.services = SimpleNamespace(get_characteristic=lambda char: SimpleNamespace(
            properties=["read", "notify"] if char == RX_UUID else ["write-without-response", "read"]))

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False
        self.closed = True
        if self.disconnected_callback:
            self.disconnected_callback(self)

    async def start_notify(self, char, callback):
        assert char == RX_UUID
        self.callback = callback

    async def stop_notify(self, char):
        assert char == RX_UUID
        self.callback = None

    async def write_gatt_char(self, char, data, *, response):
        assert char == TX_UUID and response is False
        self.writes.append(bytes(data))
        if data.startswith(b"\xff\xaa"):
            if self.silent == "auth":
                return
            reply = bytearray([0xff, 0xaa, 0x15, 1, self.auth_status, (0x16+self.auth_status) & 0xff])
            if self.corrupt_auth:
                reply[-1] ^= 1
        else:
            cmd = data[2]
            if self.silent == cmd:
                return
            reply = bytearray(self.frames[cmd-3])
        if self.split and data[0] == 0xdd:
            self.callback(None, reply[:12])
            self.callback(None, reply[12:])
        else:
            self.callback(None, reply)


def make_probe(frames, *, secret="", **kwargs):
    config = Config(mode="ble", ble_address=":".join(("00", "00", "00", "00", "00", "01")), request_timeout_seconds=.02)
    client = Client(frames, **kwargs)
    class Scanner:
        @staticmethod
        async def find_device_by_address(address, *, timeout):
            assert address == config.ble_address
            return BLEDevice(address, "Synthetic JBD", None)
    def factory(device, **options):
        assert options["pair"] is False
        client.disconnected_callback = options["disconnected_callback"]
        return client
    return PluginProbe(config, secret=secret, client_factory=factory, scanner=Scanner), client


@pytest.mark.parametrize("secret", ["", "test-P4"])
@pytest.mark.parametrize("split", [False, True])
async def test_official_plugin_auth_and_three_readings(frames, secret, split):
    with private_library_logs():
        probe, client = make_probe(frames, secret=secret, split=split)
        try:
            await probe.connect()
            for _ in range(3):
                sample = await probe.read()
                assert sample.voltage_v == 67.09
                assert sample.raw_current_a == -10
                assert sample.current_a is None and sample.power_w is None
                assert sample.configured_ah == 50 and sample.bms_version == 41
                assert sample.temperatures_c == [25, 27]
                assert sample.to_dict()["mos_temperature_c"] is None
                assert sample.source == "ble" and sample.battery_id == "gt50:ble"
            assert client.writes == ([auth_request(secret)] if secret else []) + [read_request(3), read_request(4)]*3
            assert probe.authentication == ("accepted" if secret else "not_requested")
            assert probe.report()["command_counts"] == {"authentication": int(bool(secret)),
                                                        "read_03": 3, "read_04": 3}
        finally:
            await probe.close()
        assert client.closed and not probe.secret


@pytest.mark.parametrize("fault", ["wrong", "silent", "bad_checksum"])
async def test_auth_failure_never_retries_or_reads(frames, fault):
    with private_library_logs():
        probe, client = make_probe(frames, secret="test-P4", auth_status=1 if fault == "wrong" else 0,
                                   silent="auth" if fault == "silent" else None, corrupt_auth=fault == "bad_checksum")
        try:
            with pytest.raises(PermissionError if fault == "wrong" else TimeoutError):
                await probe.connect()
            assert client.writes == [auth_request("test-P4")]
            assert not probe.ready
        finally:
            await probe.close()
        assert client.closed


@pytest.mark.parametrize("fault,stage,auth,count", [
    ("subscribe", "auth_subscribe", "not_attempted", 0),
    ("write", "auth_write", "write_unconfirmed", 1),
    ("reply", "auth_wait_reply", "reply_timeout", 1),
])
async def test_timeout_substage_and_precleanup_state(frames, fault, stage, auth, count):
    """Same timeout result must distinguish CCCD, write, and silent peripheral."""
    probe, client = make_probe(frames, secret="test-P4", silent="auth")
    if fault == "subscribe":
        async def hang_notify(char, callback):
            await asyncio.Event().wait()
        client.start_notify = hang_notify
    elif fault == "write":
        async def hang_write(char, data, *, response):
            client.writes.append(bytes(data))
            await asyncio.Event().wait()
        client.write_gatt_char = hang_write
    with private_library_logs():
        try:
            with pytest.raises(TimeoutError):
                # Matches the CLI's enclosing deadline; no new transport timeout.
                async with asyncio.timeout(.1):
                    await probe.connect()
        finally:
            await probe.close()
    report = probe.report()
    assert report["stage"] == stage
    assert report["authentication"] == auth
    assert report["notifications"] == 0
    assert report["failure"]["connected"] is True
    assert report["failure"]["disconnect_count"] == 0
    assert not client.is_connected and client.closed
    assert report["disconnect_count"] == 1
    assert report["disconnect_events"][0]["during_cleanup"] is True
    assert report["command_counts"] == {"authentication": count, "read_03": 0, "read_04": 0}
    assert len(client.writes) == count
    assert report["operations"][-1]["stage"] == stage
    assert report["operations"][-1]["completed"] is False
    for operation in report["operations"]:
        assert operation["started_at"] and operation["finished_at"]
        assert operation["duration_seconds"] >= 0
        assert ("completed_at" in operation) is operation["completed"]
    assert "test-P4" not in json.dumps(report)
    assert ":".join(("00", "00", "00", "00", "00", "01")) not in json.dumps(report)


async def test_subscribe_failure_journal_does_not_record_exception_text(frames):
    probe, client = make_probe(frames, secret="test-P4")
    async def fail_notify(char, callback):
        raise RuntimeError("PRIVATE_PAYLOAD_AND_ADDRESS")
    client.start_notify = fail_notify
    with private_library_logs():
        try:
            with pytest.raises(RuntimeError):
                await probe.connect()
        finally:
            await probe.close()
    report = probe.report()
    assert report["failure"]["stage"] == "auth_subscribe"
    assert report["failure"]["error_type"] == "RuntimeError"
    assert report["authentication"] == "not_attempted" and not client.writes
    assert "PRIVATE" not in json.dumps(report)


async def test_disconnect_before_timeout_is_distinct_from_cleanup(frames):
    probe, client = make_probe(frames, secret="test-P4", silent="auth")
    async def disconnect_on_write(char, data, *, response):
        client.writes.append(bytes(data))
        client.is_connected = False
        client.disconnected_callback(client)
    client.write_gatt_char = disconnect_on_write
    with private_library_logs():
        try:
            with pytest.raises(TimeoutError):
                await probe.connect()
        finally:
            await probe.close()
    report = probe.report()
    assert report["failure"]["connected"] is False
    assert report["failure"]["disconnect_count"] == 1
    assert report["disconnect_events"][0]["during_cleanup"] is False
    assert report["disconnect_events"][-1]["during_cleanup"] is True
    assert report["command_counts"] == {"authentication": 1, "read_03": 0, "read_04": 0}


@pytest.mark.parametrize("frame_to_send", [b"\xdd\x5a\xe1\x00\x00\x00\x77", b"\xff\xaa\x16\x00\x16",
                                     b"\xdd\xa5\x05\x00\xff\xfb\x77", b"bad", b"\xff\xaa\x15\x01x\x8e"])
async def test_final_gatt_guard_blocks_other_commands(frames, frame_to_send):
    with private_library_logs():
        probe, client = make_probe(frames, secret="test-P4")
        await probe.connect()
        try:
            with pytest.raises(ProtocolError):
                await probe.guard.write_gatt_char(TX_UUID, frame_to_send, response=False)
            with pytest.raises(ProtocolError):
                await probe.guard.write_gatt_char(TX_UUID, auth_request("test-P4"), response=False)
            with pytest.raises(ProtocolError):
                await probe.guard.write_gatt_char(RX_UUID, read_request(3), response=False)
            with pytest.raises(ProtocolError):
                await probe.guard.write_gatt_char(TX_UUID, read_request(3), response=True)
            assert len(client.writes) == 1
        finally:
            await probe.close()


async def test_missing_new_cells_never_reuses_old_sample(frames):
    with private_library_logs():
        probe, client = make_probe(frames)
        await probe.connect()
        try:
            await probe.read()
            client.silent = 4
            with pytest.raises(TimeoutError):
                await probe.read()
            assert set(probe.frames) == {3}
            assert client.writes == [read_request(3), read_request(4)]*2
        finally:
            await probe.close()


async def test_disconnection_and_late_callback_are_rejected(frames):
    with private_library_logs():
        probe, client = make_probe(frames)
        await probe.connect()
        callback = client.callback
        await probe.close()
        callback(None, bytearray(frames[0]))
        assert not probe.frames and probe.notifications == 0
        with pytest.raises(ConnectionError):
            await probe.read()


async def test_cancelling_auth_closes_and_retires_connection(frames):
    with private_library_logs():
        probe, client = make_probe(frames, secret="test-P4", silent="auth")
        probe.config.request_timeout_seconds = 10
        async def session():
            try:
                await probe.connect()
            finally:
                await probe.close()
        task = asyncio.create_task(session())
        for _ in range(10):
            await asyncio.sleep(0)
            if client.writes:
                break
        assert len(client.writes) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed and not probe.guard.active and not probe.secret


async def test_os_pairing_is_never_attempted(frames):
    with private_library_logs():
        probe, client = make_probe(frames)
        probe.config.pair = True
        with pytest.raises(ValueError):
            await probe.connect()
        assert not client.is_connected and not client.writes


async def test_disconnect_during_cell_response_has_no_sample(frames):
    with private_library_logs():
        probe, client = make_probe(frames)
        await probe.connect()
        write = client.write_gatt_char
        async def disconnect_on_cells(char, data, *, response):
            await write(char, data, response=response)
            if data == read_request(4):
                client.is_connected = False
        client.write_gatt_char = disconnect_on_cells
        try:
            with pytest.raises(ProtocolError):
                await probe.read()
        finally:
            await probe.close()


def test_version_mismatch_fails_before_connect(monkeypatch):
    monkeypatch.setattr("gtbms.plugin_probe.version", lambda name: "unreviewed")
    with pytest.raises(RuntimeError):
        PluginProbe(Config())


def test_logs_restore_stricter_existing_setting_after_error():
    previous = logging.root.manager.disable
    try:
        logging.disable(100)
        with pytest.raises(RuntimeError), private_library_logs():
            assert logging.root.manager.disable == 100
            raise RuntimeError("test")
        assert logging.root.manager.disable == 100
    finally:
        logging.disable(previous)


@pytest.mark.parametrize("secret", ["非ASCII", "abc\n", "x"*16])
def test_invalid_secret_rejected_before_connection(secret):
    with pytest.raises(ValueError):
        auth_request(secret)


def test_debug_logs_suppressed_even_with_root_handler(caplog):
    caplog.set_level(logging.DEBUG)
    previous = logging.root.manager.disable
    with private_library_logs():
        logging.getLogger("aiobmsble.bms.jbd_bms").debug("PRIVATE_AUTH_PAYLOAD")
        logging.getLogger("bleak.backends.winrt.client").warning("PRIVATE_ADDRESS")
    assert "PRIVATE" not in caplog.text
    assert logging.root.manager.disable == previous


def test_cli_never_echoes_password_from_redirected_stdin(monkeypatch):
    from gtbms.cli import read_connection_secret
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("getpass.getpass", lambda *args: pytest.fail("must not prompt with fallback"))
    with pytest.raises(ValueError):
        read_connection_secret()


def test_cli_failure_report_does_not_leak_exception_text(tmp_path, monkeypatch, capsys):
    from gtbms.cli import run_plugin_probe
    monkeypatch.chdir(tmp_path)
    class FailingProbe:
        def __init__(self, *args, **kwargs):
            pass
        async def connect(self):
            raise ValueError("PRIVATE_SECRET_AND_ADDRESS")
        async def close(self):
            pass
        def report(self):
            return {"stage": "authenticate", "authentication": "pending"}
    monkeypatch.setattr("gtbms.plugin_probe.PluginProbe", FailingProbe)
    assert run_plugin_probe(Config(), False) == 1
    output = capsys.readouterr().out
    saved = (tmp_path / "artifacts/hardware/aiobmsble-last.json").read_text()
    assert "PRIVATE" not in output + saved
    assert json.loads(saved)["error_type"] == "ValueError"
    assert json.loads(saved)["samples_received"] == 0
