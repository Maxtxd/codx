"""End-to-end: a rotation must clear the lockout and resume the chat by itself.

The engine is given a config whose session window is one minute and whose target
count is one, and a throwaway session is created immediately before the rotation.
That combination makes it structurally impossible for the test to nudge a live
chat, while still exercising the production path:

    get_new_ip() -> _record_rotation() -> resume_in_background() -> resume()

Assertions are read back out of SQLite and out of the engine snapshot. Nothing is
mocked.
"""
from __future__ import annotations

import copy
import os
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codx import config, logbus, resume  # noqa: E402
from codx.engine import Engine  # noqa: E402

DB = Path(os.path.expanduser("~")) / ".local" / "share" / "opencode" / "opencode.db"
WORKDIR = r"C:\Users\A\Downloads\OJBS-OPENCODE"


def user_rows(session_id: str) -> int:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM session_message WHERE session_id = ? AND type = 'user'",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def wait_for(predicate, timeout_s: float, poll_s: float = 1.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll_s)
    return None


def main() -> int:
    base = config.load(ROOT / "config.json")
    logbus.setup(base)

    cfg = copy.deepcopy(base)
    # One-minute window, one target: only the session created seconds ago can be
    # picked, so a live conversation cannot be touched by this test.
    cfg["resume"]["recent_minutes"] = 1
    cfg["resume"]["max_sessions"] = 1
    cfg["resume"]["auto_after_rotation"] = True
    cfg["resume"]["verify_timeout_s"] = 30
    cfg["resume"]["verify_poll_s"] = 1

    failures = 0
    session_id = f"ses_codxe2e{secrets.token_hex(8)}"
    cli = resume.resolve_cli(base)
    if not cli:
        print("FAIL: no CLI resolved")
        return 1

    engine = Engine(cfg)
    try:
        print("--- seed the throwaway session ---")
        subprocess.run(
            [cli, "run", "--session", session_id, "--title", "codx e2e probe", "--auto", "probe"],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        seeded = user_rows(session_id)
        print(f"  {session_id}  user rows: {seeded}")
        if seeded < 1:
            print("FAIL: seed session was not created")
            return 1

        print()
        print("--- start engine ---")
        engine.start()
        snap = engine.snapshot()
        print(f"  mode={snap['mode']} ip={snap['ip']} protected={snap['protected']}")
        if snap["mode"] != "WARP":
            print("FAIL: engine did not reach WARP")
            failures += 1

        print()
        print("--- rotation (this triggers the resume) ---")
        before_rows = user_rows(session_id)
        before_count = engine.snapshot()["new_ip_count"]
        ok, msg = engine.get_new_ip("e2e")
        print(f"  result: {ok} {msg}")
        if not ok:
            print("FAIL: rotation failed")
            failures += 1
        after_count = engine.snapshot()["new_ip_count"]
        if after_count != before_count + 1:
            print("FAIL: rotation not recorded")
            failures += 1

        print()
        print("--- waiting for the background resume ---")
        report = wait_for(lambda: engine.snapshot().get("last_resume"), 180)
        snap = engine.snapshot()
        print(f"  resume_running: {snap.get('resume_running')}")
        print(f"  last_resume   : {report!r}")
        if not report:
            print("FAIL: the resume never ran")
            failures += 1

        print()
        print("--- did the chat actually get nudged? ---")
        target = before_rows

        def grew() -> int | None:
            now = user_rows(session_id)
            return now if now > target else None

        after_rows = wait_for(grew, 60)
        print(f"  user rows: {before_rows} -> {after_rows or user_rows(session_id)}")
        if not after_rows:
            print("FAIL: no new user row in the throwaway session")
            failures += 1
        else:
            print("  PASS: the rotation resumed the chat without being asked")

        print()
        print("--- lockout clear ---")
        cleared = resume.clear_free_limit(cfg)
        print(f"  ok={cleared.ok} summary={cleared.summary()}")
        if not cleared.ok:
            print("FAIL: lockout clear reported errors")
            failures += 1
    finally:
        print()
        print("--- stop engine ---")
        engine.stop()
        time.sleep(1)
        final = engine.snapshot()
        print(f"  mode={final['mode']} protected={final['protected']}")

        print("--- cleanup ---")
        try:
            subprocess.run(
                [cli, "session", "delete", session_id],
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001 - cleanup
            print(f"  delete failed: {exc}")

    print()
    print(f"RESULT: {'PASS' if failures == 0 else f'{failures} failure(s)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
