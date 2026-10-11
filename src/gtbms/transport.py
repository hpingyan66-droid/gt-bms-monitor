from __future__ import annotations

import asyncio
import math
import time
from uuid import uuid4

from .config import Config
from .models import Sample, stamp
from .protocol import FrameDecoder, RX_UUID, TX_UUID, parse_sample, read_request


class JbdTransport:
    def __init__(self, config: Config, *, client_factory=None, scanner=None):
        self.config = config
        if client_factory is None or scanner is None:
            from bleak import BleakClient, BleakScanner
            client_factory = client_factory or BleakClient
            scanner = scanner or BleakScanner
        self.client_factory, self.scanner = client_factory, scanner
        self.client = None
        self.decoder = FrameDecoder()
        self.pending = None
        self.expected = None
        self.connection_id = ""
        self.write_response = False
        self.on_disconnect = None
        self._generation = None

    def disconnected(self, _client):
        if _client is not self.client:
            return
        if self.pending and not self.pending.done():
            self.pending.set_exception(ConnectionError("BLE disconnected"))
        if self.on_disconnect is not None:
            self.on_disconnect()

    def notification(self, _sender, data):
        for frame in self.decoder.feed(bytes(data)):
            if self.pending and not self.pending.done() and frame[1] == self.expected:
                self.pending.set_result(frame)

    async def connect(self):
        if not self.config.ble_address.strip():
            raise ValueError("BLE address must be configured locally")
        device = await self.scanner.find_device_by_address(self.config.ble_address, timeout=10)
        if device is None:
            raise ConnectionError("Configured device not found")
        # Device name alone is insufficient evidence; valid JBD responses confirm protocol.
        generation = object()
        self._generation = generation

        def disconnected(client):
            if self._generation is generation:
                self.disconnected(client)

        def notification(sender, data):
            # WinRT callbacks already queued by a previous connection must not
            # satisfy a request, or contaminate the decoder, after reconnecting.
            if self._generation is generation:
                self.notification(sender, data)

        self.client = self.client_factory(device, disconnected_callback=disconnected,
                                          pair=self.config.pair, timeout=20)
        await self.client.connect()
        rx = self.client.services.get_characteristic(RX_UUID)
        tx = self.client.services.get_characteristic(TX_UUID)
        if rx is None or tx is None or "notify" not in rx.properties:
            raise ConnectionError("Required JBD characteristics absent")
        if not (set(tx.properties) & {"write", "write-without-response"}):
            raise ConnectionError("JBD request characteristic is not writable")
        self.write_response = "write" in tx.properties
        self.decoder = FrameDecoder()
        self.connection_id = str(uuid4())
        await self.client.start_notify(RX_UUID, notification)

    async def query(self, command: int) -> bytes:
        request = read_request(command)  # Whitelist before any GATT operation.
        if self.client is None or not self.client.is_connected:
            raise ConnectionError("BLE disconnected")
        if self.pending is not None:
            raise RuntimeError("Concurrent JBD queries are prohibited")
        self.expected = command
        self.pending = asyncio.get_running_loop().create_future()
        try:
            await asyncio.wait_for(self.client.write_gatt_char(TX_UUID, request, response=self.write_response),
                                   self.config.request_timeout_seconds)
            return await asyncio.wait_for(self.pending, self.config.request_timeout_seconds)
        finally:
            if not self.pending.done():
                self.pending.cancel()
            self.pending = None
            self.expected = None

    async def read(self) -> Sample:
        basic = await self.query(3)
        received_at, received_monotonic = stamp(), time.monotonic()
        cells = await self.query(4)
        sample = parse_sample(basic, cells, battery_id=self.config.key, source="ble",
                              direction=self.config.current_direction, expected_cells=self.config.expected_cells)
        sample.received_at = received_at
        sample.monotonic_s = received_monotonic
        sample.connection_id = self.connection_id
        return sample

    async def close(self):
        # Retire callbacks before an intentional disconnect. Keep the retired
        # client local so its delayed completion cannot clear a newer client.
        self._generation = None
        client, self.client = self.client, None
        if client:
            await asyncio.wait_for(client.disconnect(), 5)


class MockTransport:
    """Explicitly synthetic measurements, isolated under battery_id:mock."""
    def __init__(self, config: Config):
        self.config = config
        self.counter = 0
        self.connection_id = ""

    async def connect(self):
        self.connection_id = str(uuid4())

    async def read(self) -> Sample:
        self.counter += 1
        if self.config.mock_disconnect_every and self.counter % self.config.mock_disconnect_every == 0:
            raise ConnectionError("Synthetic disconnect")
        k = self.counter
        soc = max(5, 100 - k*.015)
        cells = [4.18-k*.00001 + .005*math.sin(k/20+i) for i in range(16)]
        current = -8 - 2*math.sin(k/8)
        if k % 40 < 4:
            current = 2  # Synthetic charge; no claim about regeneration.
        return Sample(battery_id=self.config.key, source="mock", received_at=stamp(),
                      voltage_v=sum(cells), raw_current_a=current, soc_pct=soc,
                      remaining_ah=soc/2, configured_ah=50, cycles=7,
                      cell_voltages_v=cells, temperatures_c=[25+.5*math.sin(k/20), 27],
                      charge_mos=True, discharge_mos=True, protection_code=0,
                      balancing_mask=0, bms_version=41, current_direction="charge_positive",
                      connection_id=self.connection_id, monotonic_s=time.monotonic())

    async def close(self):
        pass


async def scan_devices() -> list[dict]:
    from bleak import BleakScanner
    adverts = await BleakScanner.discover(timeout=8, return_adv=True)
    return [{"name": adv.local_name or device.name or "名称なし", "address": device.address,
             "rssi": adv.rssi, "service_uuids": adv.service_uuids,
             "candidate_only": True} for device, adv in adverts.values()]
