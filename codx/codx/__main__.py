"""Entry point: python -m codx [--headless] [--selftest] [--config PATH]."""
from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from pathlib import Path

from . import config, logbus
from .engine import Engine, format_hms

MUTEX_NAME = "Global\\codx.IpRotation.SingleInstance"


def acquire_single_instance() -> bool:
    """Create-or-open a named mutex so two copies cannot fight over ports."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    if already:
        return False
    # keep the handle alive for the process lifetime
    globals()["_MUTEX_HANDLE"] = handle
    return True


def selftest(cfg: dict, cfg_path: Path) -> int:
    """Offline checks: config shape, executables, ports, module imports."""
    from . import httpc, socks  # noqa: F401 - import proves the module loads
    from .bridge import HttpBridge
    from .transports import TorTransport, WarpTransport

    ok = True
    print(f"{config.APP_NAME} {config.APP_VERSION} self-test")
    print(f"  config        : {cfg_path}")
    print(f"  python        : {sys.version.split()[0]} ({sys.executable})")

    warp = WarpTransport(cfg)
    tor = TorTransport(cfg)
    print(f"  warp-cli      : {warp.cli or 'NOT FOUND'}")
    print(f"  tor.exe       : {tor.exe or 'NOT FOUND (fallback only)'}")
    print(f"  bridge port   : {config.get(cfg, 'bridge.listen_host')}:{config.get(cfg, 'bridge.listen_port')}")
    print(f"  warp socks    : {config.get(cfg, 'warp.proxy_host')}:{config.get(cfg, 'warp.proxy_port')}")
    print(f"  tor socks     : {config.get(cfg, 'tor.socks_host')}:{config.get(cfg, 'tor.socks_port')}")

    if not warp.available():
        print("  WARN          : warp-cli.exe missing — WARP transport disabled")
        ok = False
    if not tor.available():
        print("  WARN          : tor.exe missing — Tor fallback disabled")

    bridge = HttpBridge(cfg)
    try:
        bridge.start()
        print(f"  bridge bind   : OK ({bridge.proxy_url})")
    except Exception as exc:
        print(f"  bridge bind   : FAILED — {exc}")
        ok = False
    finally:
        bridge.stop()

    try:
        settings = warp.settings() if warp.available() else {}
        if settings:
            print(f"  warp mode now : {settings.get('operation_mode')}")
            print(f"  warp protocol : {settings.get('warp_tunnel_protocol')}")
    except Exception as exc:
        print(f"  warp settings : unavailable ({exc})")

    print("  result        :", "PASS" if ok else "PASS WITH WARNINGS")
    return 0 if ok else 2


def run_headless(cfg: dict) -> int:
    """No window: bridge + transports + watchdog, status printed periodically."""
    log = logbus.logger("headless")
    engine = Engine(cfg)
    stop = threading.Event()

    def report() -> None:
        while not stop.wait(15):
            snap = engine.snapshot()
            log.info(
                "mode=%s protected=%s ip=%s new_ips=%d cooldown=%s bridge_req=%d",
                snap["mode"],
                snap["protected"],
                snap["ip"] or "unknown",
                snap["new_ip_count"],
                format_hms(snap["cooldown_remaining_s"]),
                snap["bridge"]["requests"],
            )

    engine.start()
    threading.Thread(target=report, name="headless-report", daemon=True).start()
    print(f"{config.APP_NAME} running headless — proxy {engine.bridge.proxy_url}. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        stop.set()
        engine.stop()
    return 0


def run_gui(cfg: dict, cfg_path: Path) -> int:
    from .ui import CodxUI

    engine = Engine(cfg)
    ui = CodxUI(cfg, engine, cfg_path)

    def boot() -> None:
        try:
            engine.start()
        except Exception as exc:
            logbus.logger("app").error("engine failed to start: %s", exc)
        ui.root.after(0, lambda: None)

    threading.Thread(target=boot, name="engine-boot", daemon=True).start()
    ui.run()

    # The tk mainloop returns when the root window is destroyed — which the tray
    # can do without going through _quit. Background protection means the daemon
    # outlives the window, so keep the process alive instead of letting Python
    # exit and orphan the transports.
    while not ui.closing and engine.background:
        time.sleep(0.5)
    if not ui.closing:
        ui._quit()
    engine.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codx",
        description=f"{config.APP_NAME} — keeps a target app online by rotating its egress IP",
    )
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--headless", action="store_true", help="run without a window")
    parser.add_argument("--selftest", action="store_true", help="check config, tools and ports")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    args = parser.parse_args(argv)

    if args.version:
        print(f"{config.APP_NAME} {config.APP_VERSION}")
        return 0

    cfg_path = Path(args.config) if args.config else config.DEFAULT_CONFIG_PATH
    try:
        cfg = config.load(cfg_path)
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    config.ensure_dirs(cfg)
    logbus.setup(cfg)
    logbus.logger("app").info("%s %s starting", config.APP_NAME, config.APP_VERSION)

    if args.selftest:
        return selftest(cfg, cfg_path)

    if not acquire_single_instance():
        print(f"{config.APP_NAME} is already running.", file=sys.stderr)
        return 1

    try:
        return run_headless(cfg) if args.headless else run_gui(cfg, cfg_path)
    except Exception as exc:
        logbus.logger("app").exception("fatal: %s", exc)
        print(f"fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
