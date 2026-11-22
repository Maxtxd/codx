"""Live check of the two new pieces: the WARP leak test and the resume pass.

Runs the real transports against the real WARP client, then prints what the
Cloudflare edge said and what the resume pipeline found. Nothing is mocked.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codx import config, logbus, resume  # noqa: E402
from codx.transports import WarpTransport  # noqa: E402


def main() -> int:
    cfg = config.load(ROOT / "config.json")
    logbus.setup(cfg)
    failures = 0

    print("=" * 70)
    print("1. drafts / session database discovery")
    print("=" * 70)
    drafts = resume.drafts_paths(cfg)
    sessions_db = resume.session_db_paths(cfg)
    print(f"drafts.sqlite found : {len(drafts)}")
    for p in drafts:
        print(f"  {p}  ({p.stat().st_size} bytes)")
    print(f"session db found    : {len(sessions_db)}")
    for p in sessions_db:
        print(f"  {p}  ({p.stat().st_size} bytes)")
    if not drafts:
        print("FAIL: no drafts.sqlite — the lockout cannot be cleared")
        failures += 1
    if not sessions_db:
        print("FAIL: no session database — resume cannot be verified")
        failures += 1

    print()
    print("=" * 70)
    print("2. clear the client-side lockout")
    print("=" * 70)
    cleared = resume.clear_free_limit(cfg)
    print(f"deleted : {cleared.deleted}")
    print(f"ok      : {cleared.ok}")
    print(f"summary : {cleared.summary()}")
    if not cleared.ok:
        failures += 1

    print()
    print("=" * 70)
    print("3. recent sessions")
    print("=" * 70)
    recent = resume.recent_sessions(cfg)
    if not recent:
        print("none within the configured window (not a failure)")
    for s in recent:
        print(f"  {s.id}  {s.updated_ms}  {s.title[:40]!r}")
        print(f"    dir: {s.directory}")

    print()
    print("=" * 70)
    print("4. opencode CLI resolution")
    print("=" * 70)
    cli = resume.resolve_cli(cfg)
    print(f"cli: {cli or 'NOT FOUND'}")
    if not cli:
        failures += 1

    print()
    print("=" * 70)
    print("5. WARP leak check through the live proxy")
    print("=" * 70)
    warp = WarpTransport(cfg)
    if not warp.available():
        print("FAIL: warp-cli.exe not found")
        failures += 1
    else:
        try:
            warp.start()
        except Exception as exc:  # noqa: BLE001 - live probe
            print(f"FAIL: WARP would not start: {exc}")
            failures += 1
        else:
            result = warp.check_leak()
            print(f"ok     : {result.get('ok')}")
            print(f"ip     : {result.get('ip')}")
            print(f"warp   : {result.get('warp')}")
            print(f"colo   : {result.get('colo')}")
            print(f"url    : {result.get('url')}")
            print(f"reason : {result.get('reason')}")
            if result.get("ok") is not True:
                print("FAIL: the edge did not confirm WARP is carrying the traffic")
                failures += 1
            try:
                warp.stop()
                warp.restore_original()
            except Exception as exc:  # noqa: BLE001 - teardown
                print(f"teardown warning: {exc}")

    print()
    print("=" * 70)
    print(f"RESULT: {'PASS' if failures == 0 else f'{failures} failure(s)'}")
    print("=" * 70)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
