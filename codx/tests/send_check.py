"""Exercise resume.send_continue() itself, on a throwaway session.

The unit under test is the production path: CLI discovery, `--auto` injection,
the proxy environment, exit-code classification and the arrival check. A session
created and deleted inside the test means no live chat receives a stray message.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codx import config, logbus, resume  # noqa: E402

DB = Path(os.path.expanduser("~")) / ".local" / "share" / "opencode" / "opencode.db"
WORKDIR = r"C:\Users\A\Downloads\OJBS-OPENCODE"


def session_exists(session_id: str) -> bool:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT 1 FROM session_v2 WHERE id = ?", (session_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def message_count(session_id: str) -> int:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM session_message WHERE session_id = ? AND type = 'user'",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def create_session(cli: str, session_id: str) -> bool:
    """Create the session row without needing a successful model turn."""
    proc = subprocess.run(
        [cli, "run", "--session", session_id, "--title", "codx resume probe", "--auto", "probe"],
        cwd=WORKDIR,
        capture_output=True,
        text=True,
        timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # A spent meter still stores the session and the user row; that is a pass.
    return session_exists(session_id) or proc.returncode == 0


def main() -> int:
    cfg = config.load(ROOT / "config.json")
    logbus.setup(cfg)

    failures = 0
    session_id = f"ses_codxresume{secrets.token_hex(8)}"
    cli = resume.resolve_cli(cfg)
    print(f"cli      : {cli}")
    print(f"session  : {session_id}")
    print(f"version  : {cfg.get('version')}")
    print(f"auto     : {cfg['resume'].get('auto_approve')}")
    print()

    if not cli:
        print("FAIL: no CLI resolved")
        return 1

    try:
        print("--- create the throwaway session ---")
        if not create_session(cli, session_id):
            print("FAIL: session was not created")
            return 1
        print(f"  exists: {session_exists(session_id)}  user rows: {message_count(session_id)}")

        print()
        print("--- resume.send_continue() ---")
        ref = resume.SessionRef(id=session_id, title="codx resume probe", directory=WORKDIR, updated_ms=0)
        before = message_count(session_id)
        sent, note = resume.send_continue(cfg, ref, None)
        after = message_count(session_id)
        print(f"  sent  : {sent}")
        print(f"  note  : {note}")
        print(f"  rows  : {before} -> {after}")
        if not sent:
            print("FAIL: send_continue reported failure")
            failures += 1
        if after <= before:
            print("FAIL: no new user row was stored")
            failures += 1
        else:
            print("  PASS: the message reached the store")

        print()
        print("--- schema fallback ordering ---")
        candidates = resume.cli_candidates(cfg)
        print(f"  candidates: {len(candidates)}")
        for c in candidates:
            print(f"    {c}")
        if candidates and "opencode-cli.exe" not in candidates[0].lower():
            print("FAIL: the v2 bundled CLI is not preferred")
            failures += 1
        else:
            print("  PASS: the desktop app's own CLI is preferred")

        print()
        print("--- pinned CLI ---")
        print(f"  resolve_cli: {resume.resolve_cli(cfg)}")
    finally:
        print()
        print("--- cleanup ---")
        try:
            proc = subprocess.run(
                [cli, "session", "delete", session_id],
                cwd=WORKDIR,
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print(f"  delete exit {proc.returncode}")
        except Exception as exc:  # noqa: BLE001 - cleanup
            print(f"  delete failed: {exc}")
        print(f"  still present: {session_exists(session_id)}")

    print()
    print(f"RESULT: {'PASS' if failures == 0 else f'{failures} failure(s)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
