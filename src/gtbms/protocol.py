"""JBD DD/A5 read protocol; informed by fl4p/batmon-ha (MIT).

Only basic information (03) and cell voltages (04) are permitted. This module
does not expose parameter writes, factory mode, MOS controls or firmware writes.
"""
from __future__ import annotations

from .models import Sample, stamp

RX_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
TX_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
READ_COMMANDS = frozenset((3, 4))


class ProtocolError(ValueError):
    pass


def read_request(command: int) -> bytes:
    if command not in READ_COMMANDS:
        raise ProtocolError("Only read commands 03 and 04 are allowed")
    return bytes((0xDD, 0xA5, command, 0)) + ((-command) & 0xFFFF).to_bytes(2, "big") + b"\x77"


def validate_frame(frame: bytes, expected: int | None = None) -> bytes:
    if len(frame) < 7 or frame[0] != 0xDD or frame[-1] != 0x77:
        raise ProtocolError("Invalid frame boundary")
    if frame[1] not in READ_COMMANDS or (expected is not None and frame[1] != expected):
        raise ProtocolError("Unexpected command")
    if len(frame) != frame[3] + 7:
        raise ProtocolError("Invalid frame length")
    if frame[2] != 0:
        raise ProtocolError("BMS returned error status")
    if int.from_bytes(frame[-3:-1], "big") != (-sum(frame[2:-3])) & 0xFFFF:
        raise ProtocolError("Invalid checksum")
    return frame[4:-3]


class FrameDecoder:
    """Bounded fragmented/coalesced notification decoder."""
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.invalid_frames = 0

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer.extend(data)
        if len(self.buffer) > 4096:
            self.buffer.clear()
            self.invalid_frames += 1
            return []
        frames = []
        while self.buffer:
            pos = self.buffer.find(0xDD)
            if pos < 0:
                self.buffer.clear()
                break
            if pos:
                del self.buffer[:pos]
            if len(self.buffer) < 4:
                break
            size = self.buffer[3] + 7
            if len(self.buffer) < size:
                break
            frame = bytes(self.buffer[:size])
            try:
                validate_frame(frame)
            except ProtocolError:
                self.invalid_frames += 1
                del self.buffer[0]
                continue
            del self.buffer[:size]
            frames.append(frame)
        return frames


def parse_sample(basic_frame: bytes, cell_frame: bytes, *, battery_id: str,
                 source: str, direction: str, expected_cells: int) -> Sample:
    basic = validate_frame(basic_frame, 3)
    cells = validate_frame(cell_frame, 4)
    if len(basic) < 23:
        raise ProtocolError("Basic payload too short")
    count, ntc_count = basic[21], basic[22]
    if not 1 <= count <= 32 or ntc_count > 16 or len(basic) < 23 + 2 * ntc_count:
        raise ProtocolError("Invalid cell/NTC count")
    if len(cells) != count * 2:
        raise ProtocolError("Cell payload does not match basic cell count")
    u16 = lambda start: int.from_bytes(basic[start:start + 2], "big")
    volts = [int.from_bytes(cells[i:i+2], "big") / 1000 for i in range(0, len(cells), 2)]
    temps = [(u16(23 + 2*i) - 2731) / 10 for i in range(ntc_count)]
    flags = []
    if count != expected_cells:
        flags.append("unexpected_cell_count")
    # Preserve alarming values, including 0V; mark them unsuitable for prediction.
    if any(v <= 0 or v > 5 for v in volts):
        flags.append("cell_voltage_out_of_range")
    if any(t < -60 or t > 150 for t in temps):
        flags.append("temperature_out_of_range")
    if basic[19] > 100:
        flags.append("soc_out_of_range")
    pack_v = u16(0) / 100
    if abs(pack_v-sum(volts)) > max(1, pack_v*.03):
        flags.append("pack_cell_voltage_mismatch")
    if u16(16):
        flags.append("bms_protection_active")
    return Sample(battery_id=battery_id, source=source, received_at=stamp(),
                  voltage_v=pack_v, raw_current_a=int.from_bytes(basic[2:4], "big", signed=True)/100,
                  soc_pct=basic[19], remaining_ah=u16(4)/100, configured_ah=u16(6)/100,
                  cycles=u16(8), cell_voltages_v=volts, temperatures_c=temps,
                  charge_mos=bool(basic[20] & 1), discharge_mos=bool(basic[20] & 2),
                  protection_code=u16(16), balancing_mask=u16(12) | u16(14) << 16,
                  bms_version=basic[18], current_direction=direction, quality_flags=flags)
