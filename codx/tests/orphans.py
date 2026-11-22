"""Report any session the test suite left behind in the live database.

The suite creates throwaway sessions and deletes them in a `finally`. When a run
is interrupted the delete never happens, and every later rotation would resume a
session nobody is watching. This finds them so they can be removed.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codx import config, resume  # noqa: E402

DB = Path(os.path.expanduser("~")) / ".local" / "share" / "opencode" / "opencode.db"
PREFIXES = ("ses_codx",)


def main() -> int:
    cfg = config.load(ROOT / "config.json")
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(
            "SELECT id, title FROM session_v2 WHERE parent_id IS NULL OR parent_id = ''"
        ).fetchall()
    finally:
        conn.close()

    orphans = [(sid, title or "") for sid, title in rows if sid.startswith(PREFIXES)]
    print(f"live sessions : {len(rows)}")
    print(f"leftovers     : {len(orphans)}")
    for sid, title in orphans:
        print(f"  {sid}  {title!r}")

    if not orphans:
        return 0

    if "--delete" not in sys.argv:
        print()
        print("re-run with --delete to remove them")
        return 0

    cli = resume.resolve_cli(cfg)
    if not cli:
        print("no CLI resolved; cannot delete")
        return 1

    print()
    for sid, _ in orphans:
        proc = subprocess.run(
            [cli, "session", "delete", sid],
            capture_output=True,
            text=True,
            timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(f"  delete {sid}: exit {proc.returncode}")

    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        left = conn.execute(
            "SELECT COUNT(*) FROM session_v2 WHERE id LIKE 'ses_codx%'"
        ).fetchone()[0]
    finally:
        conn.close()
    print(f"remaining     : {left}")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
