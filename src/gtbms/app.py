from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import psutil
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .analysis import RideService, predict
from .config import Config
from .editions import Edition, load_edition
from .models import parse_time, stamp, utcnow
from .monitor import Monitor
from .storage import Store
from .sync import SyncWorker
from .transport import scan_devices
from .workers import finish_io

STATIC = Path(__file__).parent/"static"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Interval(Input):
    seconds: float = Field(ge=1, le=10)


class RideStart(Input):
    ambient_c: float | None = Field(default=None, ge=-60, le=60)
    payload_kg: float | None = Field(default=None, ge=0, le=500)


class RideStop(Input):
    distance_km: float | None = Field(default=None, ge=0, le=10000)


class Distance(Input):
    distance_km: float = Field(ge=0, le=10000)


class GpsPoint(Input):
    measured_at: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_m: float | None = Field(default=None, ge=-1000, le=10000)
    speed_kmh: float | None = Field(default=None, ge=0, le=150)
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)


def origin_ok(origin: str | None, host: str, port: int) -> bool:
    if not origin:
        return True  # CLI clients must still present the CSRF token for writes.
    try:
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1") and parsed.port == port and parsed.netloc == host
    except ValueError:
        return False


def safe_csv(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'"+value
    return value


def create_app(config: Config, config_path: Path | None = None, *, monitor_factory=None,
               edition: Edition | None = None) -> FastAPI:
    edition = load_edition() if edition is None else edition
    if not isinstance(edition, Edition):
        raise TypeError("edition must be an Edition")
    config.validate()
    store = Store(config)
    monitor = Monitor(config, store, monitor_factory)
    rides = RideService(store, lambda: monitor.snapshot()["measurement"])
    sync = SyncWorker(store)
    csrf = secrets.token_urlsafe(32) if edition.can_operate else None
    process = psutil.Process()
    process.cpu_percent()
    maintenance_status = {"last_backup_at": None, "error": None, "heartbeat_at": stamp()}

    async def maintenance():
        last_backup = None
        while True:
            try:
                if last_backup is None or (utcnow()-last_backup).total_seconds() >= config.backup_hours*3600:
                    await finish_io(store.backup)
                    last_backup = utcnow()
                    maintenance_status["last_backup_at"] = stamp(last_backup)
                    await finish_io(store.prune)
                maintenance_status["error"] = None
            except Exception as exc:
                maintenance_status["error"] = type(exc).__name__
                logging.getLogger("gtbms").warning("Maintenance failed: %s", type(exc).__name__)
            maintenance_status["heartbeat_at"] = stamp()
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app):
        if store.integrity() != ["ok"]:
            raise RuntimeError("Database integrity failed; restore a verified backup")
        store.event("app_started", f"v{__version__}; {config.mode}")
        tasks = [asyncio.create_task(maintenance()), asyncio.create_task(sync.run())]
        app.state.workers = tasks
        if config.autostart_monitor:
            await monitor.start()
        try:
            yield
        finally:
            await monitor.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.close()

    app = FastAPI(title="GT BMS Monitor", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.monitor, app.state.rides = store, monitor, rides
    app.state.csrf = csrf
    app.state.edition = edition
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.middleware("http")
    async def protect(request: Request, call_next):
        host = request.headers.get("host", "")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if not edition.can_operate:
                return JSONResponse({"detail": "edition_read_only"}, status_code=403,
                                    headers={"Cache-Control": "no-store"})
            if (not edition.can_maintain and request.url.path.rstrip("/") in
                    ("/api/settings/interval", "/api/backup", "/api/scan")):
                return JSONResponse({"detail": "edition_maintenance_restricted"}, status_code=403,
                                    headers={"Cache-Control": "no-store"})
            if not origin_ok(request.headers.get("origin"), host, config.port) or not secrets.compare_digest(request.headers.get("x-gtbms-token", ""), csrf):
                return JSONResponse({"detail": "Local session token required"}, status_code=403)
            length = request.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > 1_048_576):
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def prediction(snapshot):
        sample = snapshot["measurement"]
        soc = sample["soc_pct"] if sample and not sample["quality_flags"] else None
        return predict(config, store.trips(), soc)

    def status():
        snapshot = monitor.snapshot()
        snapshot.update(prediction=prediction(snapshot), active_trip=store.open_trip(),
                        sync={"state": sync.state, "last_success_at": sync.last_success_at},
                        system={"cpu_pct": process.cpu_percent(), "memory_mb": process.memory_info().rss/1048576,
                                "worker_alive": bool(monitor.task and not monitor.task.done()),
                                "maintenance": maintenance_status}, gps_enabled=config.gps_enabled,
                        edition=edition.to_dict())
        return snapshot

    @app.get("/")
    async def index():
        return FileResponse(STATIC/"index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/session")
    async def session():
        return {"token": csrf, "version": __version__, "edition": edition.to_dict()}

    @app.get("/api/status")
    async def get_status():
        return await asyncio.to_thread(status)

    @app.get("/api/health")
    async def health():
        worker_failed = monitor.task is not None and monitor.task.done()
        background_failed = any(task.done() for task in getattr(app.state, "workers", []))
        ok = not worker_failed and not background_failed
        return JSONResponse({"ok": ok, "monitor_state": monitor.state, "worker_failed": worker_failed,
                             "background_failed": background_failed, "mode": config.mode}, status_code=200 if ok else 503)

    @app.post("/api/monitor/start")
    async def start():
        await monitor.start()
        return monitor.snapshot()

    @app.post("/api/monitor/stop")
    async def stop():
        await monitor.stop()
        return monitor.snapshot()

    @app.post("/api/settings/interval")
    async def interval(body: Interval):
        def persist_interval():
            with rides.lock:
                if store.open_trip():
                    raise HTTPException(409, "取得周期は走行セッションの終了後に変更してください")
                updated = replace(config, poll_seconds=body.seconds,
                                  max_gap_seconds=max(config.max_gap_seconds, body.seconds*3),
                                  stale_seconds=max(config.stale_seconds, body.seconds*3))
                updated.validate()
                if config_path:
                    updated.save(config_path)
                # Preserve the shared Config object, but apply only after persistence.
                config.poll_seconds = updated.poll_seconds
                config.max_gap_seconds = updated.max_gap_seconds
                config.stale_seconds = updated.stale_seconds
        await finish_io(persist_interval)
        monitor.change_interval(config.poll_seconds)
        return {"poll_seconds": config.poll_seconds, "max_gap_seconds": config.max_gap_seconds, "stale_seconds": config.stale_seconds}

    @app.get("/api/history")
    async def history(start: str | None = None, end: str | None = None, limit: int = Query(300, ge=1, le=5000)):
        return await asyncio.to_thread(store.samples, start=start, end=end, limit=limit)

    @app.get("/api/events")
    async def events():
        return {"events": store.events(), "gaps": store.gaps()}

    @app.get("/api/trips")
    async def trips():
        return store.trips()

    @app.post("/api/trips/start")
    async def trip_start(body: RideStart):
        return await finish_io(rides.start, body.ambient_c, body.payload_kg)

    @app.post("/api/trips/stop")
    async def trip_stop(body: RideStop):
        return await finish_io(rides.stop, body.distance_km)

    @app.post("/api/trips/{trip_id}/distance")
    async def trip_distance(trip_id: str, body: Distance):
        return await finish_io(rides.set_distance, trip_id, body.distance_km)

    @app.post("/api/trips/{trip_id}/gps")
    async def gps(trip_id: str, body: GpsPoint):
        if not config.gps_enabled:
            raise HTTPException(409, "GPS disabled in local configuration")
        if parse_time(body.measured_at) > utcnow():
            raise ValueError("Future GPS timestamp")
        await finish_io(rides.save_gps, trip_id, body.model_dump())
        return {"saved": True}

    @app.get("/api/trips/{trip_id}/gps")
    async def get_gps(trip_id: str):
        if not edition.can_view_gps:
            raise HTTPException(403, "edition_read_only")
        return await asyncio.to_thread(store.gps_points, trip_id)

    @app.get("/api/predictions")
    async def predictions():
        return store.predictions()

    @app.get("/api/diagnostics")
    async def diagnostics():
        with store.lock:
            count = store.db.execute("SELECT COUNT(*) FROM samples WHERE battery_id=?", (config.key,)).fetchone()[0]
            pending = store.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        return {"version": __version__, "integrity": await asyncio.to_thread(store.integrity),
                "sample_count": count, "pending_sync": pending, "mode": config.mode,
                "protocol": "JBD DD/A5 read 03/04", "bms_command_whitelist": [3, 4],
                "hardware_validation": "未検証", "current_direction": config.current_direction,
                "sensor_mapping": "NTC番号のみ。セル/BMS/MOSの設置場所は未確認", "maintenance": maintenance_status}

    @app.post("/api/backup")
    async def backup():
        result = await asyncio.to_thread(store.backup)
        return {"filename": result.name, "integrity": "ok"}

    @app.post("/api/scan")
    async def scan():
        if monitor.task and not monitor.task.done():
            raise HTTPException(409, "スキャン前に監視を停止してください")
        try:
            return await scan_devices()
        except Exception as exc:
            store.event("scan_error", type(exc).__name__)
            raise HTTPException(503, "BLE scan failed; check Windows Bluetooth and permissions") from None

    @app.get("/api/export/{fmt}")
    async def export(fmt: str, start: str | None = None, end: str | None = None, dataset: str = "samples"):
        if not edition.can_export:
            raise HTTPException(403, "edition_read_only")
        if fmt not in ("csv", "json") or dataset not in ("samples", "trips", "events", "predictions", "gps", "gaps"):
            raise HTTPException(400, "Unsupported export")
        if start:
            start = stamp(parse_time(start))
        end = stamp(parse_time(end)) if end else stamp()
        def chunks():
            with store.export_snapshot(dataset, start, end) as rows:
                if fmt == "json":
                    yield '['
                    first = True
                    for row in rows():
                        yield ('' if first else ',')+json.dumps(row, ensure_ascii=False, allow_nan=False)
                        first = False
                    yield ']'
                else:
                    # Schema fields can grow over time; preserve the union of
                    # columns without holding the whole export in memory.
                    fields = dict.fromkeys(key for row in rows() for key in row)
                    output = io.StringIO(newline="")
                    writer = csv.DictWriter(output, fieldnames=list(fields))
                    yield '\ufeff'
                    writer.writeheader()
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate(0)
                    for row in rows():
                        writer.writerow({k: safe_csv(v) for k,v in row.items()})
                        yield output.getvalue()
                        output.seek(0)
                        output.truncate(0)
        return StreamingResponse(chunks(), media_type="application/json" if fmt == "json" else "text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="gtbms-{dataset}.{fmt}"'})

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        if not origin_ok(ws.headers.get("origin"), ws.headers.get("host", ""), config.port):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            while True:
                await ws.send_json(await asyncio.to_thread(status))
                await asyncio.sleep(1)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app
