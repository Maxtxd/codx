"""Prove that `opencode run --session <id>` lands a user row in the v2 database.

Creates a throwaway session through the desktop app's own v2 CLI, dispatches one
prompt, then reads `session_message` back to confirm the row exists. The session
is deleted at the end, whether the test passed or not.

The model call itself may fail (a spent free tier is the whole reason this app
exists). That does not matter: the assertion is about the stored user message,
which is what the resume pipeline verifies against.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codx import config  # noqa: E402

CLI = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "ai.opencode.desktop" / "resources" / "opencode-cli.exe"
DB = Path(os.path.expanduser("~")) / ".local" / "share" / "opencode" / "opencode.db"
WORKDIR = Path(r"C:\Users\A\Downloads\OJBS-OPENCODE")


def user_rows(session_id: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        return conn.execute(
            """
            SELECT id, type, time_created
              FROM session_message
             WHERE session_id = ?
             ORDER BY seq
            """,
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def session_row(session_id: str) -> tuple | None:
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        return conn.execute(
            "SELECT id, title, directory FROM session_v2 WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()


def main() -> int:
    if not CLI.is_file():
        print(f"FAIL: v2 CLI missing at {CLI}")
        return 1
    if not DB.is_file():
        print(f"FAIL: session database missing at {DB}")
        return 1

    session_id = f"ses_codxprobe{secrets.token_hex(8)}"
    print(f"throwaway session : {session_id}")
    print(f"cli               : {CLI}")
    print()

    failures = 0
    try:
        print("--- dispatch ---")
        cmd = [
            str(CLI), "run",
            "--session", session_id,
            "--title", "codx probe",
            "--auto",
            "codx probe",
        ]
        print("  " + " ".join(cmd))
        started = time.time()
        proc = subprocess.run(
            cmd,
            cwd=str(WORKDIR),
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        elapsed = time.time() - started
        print(f"  exit    : {proc.returncode}  ({elapsed:.1f}s)")
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if out:
            print("  stdout  :")
            for line in out.splitlines()[:12]:
                print(f"    {line}")
        if err:
            print("  stderr  :")
            for line in err.splitlines()[:12]:
                print(f"    {line}")

        print()
        print("--- session row ---")
        row = session_row(session_id)
        if row:
            print(f"  created: {row[0]}  title={row[1]!r}  dir={row[2]!r}")
        else:
            print("  no session_v2 row — the CLI never reached the store")
            failures += 1

        print()
        print("--- message rows ---")
        rows = user_rows(session_id)
        print(f"  rows: {len(rows)}")
        for r in rows[:6]:
            print(f"    {r[0]}  type={r[1]}  t={r[2]}")
        user_rows_only = [r for r in rows if r[1] == "user"]
        if not user_rows_only:
            print("  FAIL: no type='user' row — arrived() would never confirm")
            failures += 1
        else:
            print(f"  PASS: {len(user_rows_only)} user row(s) stored")
    finally:
        print()
        print("--- cleanup ---")
        try:
            proc = subprocess.run(
                [str(CLI), "session", "delete", session_id],
                cwd=str(WORKDIR),
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            print(f"  delete exit {proc.returncode}: {(proc.stdout or proc.stderr or '').strip()[:160]}")
        except Exception as exc:  # noqa: BLE001 - cleanup
            print(f"  delete failed: {exc}")
        print(f"  row after delete: {session_row(session_id)}")

    print()
    print(f"RESULT: {'PASS' if failures == 0 else f'{failures} failure(s)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
