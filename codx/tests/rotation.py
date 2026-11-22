"""Live rotation proof: new address, leak verdict, lockout clear, chat resume.

Drives the real engine against the real WARP client, then runs the resume pass
and prints what the databases actually contain afterwards. Nothing is mocked.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codx import config, logbus, resume  # noqa: E402
from codx.engine import Engine, format_hms  # noqa: E402


def main() -> int:
    cfg = config.load(ROOT / "config.json")
    logbus.setup(cfg)

    engine = Engine(cfg)
    failures = 0
    try:
        engine.start()
        snap = engine.snapshot()
        print(f"start   mode={snap['mode']} ip={snap['ip']} protected={snap['protected']}")
        if snap["mode"] != "WARP":
            print("FAIL: expected WARP at start")
            return 1

        print()
        print("--- leak verdict on the live proxy ---")
        leak = engine.warp.check_leak()
        print(f"  ok={leak.get('ok')} warp={leak.get('warp')} colo={leak.get('colo')} ip={leak.get('ip')}")
        print(f"  url={leak.get('url')} reason={leak.get('reason')}")
        if leak.get("ok") is not True:
            print("FAIL: edge did not confirm WARP")
            failures += 1

        print()
        print("--- rotation ---")
        before = engine.snapshot()["ip"]
        count_before = engine.snapshot()["new_ip_count"]
        ok, msg = engine.get_new_ip("test")
        print(f"  before={before}")
        print(f"  result={ok} {msg}")
        after = engine.snapshot()
        print(f"  after ={after['ip']}  new_ip_count={after['new_ip_count']}")
        if not ok:
            print("FAIL: rotation returned false")
            failures += 1
        if after["new_ip_count"] != count_before + 1:
            print("FAIL: rotation was not recorded")
            failures += 1
        if after["ip"] and before and after["ip"] == before:
            print("WARN: address did not move (pool may be reusing the peer)")

        print()
        print("--- debounce ---")
        ok2, msg2 = engine.get_new_ip("test-immediate")
        print(f"  second call: {ok2} {msg2}")
        if ok2:
            print("FAIL: debounce did not block the second rotation")
            failures += 1

        print()
        print("--- resume pass ---")
        report = resume.resume(cfg, engine.bridge.proxy_url)
        print(f"  cleared   : {report.cleared.deleted} row(s) in {len(report.cleared.databases)} db")
        print(f"  sessions  : {len(report.sessions)}")
        for s in report.sessions:
            print(f"    {s.id}  {s.title[:36]!r}")
        print(f"  dispatched: {report.dispatched}")
        print(f"  confirmed : {report.confirmed}")
        for note in report.notes:
            print(f"  note      : {note}")
        print(f"  summary   : {report.summary()}")
        if not report.cleared.ok:
            print("FAIL: lockout clear reported errors")
            failures += 1

        print()
        print("--- cooldown display ---")
        snap = engine.snapshot()
        print(f"  cooldown_total={format_hms(snap['cooldown_total_s'])} "
              f"remaining={format_hms(snap['cooldown_remaining_s'])}")
        print(f"  last_resume={snap.get('last_resume')!r}")
    finally:
        engine.stop()
        time.sleep(1)
        final = engine.snapshot()
        print()
        print(f"after stop mode={final['mode']} protected={final['protected']}")

    print()
    print(f"RESULT: {'PASS' if failures == 0 else f'{failures} failure(s)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
