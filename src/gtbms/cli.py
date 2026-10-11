from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import struct
import sys
import sysconfig
import warnings

from .config import Config
from .editions import EditionError, load_edition
from .storage import Store


def read_connection_secret():
    if not sys.stdin.isatty():
        raise ValueError("A local interactive terminal is required for hidden password input")
    with warnings.catch_warnings():
        # getpass must not fall back to an echoed input stream.
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass("BMS connection password (hidden; not saved): ")
    if not secret:
        raise ValueError("No password supplied; no connection attempted")
    return secret


def run_plugin_probe(config, prompt_secret):
    from .diagnostics import save_diagnostic
    from .models import stamp
    from .plugin_probe import PluginProbe, private_library_logs
    report = {"connector": "aiobmsble", "version": "0.29.0", "samples_received": 0,
              "ok": False, "stage": "preflight", "hardware_compatibility": "unverified",
              "started_at": stamp(), "connection_total_timeout_seconds": 40,
              "request_timeout_seconds": config.request_timeout_seconds}
    probe = None
    async def run():
        await asyncio.wait_for(probe.connect(), 40)
        for n in range(3):
            sample = await probe.read()
            report["samples_received"] += 1
            print(json.dumps(sample.to_dict(), ensure_ascii=False))
            if n < 2:
                await asyncio.sleep(config.poll_seconds)
    async def session():
        try:
            await run()
        finally:
            try:
                await probe.close()
                report["cleanup_ok"] = True
            except Exception as exc:
                report["cleanup_ok"] = False
                report["disconnect_error_type"] = type(exc).__name__
    try:
        with private_library_logs():
            secret = read_connection_secret() if prompt_secret else ""
            probe = PluginProbe(config, secret=secret)
            secret = ""
            asyncio.run(session())
        report["measurement_ok"] = True
        report["ok"] = report.get("cleanup_ok", False)
    except (Exception, KeyboardInterrupt) as exc:
        # Third-party exception strings can contain device addresses or secrets.
        report["error_type"] = type(exc).__name__
    finally:
        if probe is not None:
            report.update(probe.report())
        if report.get("measurement_ok"):
            report["hardware_compatibility"] = "read_frames_verified_not_measurement_accuracy"
        report["finished_at"] = stamp()
        path = save_diagnostic(report, "aiobmsble")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Report: {path}")
    return 0 if report["ok"] else 1


def environment_report(edition=None):
    import importlib.metadata
    edition = load_edition() if edition is None else edition
    python_platform = sysconfig.get_platform()
    return {"os": platform.platform(), "os_arch": platform.machine(), "python": sys.version,
            "python_arch": python_platform.rsplit("-", 1)[-1], "python_platform": python_platform,
            "python_bits": struct.calcsize("P") * 8, "edition": edition.to_dict(),
            "packages": {p: importlib.metadata.version(p) for p in ("fastapi", "uvicorn", "bleak", "psutil")}}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="GT read-only BMS monitor")
    parser.add_argument("command", choices=["serve", "scan", "probe", "inspect-gatt", "probe-read03-response", "doctor", "backup", "integrity"], nargs="?", default="serve")
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    parser.add_argument("--connector", choices=["native", "aiobmsble"], default="native",
                        help="probe only: optional official JBD diagnostic connector")
    parser.add_argument("--prompt-secret", action="store_true",
                        help="probe/aiobmsble only: hidden session-only connection password")
    args = parser.parse_args()
    if (args.connector != "native" or args.prompt_secret) and args.command != "probe":
        parser.error("connector and prompt-secret are only available for probe")
    if args.prompt_secret and args.connector != "aiobmsble":
        parser.error("prompt-secret requires --connector aiobmsble")
    try:
        edition = load_edition()
    except EditionError as exc:
        parser.error(str(exc))
    if (not edition.can_maintain and args.command in
            ("scan", "probe", "inspect-gatt", "probe-read03-response", "backup")):
        parser.error("edition_maintenance_restricted")
    if args.command == "doctor":
        print(json.dumps(environment_report(edition), ensure_ascii=False, indent=2))
        return
    # WinRT console applications use MTA. No GUI/COM packages are imported.
    sys.coinit_flags = 0
    config = Config.load(args.config)
    if args.command in ("inspect-gatt", "probe-read03-response"):
        from .diagnostics import inspect_gatt, save_diagnostic
        from .models import stamp
        from .plugin_probe import private_library_logs
        report = {}
        interrupted = False
        try:
            with private_library_logs():
                options = {"report_sink": report}
                if args.command == "probe-read03-response":
                    options["read03_response"] = True
                result = asyncio.run(inspect_gatt(config, **options))
                report.update(result)
        except (KeyboardInterrupt, asyncio.CancelledError):
            interrupted = True
            report.update(ok=False, cancelled=True)
            report.setdefault("diagnostic", "read03_response" if args.command == "probe-read03-response" else "gatt_only")
            report.setdefault("stage", "preflight")
            report.setdefault("error_type", "KeyboardInterrupt")
            report.setdefault("cleanup_ok", None)
            report.setdefault("cleanup_status", "unknown")
            report.setdefault("finished_at", stamp())
        path = save_diagnostic(report, "read03-response" if args.command == "probe-read03-response" else "gatt")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"Report: {path}")
        raise SystemExit(130 if interrupted else 0 if report["ok"] else 1)
    if args.command == "scan":
        from .transport import scan_devices
        print(json.dumps(asyncio.run(scan_devices()), ensure_ascii=False, indent=2))
        return
    if args.command == "probe":
        if config.mode != "ble":
            parser.error("probe requires mode=ble; it never reports mock data as hardware validation")
        if args.connector == "aiobmsble":
            raise SystemExit(run_plugin_probe(config, args.prompt_secret))
        from .transport import JbdTransport
        async def probe():
            transport = JbdTransport(config)
            try:
                await asyncio.wait_for(transport.connect(), 40)
                for _ in range(3):
                    data = (await transport.read()).to_dict()
                    print(json.dumps(data, ensure_ascii=False))
                    await asyncio.sleep(config.poll_seconds)
            finally:
                await transport.close()
        asyncio.run(probe())
        return
    if args.command in ("backup", "integrity"):
        store = Store(config)
        try:
            print(store.backup() if args.command == "backup" else store.integrity())
        finally:
            store.close()
        return
    Path("logs").mkdir(exist_ok=True)
    handler = RotatingFileHandler("logs/gtbms.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log = logging.getLogger("gtbms")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    # Do not include Bleak exception strings or HTTP paths containing location input in logs.
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    from .app import create_app
    import uvicorn
    uvicorn.run(create_app(config, args.config, edition=edition), host="127.0.0.1", port=config.port,
                access_log=False, log_level="warning", ws="websockets-sansio", workers=1)


if __name__ == "__main__":
    main()
