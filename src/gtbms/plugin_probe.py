"""Bounded diagnostic using the pinned upstream JBD connector.

The monitor does not select this transport automatically. Authentication is
session-only, with no guessed secrets, retries, bonding, or configuration writes.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from importlib.metadata import version
import logging
import time
from uuid import uuid4

from .config import Config
from .models import stamp
from .protocol import FrameDecoder, ProtocolError, RX_UUID, TX_UUID, parse_sample, read_request


@contextmanager
def private_library_logs():
    # Upstream DEBUG logs include the complete authentication frame. This context
    # is only used by the standalone diagnostic CLI, never in the running server.
    previous = logging.root.manager.disable
    logging.disable(max(previous, logging.CRITICAL))
    try:
        yield
    finally:
        logging.disable(previous)


def auth_request(secret: str) -> bytes | None:
    if not secret:
        return None
    if not secret.isascii() or not 1 <= len(secret) <= 15 or not all(32 <= ord(c) < 127 for c in secret):
        raise ValueError("Connection secret must be 1..15 printable ASCII characters")
    body = bytes((0x15, len(secret))) + secret.encode("ascii")
    return b"\xff\xaa" + body + bytes((sum(body) & 0xff,))


class GuardedClient:
    """Enforce the allowlist at the final GATT write boundary."""
    def __init__(self, client, secret: str, owner):
        self.client = client
        self.owner = owner
        self.auth_frame = auth_request(secret)
        self.auth_sent = False
        self.active = True

    @property
    def is_connected(self):
        return self.active and self.client.is_connected

    async def start_notify(self, char, callback):
        if str(char).lower() not in ("ff01", RX_UUID):
            raise ProtocolError("Unexpected notification characteristic")

        def notify(sender, data):
            if not self.active:
                return
            self.owner.notifications += 1
            if self.owner.expected in (3, 4):
                for frame in self.owner.decoder.feed(bytes(data)):
                    if frame[1] == self.owner.expected:
                        self.owner.frames[frame[1]] = (frame, stamp(), time.monotonic())
            callback(sender, data)

        stage = "auth_subscribe" if self.owner.authentication == "not_attempted" else "measurement_subscribe"
        with self.owner.operation(stage):
            await self.client.start_notify(RX_UUID, notify)

    async def stop_notify(self, char):
        if str(char).lower() not in ("ff01", RX_UUID):
            raise ProtocolError("Unexpected notification characteristic")
        # Upstream only removes its login callback after a valid accepted reply.
        self.owner.authentication = "accepted"
        with self.owner.operation("auth_unsubscribe"):
            await self.client.stop_notify(RX_UUID)

    async def write_gatt_char(self, char, data, *, response):
        if not self.is_connected or str(char).lower() not in ("ff02", TX_UUID):
            raise ProtocolError("Unexpected GATT write target")
        data = bytes(data)
        if data == self.auth_frame and not self.auth_sent:
            self.auth_sent = True  # A failed/ambiguous send must not be repeated.
            self.owner.authentication = "write_pending"
        elif data not in (read_request(3), read_request(4)):
            raise ProtocolError("Only one explicit login and read commands 03/04 are allowed")
        if response is not self.owner.write_response:
            raise ProtocolError("GATT write mode must match advertised properties")
        self.owner.command_counts["authentication" if data == self.auth_frame else f"read_{data[2]:02x}"] += 1
        await self.client.write_gatt_char(TX_UUID, data, response=response)
        if data == self.auth_frame:
            self.owner.authentication = "awaiting_reply"


class PluginProbe:
    def __init__(self, config: Config, *, secret="", client_factory=None, scanner=None):
        auth_request(secret)  # Validate before discovery/connection; never include value in errors.
        if version("aiobmsble") != "0.29.0":
            raise RuntimeError("This diagnostic requires aiobmsble==0.29.0")
        from bleak import BleakClient, BleakScanner
        self.config, self.secret = config, secret
        self.client_factory = client_factory or BleakClient
        self.scanner = scanner or BleakScanner
        self.client = self.guard = self.plugin = None
        self.ready = False
        self.stage = "created"
        self.authentication = "not_requested" if not secret else "not_attempted"
        self.write_response = False
        self.notifications = 0
        self.expected = None
        self.frames = {}
        self.decoder = FrameDecoder()
        self.connection_id = str(uuid4())
        self.operations = []
        self.disconnect_events = []
        self.command_counts = {"authentication": 0, "read_03": 0, "read_04": 0}
        self.failure = None
        self.closing = False

    @contextmanager
    def operation(self, stage):
        """Record only fixed labels, timestamps and exception types, never payloads."""
        self.stage = stage
        started = time.monotonic()
        entry = {"stage": stage, "started_at": stamp(), "completed": False}
        self.operations.append(entry)
        try:
            yield
        except BaseException as exc:
            entry["outcome"] = "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"
            entry["error_type"] = type(exc).__name__
            raise
        else:
            entry["completed"] = True
            entry["outcome"] = "completed"
            entry["completed_at"] = stamp()
        finally:
            entry["finished_at"] = stamp()
            entry["duration_seconds"] = round(time.monotonic() - started, 6)

    def _disconnected(self, _client):
        self.disconnect_events.append({"at": stamp(), "during_cleanup": self.closing,
                                       "stage": self.stage})

    def _capture_failure(self, exc):
        if self.failure is not None:
            return
        self.failure = {"stage": self.stage, "at": stamp(), "error_type": type(exc).__name__,
                        "connected": bool(self.client is not None and self.client.is_connected),
                        "disconnect_count": len(self.disconnect_events)}
        if self.stage == "auth_write":
            self.authentication = "write_unconfirmed"
        elif self.stage == "auth_wait_reply" and self.authentication not in ("accepted", "rejected"):
            self.authentication = "reply_timeout" if isinstance(exc, TimeoutError) else "reply_interrupted"

    async def connect(self):
        try:
            await self._connect()
        except BaseException as exc:
            self._capture_failure(exc)
            raise

    async def _connect(self):
        if self.config.mode != "ble" or not self.config.ble_address.strip():
            raise ValueError("Diagnostic requires a configured BLE target")
        if self.config.pair:
            raise ValueError("Diagnostic does not perform OS pairing; set pair=false")
        with self.operation("discovery"):
            device = await self.scanner.find_device_by_address(self.config.ble_address, timeout=10)
        if device is None:
            raise ConnectionError("Configured target not found")
        self.client = self.client_factory(device, pair=False, timeout=20,
                                          disconnected_callback=self._disconnected)
        with self.operation("connect"):
            await self.client.connect()
        with self.operation("gatt"):
            rx = self.client.services.get_characteristic(RX_UUID)
            tx = self.client.services.get_characteristic(TX_UUID)
            if rx is None or tx is None or "notify" not in rx.properties:
                raise ProtocolError("Required JBD GATT characteristics absent")
            if not {"write", "write-without-response"}.intersection(tx.properties):
                raise ProtocolError("JBD GATT characteristic does not allow requests")
            self.write_response = "write" in tx.properties
        self.guard = GuardedClient(self.client, self.secret, self)
        from aiobmsble import BMSConfig
        from aiobmsble.bms.jbd_bms import BMS
        owner = self

        class BoundedJbd(BMS):
            async def _connect(self):
                # Never run upstream discovery, pairing, reconnect, or auto probes.
                if not owner.ready or not self._client.is_connected:
                    raise ConnectionError("Diagnostic connection is not ready")

            async def _await_msg(self, data, char=None, wait_for_notify=True, max_size=0):
                if not wait_for_notify or max_size:
                    raise ProtocolError("Unsupported diagnostic request mode")
                owner.expected = 3 if data == read_request(3) else 4 if data == read_request(4) else None
                prefix = {3: "basic_03", 4: "cells_04"}.get(owner.expected, "auth")
                owner.decoder = FrameDecoder()
                self._frame.clear()
                self._msg_event.clear()
                # Exactly one send per call, using the advertised mode only.
                async with asyncio.timeout(owner.config.request_timeout_seconds):
                    with owner.operation(f"{prefix}_write"):
                        await self._client.write_gatt_char(char or TX_UUID, data, response=owner.write_response)
                    with owner.operation(f"{prefix}_wait_reply"):
                        await self._wait_event()

        self.plugin = BoundedJbd(device, BMSConfig(keep_alive=True, secret=self.secret))
        self.plugin._client = self.guard
        self.stage = "authenticate" if self.secret else "notify"
        try:
            await self.plugin._init_connection()
        except PermissionError:
            self.authentication = "rejected"
            raise
        if self.secret:
            self.authentication = "accepted"
        self.ready = True
        self.stage = "ready"

    async def read(self):
        try:
            return await self._read()
        except BaseException as exc:
            self._capture_failure(exc)
            raise

    async def _read(self):
        if not self.ready or not self.guard.is_connected:
            raise ConnectionError("Diagnostic connection is not ready")
        self.frames.clear()
        # Run the official connector but normalize only validated original frames:
        # no inferred sensor locations, capacities, or signed current directions.
        await self.plugin.async_update(raw=True)
        if set(self.frames) != {3, 4} or not self.guard.is_connected:
            raise ProtocolError("Fresh basic and cell frames required")
        basic, received_at, monotonic_s = self.frames[3]
        sample = parse_sample(basic, self.frames[4][0], battery_id=self.config.key,
                              source="ble", direction=self.config.current_direction,
                              expected_cells=self.config.expected_cells)
        sample.received_at, sample.monotonic_s = received_at, monotonic_s
        sample.connection_id = self.connection_id
        self.stage = "sample_received"
        return sample

    def report(self):
        return {"connector": "aiobmsble", "version": "0.29.0", "stage": self.stage,
                "authentication_method": "aiobmsble_0_29_ffaa15",
                "authentication": self.authentication, "notifications": self.notifications,
                "write_response": self.write_response, "hardware_compatibility": "unverified",
                "operations": [dict(op) for op in self.operations],
                "command_counts": dict(self.command_counts),
                "disconnect_events": [dict(event) for event in self.disconnect_events],
                "disconnect_count": len(self.disconnect_events),
                "failure": dict(self.failure) if self.failure else None}

    async def close(self):
        self.closing = True
        self.ready = False
        if self.guard is not None:
            self.guard.active = False
            self.guard.auth_frame = None
        if self.plugin is not None:
            from aiobmsble import BMSConfig
            self.plugin._cfg = BMSConfig()
        self.secret = ""
        if self.client is not None:
            await asyncio.wait_for(self.client.disconnect(), 5)
