"""Edition gates use temporary stores and fake managers; no hardware/network."""
from dataclasses import FrozenInstanceError
import json
import sys
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from gtbms import app as app_module, mobile, network
from gtbms.editions import Edition, EditionError, load_edition


FULL = Edition("full_access")
LIMITED_IDS = ["corporate", "individual"]
MAIN_BASE = "http://127.0.0.1:8765"
NETWORK_BASE = "http://127.0.0.1:8767"


@pytest.mark.parametrize("edition_id", ["full_access", *LIMITED_IDS])
def test_capabilities_are_derived_from_valid_immutable_edition(edition_id):
    edition = Edition(edition_id)
    data = edition.to_dict()
    assert set(data) == {"id", "label", "can_operate", "can_manage_network", "can_export", "can_view_gps", "can_maintain"}
    assert data["id"] == edition_id and isinstance(data["label"], str)
    assert data["can_operate"] is data["can_export"] is data["can_view_gps"] is True
    assert data["can_manage_network"] is data["can_maintain"] is (edition_id == "full_access")
    data["can_operate"] = False
    assert edition.can_operate is True
    with pytest.raises(FrozenInstanceError):
        edition.id = "full_access"


@pytest.mark.parametrize("value", [None, True, 1, {}, [], "unknown", "FULL_ACCESS", "", "personal_full", "public_basic"])
def test_unknown_or_retired_edition_cannot_be_constructed(value):
    with pytest.raises(EditionError):
        Edition(value)


@pytest.mark.parametrize("edition_id", ["full_access", *LIMITED_IDS])
def test_load_valid_packaged_schema(tmp_path, edition_id):
    path = tmp_path / "edition.json"
    path.write_text(json.dumps({"schema_version": 1, "id": edition_id}), encoding="utf-8")
    assert load_edition(path) == Edition(edition_id)


@pytest.mark.parametrize("contents", [
    b"", b"{", b"null", b"[]", b"true", b"{}", b"\xff",
    b'{"schema_version":1}', b'{"id":"full_access"}',
    b'{"schema_version":true,"id":"full_access"}',
    b'{"schema_version":1.0,"id":"full_access"}',
    b'{"schema_version":2,"id":"full_access"}',
    b'{"schema_version":1,"id":"unrecognized-private-content"}',
    b'{"schema_version":1,"id":null}',
    b'{"schema_version":1,"id":"full_access","can_operate":true}',
    b'{"schema_version":1,"id":"corporate","id":"full_access"}',
])
def test_invalid_manifest_fails_closed_without_echoing_content(tmp_path, contents):
    path = tmp_path / "edition.json"
    path.write_bytes(contents)
    with pytest.raises(EditionError) as error:
        load_edition(path)
    assert "unrecognized-private-content" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


def test_missing_manifest_fails_closed(tmp_path):
    with pytest.raises(EditionError):
        load_edition(tmp_path / "missing.json")


def test_checked_in_edition_keeps_full_access():
    assert load_edition() == FULL


@pytest.mark.parametrize("module", [app_module, network])
def test_invalid_manifest_prevents_store_or_manager_construction(config, monkeypatch, module):
    def missing():
        raise EditionError("missing manifest")
    def forbidden(*args, **kwargs):
        raise AssertionError("No operational object may be created before edition validation")
    monkeypatch.setattr(module, "load_edition", missing)
    if module is app_module:
        monkeypatch.setattr(module, "Store", forbidden)
        factory = lambda: module.create_app(config)
    else:
        monkeypatch.setattr(module, "NetworkManager", forbidden)
        factory = module.create_network_app
    with pytest.raises(EditionError):
        factory()


@pytest.mark.parametrize("module", [app_module, network])
def test_test_injection_requires_an_edition_object(config, module):
    with pytest.raises(TypeError):
        if module is app_module:
            module.create_app(config, edition={"id": "full_access"})
        else:
            module.create_network_app(edition={"id": "full_access"})


@pytest.fixture(params=LIMITED_IDS)
def limited_client(config, request):
    app = app_module.create_app(config, edition=Edition(request.param))
    with TestClient(app, base_url=MAIN_BASE) as client:
        client.headers["X-GTBMS-Token"] = client.get("/api/session").json()["token"]
        yield client


