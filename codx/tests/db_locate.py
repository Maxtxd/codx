"""Find the database the opencode CLI actually opens, and why it refuses it.

Reads the CLI's own resolution logic out of the installed executable, lists the
candidate directories on this machine, and inspects the schema of each database
that exists. Read-only: nothing is created, migrated or deleted.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

EXE = Path(os.environ["LOCALAPPDATA"]) / "nodejs" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"

CHANNEL_ENV = ("OPENCODE_CHANNEL", "OPENCODE_DB", "OPENCODE_DISABLE_CHANNEL_DB", "XDG_DATA_HOME")


def safe(text: str) -> str:
    """Strip anything the Windows console cannot encode, so a dump never dies."""
    return text.encode("ascii", "replace").decode("ascii")


def env_report() -> None:
    print("=" * 70)
    print("environment")
    print("=" * 70)
    for name in CHANNEL_ENV:
        print(f"{name:28} = {os.environ.get(name)!r}")


def exe_report() -> None:
    print()
    print("=" * 70)
    print("channel database resolution (from the installed exe)")
    print("=" * 70)
    if not EXE.is_file():
        print(f"missing: {EXE}")
        return
    text = EXE.read_bytes().decode("ascii", "replace")
    needle = "OPENCODE_DISABLE_CHANNEL_DB"
    idx = text.find(needle)
    if idx < 0:
        print("marker not found")
        return
    print(text[max(0, idx - 1100): idx + 400].replace("\x00", ""))
    print()
    print("-" * 70)
    print("data directory (win32 branch)")
    print("-" * 70)
    marker = '"win32"'
    pos = text.find(marker)
    if pos >= 0:
        print(safe(text[max(0, pos - 400): pos + 400].replace("\x00", "")))
    print()
    print("-" * 70)
    print("channel value (Hs)")
    print("-" * 70)
    for probe in ('OPENCODE_CHANNEL', 'var Hs=', 'Hs='):
        at = text.find(probe)
        if at >= 0:
            print(f"[{probe}] @ {at}")
            print(safe(text[max(0, at - 300): at + 300].replace("\x00", "")))
            print()


def db_report() -> None:
    print()
    print("=" * 70)
    print("candidate databases")
    print("=" * 70)
    home = Path(os.path.expanduser("~"))
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))

    roots = [
        local / "opencode",
        home / ".local" / "share" / "opencode",
        roaming / "opencode",
        roaming / "ai.opencode.desktop",
    ]
    candidates: list[Path] = []
    for root in roots:
        print(f"\n{root}")
        if not root.is_dir():
            print("  (does not exist)")
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_file() and entry.suffix == ".db":
                size = entry.stat().st_size
                print(f"  {entry.name:34} {size:>12,} bytes")
                candidates.append(entry)
            elif entry.is_file():
                print(f"  {entry.name:34} (not a db)")

    for db in candidates:
        print()
        print("-" * 70)
        print(db)
        print("-" * 70)
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error as exc:
            print(f"  cannot open: {exc}")
            continue
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            print(f"  tables ({len(tables)}): {', '.join(tables) if tables else 'NONE'}")
            has_session = "session" in tables
            print(f"  has 'session' table: {has_session}")
            if has_session:
                count = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
                print(f"  session rows: {count}")
            if "session_v2" in tables:
                count = conn.execute("SELECT COUNT(*) FROM session_v2").fetchone()[0]
                print(f"  session_v2 rows: {count}")
            if "session_message" in tables:
                count = conn.execute("SELECT COUNT(*) FROM session_message").fetchone()[0]
                print(f"  session_message rows: {count}")
        except sqlite3.Error as exc:
            print(f"  query failed: {exc}")
        finally:
            conn.close()


def main() -> int:
    env_report()
    exe_report()
    db_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
