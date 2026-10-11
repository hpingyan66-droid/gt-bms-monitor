"""Command-free inspection and immutable report checks; no real BLE devices."""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from gtbms.config import Config
from gtbms.diagnostics import inspect_gatt, save_diagnostic
from gtbms.protocol import RX_UUID, TX_UUID


SYNTHETIC_ADDRESS = ":".join(("00", "00", "00", "00", "00", "01"))
PRIVATE_MARKER = "private-device-name-and-notification-payload"
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"


class Services(list):
    def get_characteristic(self, uuid):
        return next((c for s in self for c in s.characteristics if c.uuid == uuid), None)


class InspectionClient:
    def __init__(self, *, cccd=b"\x01\x00", cccd_error=None, connect_error=None,
                 subscribe_error=None, disconnect_error=None, drop=False):
        self.is_connected = False
        self.cccd, self.cccd_error = cccd, cccd_error
        self.connect_error, self.subscribe_error = connect_error, subscribe_error
        self.disconnect_error, self.drop = disconnect_error, drop
        self.subscribed = asyncio.Event()
        self.connecting = asyncio.Event()
        self.hold_connection = False
        self.closed = False
        self.descriptor_reads = []
        self.options = None
        rx = SimpleNamespace(uuid=RX_UUID, handle=2, properties=["read", "notify"],
                             descriptors=[SimpleNamespace(uuid=CCCD_UUID, handle=3)])
        tx = SimpleNamespace(uuid=TX_UUID, handle=4, properties=["read", "write-without-response"],
                             descriptors=[])
        self.services = Services([SimpleNamespace(uuid="0000ff00-0000-1000-8000-00805f9b34fb",
                                                   handle=1, characteristics=[rx, tx])])
        self._backend = SimpleNamespace(_requester=SimpleNamespace(device_information=SimpleNamespace(
            pairing=SimpleNamespace(is_paired=False, can_pair=True))))
        # Even an accidentally swallowed forbidden-operation failure must be visible.
        for name in ("write_gatt_char", "write_gatt_descriptor", "read_gatt_char", "pair", "unpair"):
            setattr(self, name, AsyncMock(side_effect=AssertionError(f"Forbidden operation: {name}")))

    async def connect(self):
        self.connecting.set()
        if self.connect_error:
            raise self.connect_error
        if self.hold_connection:
            await asyncio.Future()
        self.is_connected = True

    async def start_notify(self, characteristic, callback):
        assert characteristic.uuid == RX_UUID
        self.notification_callback = callback
        if self.subscribe_error:
            raise self.subscribe_error
        callback(characteristic, bytearray(PRIVATE_MARKER.encode()))
        self.subscribed.set()
        if self.drop:
            self.is_connected = False
            self.options["disconnected_callback"](self)

    async def read_gatt_descriptor(self, handle, *, use_cached):
        self.descriptor_reads.append((handle, use_cached))
        if self.cccd_error:
            raise self.cccd_error
        return bytearray(self.cccd)

    async def disconnect(self):
        self.closed = True
        self.is_connected = False
        if self.options:
            self.options["disconnected_callback"](self)
        if self.disconnect_error:
            raise self.disconnect_error

    def assert_command_free(self):
        for name in ("write_gatt_char", "write_gatt_descriptor", "read_gatt_char", "pair", "unpair"):
            getattr(self, name).assert_not_called()


def inspection(client=None, *, target_found=True, config=None):
    client = client or InspectionClient()
    config = config or Config(mode="ble", ble_address=SYNTHETIC_ADDRESS)
    device = SimpleNamespace(address=SYNTHETIC_ADDRESS, name=PRIVATE_MARKER)
    scanner = SimpleNamespace(find_device_by_address=AsyncMock(return_value=device if target_found else None))

    def factory(found_device, **kwargs):
        assert found_device is device
        client.options = kwargs
        return client

    return config, client, scanner, factory