def test_limited_session_status_and_websocket_agree(limited_client):
    session = limited_client.get("/api/session").json()
    assert session["token"] and session["version"]
    edition = limited_client.app.state.edition.to_dict()
    assert session["edition"] == edition
    status = limited_client.get("/api/status").json()
    assert status["edition"] == edition and "token" not in status
    with limited_client.websocket_connect("ws://127.0.0.1:8765/ws") as ws:
        message = ws.receive_json()
        assert message["edition"] == edition and "token" not in message


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/api/status", "/api/health", "/api/history",
    "/api/events", "/api/trips", "/api/predictions", "/api/diagnostics", "/api/export/json",
    "/api/export/csv?dataset=trips"])
def test_limited_normal_read_and_export_remain_available(limited_client, path):
    assert limited_client.get(path).status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_limited_maintenance_denied_before_csrf_and_validation(limited_client, method):
    for path in ("/api/settings/interval", "/api/settings/interval/", "/api/scan", "/api/backup"):
        response = limited_client.request(method, path, json={"id": "full_access"},
            headers={"X-GTBMS-Token": "", "X-GTBMS-Edition": "full_access"})
        assert response.status_code == 403
        assert response.json() == {"detail": "edition_maintenance_restricted"}
    assert limited_client.app.state.monitor.task is None


def test_limited_normal_monitor_controls_require_csrf(limited_client, monkeypatch):
    monitor = limited_client.app.state.monitor
    start, stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(monitor, "start", start)
    monkeypatch.setattr(monitor, "stop", stop)
    assert limited_client.post("/api/monitor/start", headers={"X-GTBMS-Token": ""}).status_code == 403
    assert limited_client.post("/api/monitor/start").status_code == 200
    assert limited_client.post("/api/monitor/stop").status_code == 200
    start.assert_awaited_once()
    stop.assert_awaited_once()


def test_limited_ride_and_manual_distance_remain_operable(limited_client):
    started = limited_client.post("/api/trips/start", json={})
    assert started.status_code == 200
    trip_id = started.json()["id"]
    assert limited_client.post("/api/trips/stop", json={"distance_km": 2}).status_code == 200
    assert limited_client.post(f"/api/trips/{trip_id}/distance", json={"distance_km": 3}).status_code == 200
    assert limited_client.get("/api/trips").json()[0]["distance_km"] == 3
    assert limited_client.get(f"/api/trips/{trip_id}/gps").status_code == 200


def test_limited_gps_write_uses_normal_local_configuration_gate(limited_client):
    # Capability is allowed; GPS opt-in remains required for every edition.
    response = limited_client.post("/api/trips/id/gps", json={
        "measured_at": "2026-01-01T00:00:00Z", "latitude": 35, "longitude": 139})
    assert response.status_code == 409
    assert response.json()["detail"] == "GPS disabled in local configuration"


def test_http_query_cookie_and_headers_cannot_select_full_edition(limited_client):
    response = limited_client.get("/api/session?edition=full_access", headers={
        "X-GTBMS-Edition": "full_access", "Cookie": "edition=full_access"})
    assert response.json()["edition"] == limited_client.app.state.edition.to_dict()
    assert limited_client.post("/api/edition", json={"id": "full_access"}).status_code == 404
    assert limited_client.post("/api/settings/interval?edition=full_access", json={"seconds": 3}).status_code == 403


def test_full_token_maintenance_and_export_preserved(config):
    app = app_module.create_app(config, edition=FULL)
    with TestClient(app, base_url=MAIN_BASE) as client:
        session = client.get("/api/session").json()
        assert session["edition"] == FULL.to_dict() and session["token"]
        assert client.post("/api/settings/interval", json={"seconds": 3}).status_code == 403
        response = client.post("/api/settings/interval", json={"seconds": 3},
            headers={"X-GTBMS-Token": session["token"]})
        assert response.status_code == 200 and response.json()["poll_seconds"] == 3
        assert client.get("/api/export/json").status_code == 200
        trip = client.post("/api/trips/start", json={}, headers={"X-GTBMS-Token": session["token"]}).json()
        assert client.get(f"/api/trips/{trip['id']}/gps").status_code == 200


class FakeManager:
    def __init__(self):
        self.calls = []

    async def start(self):
        self.calls.append("start")

    async def close(self):
        self.calls.append("close")

    def state(self):
        return {"active_mode": "unknown", "viewer": {"state": "stopped"}}

    async def refresh(self):
        self.calls.append("refresh")


