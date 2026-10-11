import random

import pytest

from gtbms.protocol import FrameDecoder, ProtocolError, parse_sample, read_request, validate_frame
from conftest import response


def decode(frames):
    return parse_sample(*frames,battery_id="gt50:ble",source="ble",direction="unconfirmed",expected_cells=16)


def test_read_commands_exact():
    assert read_request(3).hex() == "dda50300fffd77"
    assert read_request(4).hex() == "dda50400fffc77"


@pytest.mark.parametrize("cmd", [0,1,2,5,0xE1,0x10,0xFF,-1])
def test_no_other_commands(cmd):
    with pytest.raises(ProtocolError):
        read_request(cmd)


def test_units_and_no_invented_mapping(frames):
    s = decode(frames)
    assert s.voltage_v == 67.09
    assert s.raw_current_a == -10
    assert s.remaining_ah == s.configured_ah == 50
    assert s.cycles == 7
    assert s.temperatures_c == [25,27]
    assert len(s.cell_voltages_v) == 16
    assert s.current_a is None and s.power_w is None
    assert s.to_dict()["mos_temperature_c"] is None
    assert s.bms_measured_at is None
    assert s.balancing_mask == 1


def test_explicit_current_direction(frames):
    s = decode(frames)
    s.current_direction = "charge_positive"
    assert s.current_a == 10 and s.power_w == pytest.approx(670.9)
    s.current_direction = "discharge_positive"
    assert s.current_a == -10


@pytest.mark.parametrize("mutation", ["header", "end", "checksum", "length", "status", "command"])
def test_reject_invalid_frames(frames,mutation):
    frame = bytearray(frames[0])
    positions={"header":0,"end":-1,"checksum":-2,"length":3,"status":2,"command":1}
    frame[positions[mutation]] ^= 1
    with pytest.raises(ProtocolError):
        validate_frame(frame,3)


def test_fragmentation_coalescing_and_noise(frames):
    decoder=FrameDecoder()
    assert decoder.feed(b"noise"+frames[0][:5]) == []
    assert decoder.feed(frames[0][5:]+frames[1]) == list(frames)


def test_corruption_resynchronization(frames):
    damaged=bytearray(frames[0]);damaged[-2]^=1
    decoder=FrameDecoder()
    assert decoder.feed(damaged+frames[1]) == [frames[1]]
    assert decoder.invalid_frames == 1


def test_buffer_bounded():
    decoder=FrameDecoder()
    decoder.feed(b"\xdd"*10000)
    assert len(decoder.buffer) <= 4096


def test_fuzz_must_not_return_invalid_frames():
    rnd=random.Random(43)
    decoder=FrameDecoder()
    for _ in range(2000):
        for frame in decoder.feed(rnd.randbytes(rnd.randint(1,300))):
            validate_frame(frame)
    assert len(decoder.buffer)<=4096


def test_ntc_bounds(frames):
    payload=bytearray(validate_frame(frames[0]));payload[22]=16
    with pytest.raises(ProtocolError):
        decode((response(3,payload),frames[1]))


def test_cells_count_must_match(frames):
    with pytest.raises(ProtocolError):
        decode((frames[0],response(4,b"\x10\x00")))


def test_zero_cell_preserved_as_alarm(frames):
    payload=bytearray(validate_frame(frames[1]));payload[:2]=b"\x00\x00"
    s=decode((frames[0],response(4,payload)))
    assert s.cell_voltages_v[0] == 0
    assert "cell_voltage_out_of_range" in s.quality_flags
