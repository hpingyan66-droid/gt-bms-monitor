from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from gtbms.config import Config
from gtbms.models import Sample, stamp, utcnow
from gtbms.storage import Store
from gtbms.app import create_app


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=str(tmp_path/"data"), backup_dir=str(tmp_path/"backups"), autostart_monitor=False)


@pytest.fixture
def store(config):
    db = Store(config)
    yield db
    db.close()


@pytest.fixture
def client(config):
    app=create_app(config)
    with TestClient(app,base_url="http://127.0.0.1:8765") as client:
        client.headers['X-GTBMS-Token']=client.get('/api/session').json()['token']
        yield client


def sample(at=None, **changes):
    fields = dict(battery_id="gt50:mock", source="mock", received_at=stamp(at or utcnow()),
                  voltage_v=60, raw_current_a=-10, soc_pct=80, remaining_ah=40, configured_ah=50,
                  cycles=7, cell_voltages_v=[3.75]*16, temperatures_c=[25,27],
                  charge_mos=True, discharge_mos=True, protection_code=0, balancing_mask=0,
                  bms_version=41, current_direction="charge_positive", connection_id="epoch-1", monotonic_s=1000)
    fields.update(changes)
    return Sample(**fields)


def timeline(count=10, step=2, start=None, **changes):
    start = start or utcnow()-timedelta(seconds=count*step)
    return [sample(start+timedelta(seconds=i*step), monotonic_s=1000+i*step, **changes) for i in range(count)]


def response(command, payload, status=0):
    body = bytes([status, len(payload)])+payload
    return bytes([0xDD,command])+body+((-sum(body)) & 0xFFFF).to_bytes(2,"big")+b"\x77"


@pytest.fixture
def frames():
    # Synthetic fixture, NOT a capture from the GT battery.
    basic = bytes.fromhex("1a35fc18138813880007000000010000000029640310020ba50bb9")
    cells = b"".join(int(v).to_bytes(2,"big") for v in [4193]*15+[4195])
    return response(3,basic), response(4,cells)
