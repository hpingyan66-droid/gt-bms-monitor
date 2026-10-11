"""Private-data-free reports and a command-free BLE/GATT inspection."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time
from uuid import uuid4

from .models import stamp, utcnow
from .protocol import FrameDecoder, ProtocolError, RX_UUID, TX_UUID, read_request, validate_frame


def save_diagnostic(report: dict, kind: str, directory=Path("artifacts/hardware")) -> Path:
    """Append an immutable report; retain a replaceable convenience pointer."""
    if kind not in ("aiobmsble", "gatt", "read03-response"):
        raise ValueError("Unknown diagnostic report kind")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    report.update(report_schema=2, run_id=run_id)
    report.setdefault("finished_at", stamp())
    path = directory / f"{kind}-{utcnow().strftime('%Y%m%dT%H%M%S%fZ')}-{run_id}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    # The historical run above is the authoritative record.
    latest = directory / f"{kind}-last.json"
    tmp = directory / f".{kind}-{run_id}.tmp"
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(latest)
    finally:
        tmp.unlink(missing_ok=True)
    return path


async def inspect_gatt(config, *, scanner=None, client_factory=None, listen_seconds=2, read03_response=False,
                       report_sink=None):
    """Discover, subscribe, and read only the notification configuration (CCCD).

    Default inspection never calls write_gatt_char, authenticate, or pair/unpair.
    The separate, explicit read03_response diagnostic sends the fixed read03 frame
    once with response=True, to test a potentially misreported write property.
    It does not retry, change the monitor, or send an authentication request.
    Subscribing changes only this connection's notification configuration.
    """
    from bleak import BleakClient, BleakScanner
    scanner = scanner or BleakScanner
    client_factory = client_factory or BleakClient
    report = {"diagnostic": "gatt_only", "started_at": stamp(), "ok": False,
              "stage": "preflight", "bms_requests_sent": 0, "pairing_requested": False,
              "uncached_services_requested": True, "notifications": 0,
              "unexpected_disconnects": 0, "operations": [], "cancelled": False,
              "cleanup_ok": None, "cleanup_status": "not_started"}
    if read03_response:
        report.update(diagnostic="read03_response", bms_requests_attempted=0,
                      authentication_requested=False, basic_response_valid=False)
    client = None
    cleanup = False
    capture_reply = False
    reply_event = asyncio.Event()
    decoder = FrameDecoder()
    basic_frame = None
    async def step(name, awaitable, timeout):
        report["stage"] = name
        record = {"operation": name, "started_at": stamp(), "completed": False}
        report["operations"].append(record)
        start = time.monotonic()
        try:
            value = await asyncio.wait_for(awaitable, timeout)
            record["completed"] = True
            return value
        except (Exception, asyncio.CancelledError) as exc:
            record["error_type"] = type(exc).__name__
            from bleak.exc import BleakGATTProtocolError
            if isinstance(exc, BleakGATTProtocolError):
                record["att_error_code"] = int(exc.code)
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic()-start, 4)

    def disconnected(_):
        if not cleanup:
            report["unexpected_disconnects"] += 1

    def notified(_sender, _data):
        nonlocal basic_frame
        if not cleanup:
            report["notifications"] += 1  # Never retain unsolicited data bytes.
            if capture_reply:
                for frame in decoder.feed(bytes(_data)):
                    if frame[1] == 3:
                        basic_frame = frame
                        reply_event.set()

    try:
        if config.mode != "ble" or not config.ble_address.strip():
            raise ValueError("Configured BLE target required")
        device = await step("discovery", scanner.find_device_by_address(config.ble_address, timeout=10), 12)
        report["target_found"] = device is not None
        if device is None:
            raise ConnectionError("Target not found")
        client = client_factory(device, pair=False, timeout=20,
                                disconnected_callback=disconnected, winrt={"use_cached_services": False})
        await step("connect_and_uncached_gatt", client.connect(), 30)
        report["profile"] = [
            {"uuid": s.uuid, "handle": s.handle,
             "characteristics": [{"uuid": c.uuid, "handle": c.handle,
                                   "properties": list(c.properties),
                                   "descriptors": [{"uuid": d.uuid, "handle": d.handle} for d in c.descriptors]}
                                  for c in s.characteristics]}
            for s in client.services]
        # Read-only Windows metadata; no pair()/unpair() and no identity fields.
        try:
            pairing = client._backend._requester.device_information.pairing
            report["windows_paired"] = bool(pairing.is_paired)
            report["windows_can_pair"] = bool(pairing.can_pair)
        except (AttributeError, OSError):
            report["windows_paired"] = None
        rx = client.services.get_characteristic(RX_UUID)
        tx = client.services.get_characteristic(TX_UUID)
        report["jbd_rx_present"] = rx is not None
        report["jbd_tx_present"] = tx is not None
        if rx is None or "notify" not in rx.properties:
            raise ValueError("JBD notification characteristic absent")
        await step("subscribe", client.start_notify(rx, notified), 8)
        report["subscription_completed"] = True
        cccd = next((d for d in rx.descriptors if d.uuid.lower() == "00002902-0000-1000-8000-00805f9b34fb"), None)
        report["cccd_present"] = cccd is not None
        if cccd is not None:
            try:
                value = await step("read_cccd", client.read_gatt_descriptor(cccd.handle, use_cached=False), 8)
                # CCCD is exactly a 2-byte flag word. Ignore unexpected content.
                report["cccd_value"] = int.from_bytes(value, "little") if len(value) == 2 else None
                report["cccd_length_valid"] = len(value) == 2
            except Exception as exc:
                report["cccd_error_type"] = type(exc).__name__
        await step("passive_listen", asyncio.sleep(listen_seconds), listen_seconds+1)
        if read03_response:
            if tx is None or not client.is_connected:
                raise ConnectionError("Basic read target unavailable")
            report["advertised_write_properties"] = list(tx.properties)
            capture_reply = True
            report["bms_requests_attempted"] = 1
            # The only payload at this boundary is this exact, whitelisted read.
            await step("read03_write_response", client.write_gatt_char(tx, read_request(3), response=True),
                       config.request_timeout_seconds)
            report["bms_requests_sent"] = 1
            report["gatt_write_completed"] = True
            await step("read03_wait_reply", reply_event.wait(), config.request_timeout_seconds)
            payload = validate_frame(basic_frame, 3)
            if (len(payload) < 23 or not 1 <= payload[21] <= 32 or payload[22] > 16 or
                    len(payload) < 23+2*payload[22]):
                raise ProtocolError("Basic reply payload shape invalid")
            report["basic_response_valid"] = True
        report["connected_after_inspection"] = bool(client.is_connected)
        report["ok"] = bool(client.is_connected)
        report["stage"] = "inspection_complete"
    except asyncio.CancelledError:
        report["cancelled"] = True
        report["ok"] = False
        report["error_type"] = "CancelledError"
        report["connected_at_failure"] = bool(client and client.is_connected)
        raise
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["connected_at_failure"] = bool(client and client.is_connected)
        from bleak.exc import BleakGATTProtocolError
        if isinstance(exc, BleakGATTProtocolError):
            report["att_error_code"] = int(exc.code)
    finally:
        cleanup = True
        try:
            if client is not None:
                report["cleanup_status"] = "attempted"
                try:
                    await asyncio.wait_for(client.disconnect(), 5)
                    report["cleanup_ok"] = True
                    report["cleanup_status"] = "completed"
                except asyncio.CancelledError:
                    report["cleanup_status"] = "interrupted"
                    report["cancelled"] = True
                    report["ok"] = False
                    report["stage"] = "cleanup_disconnect"
                    report["error_type"] = "CancelledError"
                    raise
                except Exception as exc:
                    report["cleanup_ok"] = False
                    report["cleanup_status"] = "failed"
                    report["cleanup_error_type"] = type(exc).__name__
                    report["ok"] = False
            else:
                report["cleanup_status"] = "not_needed"
        finally:
            report["finished_at"] = stamp()
            if report_sink is not None:
                report_sink.update(report)
    return report
