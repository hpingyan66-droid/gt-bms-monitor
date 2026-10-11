from __future__ import annotations

import asyncio
import logging
import random
import time
from uuid import uuid4

from .config import Config
from .models import parse_time, stamp, utcnow
from .storage import Store
from .transport import JbdTransport, MockTransport
from .workers import finish_io

logger = logging.getLogger("gtbms")


def backoff(attempt: int) -> float:
    return min(60, 2**min(attempt, 6)) * random.uniform(.9, 1.1)


class Monitor:
    def __init__(self, config: Config, store: Store, factory=None):
        self.config, self.store = config, store
        self.factory = factory or (MockTransport if config.mode == "mock" else JbdTransport)
        self.task = None
        self.state = "stopped"
        self.latest = None
        self.last_success_monotonic = None
        persisted = store.latest()
        self.last_success_at = persisted["received_at"] if persisted else None
        self.last_error = None
        self.retry_at = None
        self.running_since = stamp()
        self._control = asyncio.Lock()
        self._wake = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._accept_disconnect = False

    def disconnected(self):
        if self._accept_disconnect:
            # A known BLE disconnect invalidates the live display immediately,
            # even when it happens between requests during a long poll interval.
            self.state, self.latest = "reconnecting", None
            self._disconnected.set()
            self._wake.set()

    async def start(self):
        async with self._control:
            if self.task and not self.task.done():
                return
            self.task = asyncio.create_task(self.run(), name="bms-monitor")

    async def stop(self):
        async with self._control:
            if self.task:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
                self.task = None
            self.state = "stopped"
            self.latest = None
            self.store.event("monitor_stopped")

    def change_interval(self, interval: float):
        if not 1 <= interval <= 10:
            raise ValueError("Interval must be 1..10 seconds")
        self.config.poll_seconds = interval
        self._wake.set()

    def snapshot(self):
        age = time.monotonic()-self.last_success_monotonic if self.last_success_monotonic is not None else None
        fresh = self.state == "connected" and self.latest is not None and age is not None and 0 <= age <= self.config.stale_seconds
        latest = self.latest.to_dict() if fresh else None
        return {"state": self.state, "fresh": fresh, "measurement": latest,
                "last_success_at": self.last_success_at, "age_seconds": age,
                "last_error": self.last_error, "retry_in_seconds": max(0, self.retry_at-time.monotonic()) if self.retry_at else None,
                "mode": self.config.mode, "battery_id": self.config.key, "running_since": self.running_since,
                "poll_seconds": self.config.poll_seconds, "current_direction": self.config.current_direction,
                "hardware_verified": False if self.config.mode == "mock" else "未検証（利用者による実機確認が必要）"}

    async def delay(self, seconds):
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def run(self):
        attempt = 0
        gap_start = self.last_success_at or stamp()
        previous = None
        transport = self.factory(self.config)
        transport.on_disconnect = self.disconnected
        try:
            while True:
                try:
                    self._disconnected.clear()
                    self._accept_disconnect = True
                    self.state = "connecting"
                    self.store.event("connecting")
                    await asyncio.wait_for(transport.connect(), 40)
                    previous = None
                    while True:
                        if self._disconnected.is_set():
                            raise ConnectionError("BLE disconnected")
                        cycle_start = time.monotonic()
                        # Resume/scheduler stalls break the integration epoch and reinitialize BLE.
                        if previous and (utcnow()-parse_time(previous.received_at)).total_seconds() > self.config.max_gap_seconds:
                            raise ConnectionError("Sampling gap requires reinitialization")
                        sample = await asyncio.wait_for(transport.read(), self.config.request_timeout_seconds*2+2)
                        if self._disconnected.is_set():
                            raise ConnectionError("BLE disconnected")
                        if previous and sample.monotonic_s-previous.monotonic_s > self.config.max_gap_seconds:
                            self.store.gap(previous.received_at, sample.received_at, "sampling_delay")
                            sample.connection_id = str(uuid4())
                        await finish_io(self.store.save_sample, sample)
                        self.last_success_at = sample.received_at
                        self.last_success_monotonic = sample.monotonic_s
                        if self._disconnected.is_set():
                            raise ConnectionError("BLE disconnected while saving sample")
                        if gap_start:
                            self.store.gap(gap_start, sample.received_at, "startup_or_disconnected")
                            gap_start = None
                            self.store.event("connected", "JBD response valid" if self.config.mode == "ble" else "SYNTHETIC DATA")
                        if sample.protection_code or sample.quality_flags:
                            self.store.event("battery_alarm", f"code={sample.protection_code}; flags={','.join(sample.quality_flags)}")
                        self.latest, previous = sample, sample
                        self.state = "connected"
                        self.last_error, self.retry_at = None, None
                        attempt = 0
                        await self.delay(max(.05, self.config.poll_seconds-(time.monotonic()-cycle_start)))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.state, self.latest = "reconnecting", None
                    self.last_error = type(exc).__name__
                    logger.warning("BMS cycle failed: %s", type(exc).__name__)
                    self.store.event("communication_error", type(exc).__name__)
                    gap_start = gap_start or self.last_success_at or stamp()
                    self._accept_disconnect = False
                    try:
                        await transport.close()
                    except Exception as close_exc:
                        self.store.event("disconnect_error", type(close_exc).__name__)
                    wait = backoff(attempt)
                    attempt += 1
                    self.retry_at = time.monotonic()+wait
                    await self.delay(wait)
        finally:
            self._accept_disconnect = False
            transport.on_disconnect = None
            self.latest = None
            try:
                await transport.close()
            except Exception:
                logger.warning("BLE cleanup failed")
            if gap_start:
                self.store.gap(gap_start, stamp(), "stopped_during_gap")