async def inspect(fixture, **kwargs):
    config, _, scanner, factory = fixture
    return await inspect_gatt(config, scanner=scanner, client_factory=factory,
                              listen_seconds=kwargs.pop("listen_seconds", 0), **kwargs)


async def test_command_free_inspection_reads_profile_and_uncached_cccd():
    fixture = inspection()
    report = await inspect(fixture)
    _, client, scanner, _ = fixture
    client.assert_command_free()
    scanner.find_device_by_address.assert_awaited_once_with(SYNTHETIC_ADDRESS, timeout=10)
    assert client.options["pair"] is False
    assert client.options["winrt"] == {"use_cached_services": False}
    assert client.descriptor_reads == [(3, False)]
    assert client.closed
    assert report["ok"] and report["cleanup_ok"]
    assert report["stage"] == "inspection_complete"
    assert report["cccd_value"] == 1 and report["cccd_length_valid"]
    assert report["subscription_completed"] and report["connected_after_inspection"]
    assert report["jbd_rx_present"] and report["jbd_tx_present"]
    assert report["bms_requests_sent"] == 0 and report["pairing_requested"] is False
    assert report["notifications"] == 1 and report["unexpected_disconnects"] == 0
    assert report["windows_paired"] is False and report["windows_can_pair"] is True
    assert report["profile"][0]["characteristics"][0]["descriptors"][0]["uuid"] == CCCD_UUID
    assert [op["operation"] for op in report["operations"]] == [
        "discovery", "connect_and_uncached_gatt", "subscribe", "read_cccd", "passive_listen"]
    assert all(op["completed"] and op["elapsed_seconds"] >= 0 for op in report["operations"])
    payload = json.dumps(report)
    assert SYNTHETIC_ADDRESS not in payload and PRIVATE_MARKER not in payload


async def test_explicit_response_probe_sends_only_one_read03(frames):
    from gtbms.protocol import read_request
    fixture = inspection()
    client = fixture[1]
    async def write(characteristic, payload, *, response):
        assert characteristic.uuid == TX_UUID and response is True
        assert payload == read_request(3)
        client.notification_callback(None, bytearray(frames[0]))
    client.write_gatt_char = AsyncMock(side_effect=write)
    report = await inspect(fixture, read03_response=True)
    assert report["ok"] and report["basic_response_valid"]
    assert report["bms_requests_attempted"] == report["bms_requests_sent"] == 1
    assert report["authentication_requested"] is False
    assert report["gatt_write_completed"]
    assert client.write_gatt_char.await_count == 1
    for method in ("pair", "unpair", "read_gatt_char", "write_gatt_descriptor"):
        getattr(client, method).assert_not_called()
    assert client.closed


@pytest.mark.parametrize("code", [3, 5, 6])
async def test_response_probe_records_att_error_without_retry(code):
    from bleak.exc import BleakGATTProtocolError
    fixture = inspection()
    fixture[1].write_gatt_char = AsyncMock(side_effect=BleakGATTProtocolError(code))
    report = await inspect(fixture, read03_response=True)
    assert not report["ok"] and report["att_error_code"] == code
    assert report["stage"] == "read03_write_response"
    assert report["bms_requests_attempted"] == 1
    assert fixture[1].write_gatt_char.await_count == 1
    assert report["cleanup_ok"]


async def test_response_probe_silent_device_does_not_retry():
    fixture = inspection()
    fixture[0].request_timeout_seconds = .01
    fixture[1].write_gatt_char = AsyncMock()
    report = await inspect(fixture, read03_response=True)
    assert not report["ok"] and report["gatt_write_completed"]
    assert report["error_type"] == "TimeoutError" and report["stage"] == "read03_wait_reply"
    assert fixture[1].write_gatt_char.await_count == 1
    assert not report["basic_response_valid"] and report["cleanup_ok"]