@pytest.fixture(params=LIMITED_IDS)
def limited_network(request):
    manager = FakeManager()
    app = network.create_network_app(manager=manager, edition=Edition(request.param))
    with TestClient(app, base_url=NETWORK_BASE, client=("127.0.0.1", 3210)) as client:
        yield client, manager
    assert manager.calls == []


def test_limited_network_lifespan_does_not_restore_or_stop_viewer(limited_network):
    client, manager = limited_network
    assert manager.calls == []
    session = client.get("/api/session").json()
    assert session["token"] and session["edition"] == client.app.state.edition.to_dict()
    assert client.get("/api/state").json()["edition"] == session["edition"]
    for path in ("/", "/api/health", "/static/network.js", "/static/network.css"):
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_limited_network_denies_mutations_even_with_valid_csrf(limited_network, method):
    client, manager = limited_network
    token = client.get("/api/session").json()["token"]
    for path in ("/api/profile", "/api/switch", "/api/refresh", "/api/hotspot-settings", "/api/edition"):
        response = client.request(method, path, json={"id": "full_access"}, headers={"X-GTBMS-Token": token})
        assert response.status_code == 403
        assert response.json() == {"detail": "edition_network_restricted"}
        assert response.headers["cache-control"] == "no-store"
    assert manager.calls == []


def test_limited_network_connection_never_reads_credential_page(limited_network):
    client, _ = limited_network
    # FakeManager deliberately has no viewer/files; the gate must run first.
    for path in ("/connection", "/connection/", "/connection?edition=full_access"):
        response = client.get(path)
        assert response.status_code == 403
        assert response.json() == {"detail": "edition_network_restricted"}


def test_limited_network_retains_loopback_guard():
    manager = FakeManager()
    app = network.create_network_app(manager=manager, edition=Edition("corporate"))
    with TestClient(app, base_url=NETWORK_BASE, client=("192.168.1.5", 3210)) as client:
        assert client.get("/api/state").status_code == 403
    assert manager.calls == []


def test_full_network_lifecycle_and_token_write_preserved():
    manager = FakeManager()
    app = network.create_network_app(manager=manager, edition=FULL)
    with TestClient(app, base_url=NETWORK_BASE, client=("127.0.0.1", 3210)) as client:
        assert manager.calls == ["start"]
        session = client.get("/api/session").json()
        assert session["edition"] == FULL.to_dict() and session["token"]
        assert client.post("/api/refresh", json={}).status_code == 403
        assert client.post("/api/refresh", json={}, headers={"X-GTBMS-Token": session["token"]}).status_code == 200
    assert manager.calls == ["start", "refresh", "close"]


@pytest.mark.parametrize("edition_id", LIMITED_IDS)
@pytest.mark.parametrize("command", ["scan", "probe", "inspect-gatt", "probe-read03-response", "backup"])
def test_limited_cli_maintenance_rejected_before_config_or_ble_imports(monkeypatch, capsys, edition_id, command):
    import builtins
    from gtbms import cli
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.split(".")[-1] in ("bleak", "transport", "diagnostics", "plugin_probe"):
            raise AssertionError("BLE/diagnostic import must not run")
        return original_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("Configuration and storage must not be touched")
    monkeypatch.setattr(cli, "load_edition", lambda: Edition(edition_id))
    monkeypatch.setattr(cli.Config, "load", forbidden)
    monkeypatch.setattr(cli, "Store", forbidden)
    monkeypatch.setattr(cli.sys, "argv", ["gtbms", command])
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "edition_maintenance_restricted" in capsys.readouterr().err


