r"""Live smoke test: bring the engine up, rotate once, tear down.

    py -3.14 tests\smoke.py

Prints a snapshot at each stage. Safe: the engine restores the WARP mode it
found on exit, and only the bridge port is ever opened.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codx import config, logbus  # noqa: E402
from codx.engine import Engine, format_hms  # noqa: E402


def show(label: str, snap: dict) -> None:
    print(f"\n--- {label} ---")
    print(f"  mode        : {snap['mode']}")
    print(f"  protected   : {snap['protected']}")
    print(f"  ip          : {snap['ip']}")
    print(f"  upstream    : {snap['upstream']}")
    print(f"  bridge      : {snap['proxy_url']} running={snap['bridge_running']}")
    print(f"  transport   : {json.dumps(snap['transport'], default=str)}")
    print(f"  new ips     : {snap['new_ip_count']}")
    print(f"  cooldown    : {format_hms(snap['cooldown_remaining_s'])} / {format_hms(snap['cooldown_total_s'])}")
    print(f"  last error  : {snap['last_error']}")


def main() -> int:
    cfg = config.load(ROOT / "config.json")
    config.ensure_dirs(cfg)
    logbus.setup(cfg)

    engine = Engine(cfg)
    try:
        engine.start()
        show("after start", engine.snapshot())

        health = engine.health_check()
        print(f"\n  health_check: {health}")

        probe = engine.probe()
        print(f"  probe       : status={probe['status']} limited={probe['limited']} {probe['detail']}")

        ok, msg = engine.get_new_ip("smoke test")
        print(f"  rotation    : {ok} — {msg}")
        show("after rotation", engine.snapshot())

        ok2, msg2 = engine.get_new_ip("debounce check")
        print(f"  debounce    : {ok2} — {msg2}")
    finally:
        engine.stop()
        show("after stop", engine.snapshot())
    print("\nsmoke test finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