async def test_response_probe_rejects_checksum_valid_but_short_payload():
    from conftest import response
    fixture = inspection()
    async def write(*args, **kwargs):
        fixture[1].notification_callback(None, bytearray(response(3, b"\x00")))
    fixture[1].write_gatt_char = AsyncMock(side_effect=write)
    report = await inspect(fixture, read03_response=True)
    assert not report["ok"] and not report["basic_response_valid"]
    assert report["error_type"] == "ProtocolError" and report["cleanup_ok"]


async def test_cccd_read_failure_preserves_profile_without_leaking_exception():
    fixture = inspection(InspectionClient(cccd_error=PermissionError(
        f"{SYNTHETIC_ADDRESS} {PRIVATE_MARKER}")))
    report = await inspect(fixture)
    assert report["ok"] and report["cleanup_ok"]
    assert report["cccd_present"] and report["cccd_error_type"] == "PermissionError"
    assert "cccd_value" not in report
    operation = next(op for op in report["operations"] if op["operation"] == "read_cccd")
    assert not operation["completed"] and operation["error_type"] == "PermissionError"
    assert report["profile"]
    assert PRIVATE_MARKER not in json.dumps(report) and SYNTHETIC_ADDRESS not in json.dumps(report)
    fixture[1].assert_command_free()


@pytest.mark.parametrize("value", [b"", b"\x01", PRIVATE_MARKER.encode()])
async def test_non_two_byte_cccd_value_is_never_saved(value):
    report = await inspect(inspection(InspectionClient(cccd=value)))
    assert report["cccd_length_valid"] is False and report["cccd_value"] is None
    assert PRIVATE_MARKER not in json.dumps(report)


async def test_absent_cccd_is_distinct_from_successful_read():
    fixture = inspection()
    fixture[1].services.get_characteristic(RX_UUID).descriptors = []
    report = await inspect(fixture)
    assert report["ok"] and report["cccd_present"] is False
    assert "cccd_value" not in report and fixture[1].descriptor_reads == []
    fixture[1].assert_command_free()


async def test_connect_timeout_is_staged_sanitized_and_cleaned_up():
    fixture = inspection(InspectionClient(connect_error=TimeoutError(
        f"{SYNTHETIC_ADDRESS} {PRIVATE_MARKER}")))
    report = await inspect(fixture)
    assert not report["ok"] and report["error_type"] == "TimeoutError"
    assert report["stage"] == "connect_and_uncached_gatt"
    assert not report["connected_at_failure"] and report["cleanup_ok"]
    assert report["operations"][-1]["error_type"] == "TimeoutError"
    assert PRIVATE_MARKER not in json.dumps(report) and SYNTHETIC_ADDRESS not in json.dumps(report)
    assert fixture[1].closed
    fixture[1].assert_command_free()


async def test_missing_target_does_not_construct_or_touch_client():
    fixture = inspection(target_found=False)
    report = await inspect(fixture)
    assert report["target_found"] is False and report["error_type"] == "ConnectionError"
    assert report["stage"] == "discovery" and not report["ok"]
    assert fixture[1].options is None and not fixture[1].closed
    fixture[1].assert_command_free()


async def test_mock_configuration_fails_before_scanning():
    fixture = inspection(config=Config())
    report = await inspect(fixture)
    assert report["stage"] == "preflight" and report["error_type"] == "ValueError"
    fixture[2].find_device_by_address.assert_not_called()
    fixture[1].assert_command_free()


async def test_subscription_failure_is_not_misreported_as_auth_failure():
    fixture = inspection(InspectionClient(subscribe_error=PermissionError(PRIVATE_MARKER)))
    report = await inspect(fixture)
    assert report["stage"] == "subscribe" and report["error_type"] == "PermissionError"
    assert not report["ok"] and report["cleanup_ok"] and "authentication" not in report
    assert fixture[1].descriptor_reads == [] and PRIVATE_MARKER not in json.dumps(report)
    fixture[1].assert_command_free()


async def test_unexpected_disconnect_cannot_pass_inspection():
    fixture = inspection(InspectionClient(drop=True))
    report = await inspect(fixture)
    assert report["unexpected_disconnects"] == 1
    assert not report["ok"] and not report["connected_after_inspection"]
    assert report["cleanup_ok"]
    fixture[1].assert_command_free()


