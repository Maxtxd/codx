"""Find every opencode binary on this machine and what schema each one expects.

The desktop app writes schema v2 (`session_v2`); the CLI on PATH is v1 and looks
for a `session` table. This enumerates the candidates so the resume path can be
pointed at something that can actually read the database.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

LOCAL = Path(os.environ["LOCALAPPDATA"])
ROAMING = Path(os.environ["APPDATA"])
HOME = Path(os.path.expanduser("~"))


def report_binary(path: Path) -> None:
    print("-" * 70)
    print(path)
    print("-" * 70)
    try:
        size = path.stat().st_size
    except OSError as exc:
        print(f"  stat failed: {exc}")
        return
    print(f"  size: {size:,} bytes")

    text = path.read_bytes().decode("ascii", "replace")

    # Which schema does it know about?
    has_session = 'name==="session"' in text or "session_v2" not in text
    print(f"  mentions session_v2 : {'session_v2' in text}")
    print(f"  mentions session    : {'\"session\"' in text}")

    # Version string candidates.
    for marker in ('"1.19', 'version:"', '"version"'):
        at = text.find(marker)
        if at >= 0:
            snippet = text[max(0, at - 60): at + 80].replace("\x00", " ")
            print(f"  [{marker}] {snippet.encode('ascii','replace').decode('ascii')}")
            break


def scan(root: Path, depth: int = 4) -> list[Path]:
    found: list[Path] = []
    if not root.is_dir():
        return found
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if len(here.parts) - base_depth > depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in {"node_modules", "Cache", "GPUCache"}][:40]
        for name in filenames:
            if name.lower().startswith("opencode") and name.lower().endswith((".exe", ".cmd", ".ps1")):
                found.append(here / name)
        if len(found) > 60:
            break
    return found


def main() -> int:
    print("=" * 70)
    print("desktop app version")
    print("=" * 70)
    pkg = LOCAL / "Programs" / "ai.opencode.desktop" / "resources" / "app.asar"
    for candidate in (
        LOCAL / "Programs" / "ai.opencode.desktop" / "resources",
        LOCAL / "Programs" / "ai.opencode.desktop",
    ):
        if candidate.is_dir():
            for entry in sorted(candidate.iterdir()):
                print(f"  {entry.name:40} {entry.stat().st_size if entry.is_file() else '<dir>'}")
    print()

    print("=" * 70)
    print("every opencode binary / launcher")
    print("=" * 70)
    roots = [
        LOCAL / "nodejs",
        LOCAL / "opencode-unlimited.old",
        LOCAL / "opencode",
        ROAMING / "npm",
        LOCAL / "Programs",
        LOCAL / "Microsoft" / "WindowsApps",
        HOME / ".bun" / "bin",
    ]
    seen: set[Path] = set()
    for root in roots:
        for hit in scan(root):
            if hit in seen:
                continue
            seen.add(hit)
            print(f"  {hit}")
    print()

    exe = LOCAL / "nodejs" / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    if exe.is_file():
        report_binary(exe)

    old = LOCAL / "opencode-unlimited.old"
    if old.is_dir():
        print()
        print("=" * 70)
        print("opencode-unlimited.old contents")
        print("=" * 70)
        for entry in sorted(old.iterdir())[:30]:
            kind = "<dir>" if entry.is_dir() else f"{entry.stat().st_size:,}"
            print(f"  {entry.name:40} {kind}")

    print()
    print("=" * 70)
    print("desktop app package metadata")
    print("=" * 70)
    for p in (
        LOCAL / "Programs" / "ai.opencode.desktop" / "resources" / "app" / "package.json",
        LOCAL / "Programs" / "ai.opencode.desktop" / "package.json",
    ):
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 - probe
                print(f"  {p}: unreadable ({exc})")
                continue
            print(f"  {p}")
            print(f"    name    : {data.get('name')}")
            print(f"    version : {data.get('version')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