@pytest.mark.parametrize("edition_id", ["full_access", *LIMITED_IDS])
def test_cli_doctor_displays_edition_without_loading_configuration(monkeypatch, capsys, edition_id):
    from gtbms import cli
    def forbidden(*args):
        raise AssertionError("doctor must not load configuration")
    monkeypatch.setattr(cli, "load_edition", lambda: Edition(edition_id))
    monkeypatch.setattr(cli.Config, "load", forbidden)
    monkeypatch.setattr(cli.sys, "argv", ["gtbms", "doctor"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["edition"] == Edition(edition_id).to_dict()


@pytest.mark.parametrize("edition_id", LIMITED_IDS)
def test_limited_cli_integrity_allowed_with_temporary_database(monkeypatch, capsys, config, edition_id):
    from gtbms import cli
    monkeypatch.setattr(cli, "load_edition", lambda: Edition(edition_id))
    monkeypatch.setattr(cli.Config, "load", lambda path: config)
    monkeypatch.setattr(cli.sys, "argv", ["gtbms", "integrity"])
    cli.main()
    assert "['ok']" in capsys.readouterr().out


def test_cli_missing_edition_rejects_doctor_and_config(monkeypatch, capsys):
    from gtbms import cli
    def missing():
        raise EditionError("Application edition is missing or invalid")
    monkeypatch.setattr(cli, "load_edition", missing)
    monkeypatch.setattr(cli.sys, "argv", ["gtbms", "doctor"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "Application edition is missing or invalid" in capsys.readouterr().err


@pytest.mark.parametrize("edition_id", LIMITED_IDS)
def test_limited_cli_serve_passes_validated_edition_without_starting_server(monkeypatch, tmp_path, config, edition_id):
    from gtbms import cli
    import uvicorn
    edition = Edition(edition_id)
    fake_app = object()
    calls = []
    def factory(actual_config, path, **kwargs):
        assert actual_config is config
        assert kwargs["edition"] == edition
        return fake_app
    def run(app, **kwargs):
        assert app is fake_app
        assert kwargs["host"] == "127.0.0.1"
        calls.append("server_configured")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load_edition", lambda: edition)
    monkeypatch.setattr(cli.Config, "load", lambda path: config)
    monkeypatch.setattr(cli.sys, "argv", ["gtbms", "serve"])
    monkeypatch.setattr(app_module, "create_app", factory)
    monkeypatch.setattr(uvicorn, "run", run)
    logger = cli.logging.getLogger("gtbms")
    original_handlers = list(logger.handlers)
    try:
        cli.main()
    finally:
        for handler in list(logger.handlers):
            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()
    assert calls == ["server_configured"]


@pytest.mark.parametrize("edition_id", LIMITED_IDS)
@pytest.mark.parametrize("command", ["init", "share", "serve"])
def test_limited_mobile_cli_rejects_before_private_files_or_interfaces(monkeypatch, tmp_path, capsys, edition_id, command):
    def forbidden(*args, **kwargs):
        raise AssertionError("Private configuration and interfaces must not be touched")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mobile, "load_edition", lambda: Edition(edition_id))
    monkeypatch.setattr(mobile.MobileSettings, "load", forbidden)
    monkeypatch.setattr(mobile.MobileSettings, "require_local_interface", forbidden)
    monkeypatch.setattr(mobile, "atomic_private_write", forbidden)
    monkeypatch.setattr(mobile, "connection_page", forbidden)
    arguments = ["gtbms.mobile", command]
    if command == "init":
        arguments += ["--host", "192.168.50.10", "--prefix-length", "24"]
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit) as error:
        mobile.main()
    assert error.value.code == 2
    assert "edition_network_restricted" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_mobile_cli_invalid_edition_is_fixed_error_before_settings(monkeypatch, capsys):
    import sys
    def invalid():
        raise EditionError("arbitrary-private-manifest-content")
    def forbidden(*args, **kwargs):
        raise AssertionError("Private settings must not be read")
    monkeypatch.setattr(mobile, "load_edition", invalid)
    monkeypatch.setattr(mobile.MobileSettings, "load", forbidden)
    monkeypatch.setattr(sys, "argv", ["gtbms.mobile", "serve"])
    with pytest.raises(SystemExit) as error:
        mobile.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "application_edition_invalid" in output.err
    assert "arbitrary-private-manifest-content" not in output.out + output.err


@pytest.mark.parametrize("edition_id", LIMITED_IDS)
def test_limited_mobile_factory_rejects_before_feed_or_settings(monkeypatch, edition_id):
    def forbidden(*args, **kwargs):
        raise AssertionError("Feed must not be created")
    monkeypatch.setattr(mobile, "SnapshotFeed", forbidden)
    with pytest.raises(EditionError, match="edition_network_restricted"):
        mobile.create_mobile_app(None, edition=Edition(edition_id))


def test_mobile_factory_missing_manifest_fails_before_settings(monkeypatch):
    def invalid():
        raise EditionError("Application edition is missing or invalid")
    monkeypatch.setattr(mobile, "load_edition", invalid)
    with pytest.raises(EditionError):
        mobile.create_mobile_app(None)