async def test_cleanup_failure_marks_report_unsuccessful_and_sanitizes_message():
    fixture = inspection(InspectionClient(disconnect_error=OSError(PRIVATE_MARKER)))
    report = await inspect(fixture)
    assert report["cleanup_ok"] is False and not report["ok"]
    assert report["cleanup_error_type"] == "OSError"
    assert PRIVATE_MARKER not in json.dumps(report)


@pytest.mark.parametrize("phase", ["connect", "listen"])
async def test_cancellation_propagates_after_disconnect(phase):
    fixture = inspection()
    client = fixture[1]
    client.hold_connection = phase == "connect"
    partial = {}
    task = asyncio.create_task(inspect(fixture, listen_seconds=30, report_sink=partial))
    try:
        await asyncio.wait_for(client.connecting.wait() if phase == "connect" else client.subscribed.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed and not client.is_connected
        assert partial["cancelled"] and not partial["ok"]
        assert partial["error_type"] == "CancelledError"
        assert partial["cleanup_ok"] is True and partial["cleanup_status"] == "completed"
        assert partial["stage"] != "inspection_complete" and partial["finished_at"]
        assert PRIVATE_MARKER not in json.dumps(partial) and SYNTHETIC_ADDRESS not in json.dumps(partial)
        client.assert_command_free()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_two_reports_in_same_timestamp_coexist_and_latest_reads_back(tmp_path, monkeypatch):
    monkeypatch.setattr("gtbms.diagnostics.utcnow", lambda: datetime(2026, 10, 10, tzinfo=timezone.utc))
    first = {"ok": False, "stage": "authenticate", "authentication": "no_response", "notifications": 0}
    second = {"ok": True, "stage": "inspection_complete", "notifications": 1}
    path1 = save_diagnostic(first, "gatt", tmp_path)
    before = path1.read_bytes()
    path2 = save_diagnostic(second, "gatt", tmp_path)
    assert path1 != path2 and path1.exists() and path2.exists()
    assert path1.read_bytes() == before
    assert json.loads(path1.read_text(encoding="utf-8")) == first
    assert json.loads(path2.read_text(encoding="utf-8")) == second
    assert json.loads((tmp_path / "gatt-last.json").read_text(encoding="utf-8")) == second
    assert first["run_id"] != second["run_id"]
    assert first["report_schema"] == second["report_schema"] == 2
    assert first["finished_at"] and second["finished_at"]
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))


