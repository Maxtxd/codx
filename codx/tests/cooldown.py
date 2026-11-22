"""Cooldown round-trip: WARP -> Tor (cooldown armed) -> automatic return to WARP.

Runs the real engine against the real WARP client and a real Tor process. Uses a
short cooldown so the return happens inside the test window.
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


def main() -> int:
    cfg = config.load(ROOT / "config.json")
    cfg = json.loads(json.dumps(cfg))
    cfg["rotation"]["cooldown_minutes"] = 0.15  # 9 seconds
    cfg["rotation"]["background_protection"] = True
    logbus.setup(cfg)

    engine = Engine(cfg)
    ok = True
    try:
        engine.start()
        snap = engine.snapshot()
        print(f"start mode={snap['mode']} ip={snap['ip']} protected={snap['protected']}")
        if snap["mode"] != "WARP":
            print("expected WARP at start")
            return 1

        print("forcing Tor with cooldown")
        if not engine.connect_tor(start_cooldown=True):
            print("connect_tor failed")
            return 1
        snap = engine.snapshot()
        print(f"  mode={snap['mode']} ip={snap['ip']} remaining={format_hms(snap['cooldown_remaining_s'])}")
        if snap["mode"] != "COOLDOWN":
            print("expected COOLDOWN")
            ok = False

        deadline = time.monotonic() + 120
        returned = False
        while time.monotonic() < deadline:
            time.sleep(2)
            snap = engine.snapshot()
            print(f"  t+{int(time.monotonic() % 1000):4d} mode={snap['mode']} ip={snap['ip']}")
            if snap["mode"] == "WARP" and snap["protected"]:
                returned = True
                break
        if not returned:
            print("engine never returned to WARP")
            ok = False
        else:
            print(f"returned to WARP, ip={snap['ip']}")
    finally:
        engine.stop()
        print(f"after stop mode={engine.snapshot()['mode']} protected={engine.snapshot()['protected']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