def test_unknown_report_kind_does_not_create_files(tmp_path):
    with pytest.raises(ValueError):
        save_diagnostic({"ok": False}, "../not-a-kind", tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("ok", [False, True])
def test_inspect_gatt_cli_reports_exit_status_and_saves_report(tmp_path, monkeypatch, capsys, ok):
    from gtbms import cli

    config = Config(mode="ble", ble_address=SYNTHETIC_ADDRESS)
    mock_inspect = AsyncMock(return_value={"ok": ok, "diagnostic": "gatt_only", "notifications": 0})
    monkeypatch.setattr("gtbms.diagnostics.inspect_gatt", mock_inspect)
    monkeypatch.setattr("gtbms.diagnostics.save_diagnostic", lambda report, kind: save_diagnostic(report, kind, tmp_path))
    monkeypatch.setattr(cli.Config, "load", lambda _path: config)
    monkeypatch.setattr(cli.sys, "argv", ["gtbms", "inspect-gatt"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == (0 if ok else 1)
    mock_inspect.assert_awaited_once_with(config, report_sink=ANY)
    output = capsys.readouterr().out
    assert '"diagnostic": "gatt_only"' in output and "Report:" in output
    assert PRIVATE_MARKER not in output and SYNTHETIC_ADDRESS not in output
    saved = json.loads((tmp_path / "gatt-last.json").read_text(encoding="utf-8"))
    assert saved["ok"] is ok and saved["report_schema"] == 2


@pytest.mark.parametrize("command,kind", [("inspect-gatt", "gatt"),
                                          ("probe-read03-response", "read03-response")])
def test_cancelled_cli_preserves_partial_report_and_exits_130(tmp_path, monkeypatch, capsys, command, kind):
    from gtbms import cli
    fixture = inspection()
    config, client, scanner, factory = fixture
    client.hold_connection = True
    real_inspect = inspect_gatt

    async def interrupted_inspect(actual_config, **options):
        assert actual_config is config
        task = asyncio.create_task(real_inspect(config, scanner=scanner, client_factory=factory, **options))
        await client.connecting.wait()
        task.cancel()
        return await task  # Cancellation remains observable to the CLI.

    monkeypatch.setattr("gtbms.diagnostics.inspect_gatt", interrupted_inspect)
    monkeypatch.setattr("gtbms.diagnostics.save_diagnostic", lambda report, name: save_diagnostic(report, name, tmp_path))
    monkeypatch.setattr(cli.Config, "load", lambda _path: config)
    monkeypatch.setattr(cli.sys, "argv", ["gtbms", command])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 130
    saved = json.loads((tmp_path / f"{kind}-last.json").read_text(encoding="utf-8"))
    assert saved["cancelled"] and not saved["ok"]
    assert saved["stage"] == "connect_and_uncached_gatt"
    assert saved["error_type"] == "CancelledError"
    assert saved["cleanup_ok"] is True and saved["cleanup_status"] == "completed"
    assert len([path for path in tmp_path.glob(f"{kind}-*.json") if path.name != f"{kind}-last.json"]) == 1
    assert client.closed and saved["bms_requests_sent"] == 0
    client.assert_command_free()
    output = capsys.readouterr().out + json.dumps(saved)
    assert PRIVATE_MARKER not in output and SYNTHETIC_ADDRESS not in output


def test_ctrl_c_before_inspection_returns_unknown_cleanup_without_exception_text(tmp_path, monkeypatch, capsys):
    from gtbms import cli
    config = Config(mode="ble", ble_address=SYNTHETIC_ADDRESS)

    def interrupted_runner(coroutine):
        coroutine.close()
        raise KeyboardInterrupt(PRIVATE_MARKER)

    monkeypatch.setattr(cli.asyncio, "run", interrupted_runner)
    monkeypatch.setattr("gtbms.diagnostics.save_diagnostic", lambda report, kind: save_diagnostic(report, kind, tmp_path))
    monkeypatch.setattr(cli.Config, "load", lambda _path: config)
    monkeypatch.setattr(cli.sys, "argv", ["gtbms", "inspect-gatt"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 130
    saved = json.loads((tmp_path / "gatt-last.json").read_text(encoding="utf-8"))
    assert saved["cancelled"] and not saved["ok"]
    assert saved["stage"] == "preflight"
    assert saved["cleanup_ok"] is None and saved["cleanup_status"] == "unknown"
    assert PRIVATE_MARKER not in capsys.readouterr().out + json.dumps(saved)


async def test_second_cancellation_reports_cleanup_as_interrupted_not_successful():
    fixture = inspection()
    client = fixture[1]
    disconnect_started = asyncio.Event()
    async def pending_disconnect():
        disconnect_started.set()
        await asyncio.Future()
    client.disconnect = pending_disconnect
    partial = {}
    task = asyncio.create_task(inspect(fixture, listen_seconds=30, report_sink=partial))
    try:
        await asyncio.wait_for(client.subscribed.wait(), 1)
        task.cancel()
        await asyncio.wait_for(disconnect_started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert partial["cancelled"] and not partial["ok"]
        assert partial["stage"] == "cleanup_disconnect"
        assert partial["cleanup_status"] == "interrupted" and partial["cleanup_ok"] is None
        assert partial["error_type"] == "CancelledError" and partial["finished_at"]
        client.assert_command_free()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
