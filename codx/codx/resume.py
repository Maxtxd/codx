"""Clear the client-side limit lockout and resume the chats that hit it.

Rotating the egress IP changes what the *server* sees. It does not change two
things on this machine:

1. OpenCode Desktop keeps its own rate-limit timestamp in `drafts.sqlite`
   (table `state`), and the composer stays disabled until that row is gone —
   even though the new IP is perfectly usable.
2. The chat itself is stopped. Nothing is going to send the next prompt.

So a rotation on its own looks like it worked and changes nothing the user can
see. This module does the other half: purge the lockout, find the sessions that
were active, nudge each one to continue through the live proxy, then prove the
nudge actually landed by reading it back out of the session database.

Nothing here invents state. Every claim is a row read back from SQLite.
"""
from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, logbus

log = logbus.logger("resume")

# The binary that last worked is remembered for the life of the process: probing
# a candidate costs a real dispatch, so the fallback should only pay once.
_cli_cache: str | None = None
_cli_lock = threading.Lock()


class ResumeError(RuntimeError):
    """The lockout could not be cleared or the resume could not be dispatched."""


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------
def _candidate_paths(raw_values: list[str]) -> list[Path]:
    seen: dict[str, Path] = {}
    for raw in raw_values:
        if not raw:
            continue
        path = Path(config.expand(raw))
        key = str(path).lower()
        if key not in seen:
            seen[key] = path
    return [p for p in seen.values() if p.is_file()]


def drafts_paths(cfg: dict) -> list[Path]:
    """Every drafts.sqlite that exists, in configured order."""
    return _candidate_paths(list(config.get(cfg, "resume.drafts_db", []) or []))


def session_db_paths(cfg: dict) -> list[Path]:
    """Every OpenCode session database that exists, in configured order."""
    return _candidate_paths(list(config.get(cfg, "resume.session_db", []) or []))


def _connect(path: Path, readonly: bool, timeout: float) -> sqlite3.Connection:
    """Open one database.

    Read-only callers use `mode=ro` so a running desktop app cannot be disturbed
    by a probe. Writers use the plain path with a busy timeout: SQLite serialises
    them against the app's own connection instead of failing outright.
    """
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=timeout)
    else:
        conn = sqlite3.connect(str(path), timeout=timeout)
    conn.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
    return conn


# --------------------------------------------------------------------------
# 1. clear the lockout
# --------------------------------------------------------------------------
@dataclass
class ClearResult:
    deleted: int = 0
    databases: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.errors:
            return "; ".join(self.errors)
        if self.deleted:
            return f"cleared {self.deleted} lockout row(s) in {len(self.databases)} database(s)"
        return "no client-side lockout present"


def clear_free_limit(cfg: dict) -> ClearResult:
    """Delete the client-side limit rows from every drafts.sqlite.

    The keys are the ones OpenCode Desktop writes when the free tier is spent.
    Both `name` and `key` are matched because the table carries two identifying
    columns and older builds populate only one of them.
    """
    result = ClearResult()
    if not bool(config.get(cfg, "resume.enabled", True)):
        return result

    keys = [str(k) for k in (config.get(cfg, "resume.lock_keys", []) or [])]
    likes = [str(p) for p in (config.get(cfg, "resume.lock_like", []) or [])]
    timeout = float(config.get(cfg, "resume.db_timeout_s", 5.0))

    if not keys and not likes:
        result.errors.append("resume.lock_keys and resume.lock_like are both empty")
        return result

    clauses: list[str] = []
    params: list[str] = []
    for column in ("key", "name"):
        if keys:
            marks = ",".join("?" for _ in keys)
            clauses.append(f"{column} IN ({marks})")
            params.extend(keys)
        for pattern in likes:
            clauses.append(f"{column} LIKE ?")
            params.append(pattern)
    where = " OR ".join(clauses)

    for path in drafts_paths(cfg):
        try:
            conn = _connect(path, readonly=False, timeout=timeout)
        except sqlite3.Error as exc:
            result.errors.append(f"{path.name}: cannot open ({exc})")
            continue
        try:
            with conn:
                cur = conn.execute(f"DELETE FROM state WHERE {where}", params)
                removed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            result.deleted += removed
            result.databases.append(str(path))
        except sqlite3.Error as exc:
            result.errors.append(f"{path.name}: {exc}")
        finally:
            conn.close()

    if result.deleted:
        log.info("cleared %d client-side lockout row(s)", result.deleted)
    elif result.ok:
        log.debug("no client-side lockout rows to clear")
    for err in result.errors:
        log.warning("lockout clear: %s", err)
    return result


# --------------------------------------------------------------------------
# 2. find what was running
# --------------------------------------------------------------------------
@dataclass
class SessionRef:
    id: str
    title: str
    directory: str
    updated_ms: int


def recent_sessions(cfg: dict) -> list[SessionRef]:
    """Sessions touched recently, newest first, one row per id.

    Archived sessions and child (sub-agent) sessions are skipped: nudging a
    sub-agent's session would start work nobody asked for.
    """
    within_minutes = float(config.get(cfg, "resume.recent_minutes", 120))
    limit = int(config.get(cfg, "resume.max_sessions", 3))
    timeout = float(config.get(cfg, "resume.db_timeout_s", 5.0))
    if limit <= 0:
        return []

    cutoff_ms = int((time.time() - within_minutes * 60.0) * 1000)
    found: dict[str, SessionRef] = {}

    for path in session_db_paths(cfg):
        try:
            conn = _connect(path, readonly=True, timeout=timeout)
        except sqlite3.Error as exc:
            log.warning("session scan %s: %s", path.name, exc)
            continue
        try:
            cur = conn.execute(
                """
                SELECT id, COALESCE(title, ''), COALESCE(directory, ''), time_updated
                  FROM session_v2
                 WHERE time_updated >= ?
                   AND (parent_id IS NULL OR parent_id = '')
                   AND (time_archived IS NULL OR time_archived = 0)
                 ORDER BY time_updated DESC
                 LIMIT ?
                """,
                (cutoff_ms, limit * 4),
            )
            for row in cur.fetchall():
                sid, title, directory, updated = row
                if not sid or sid in found:
                    continue
                found[sid] = SessionRef(
                    id=str(sid),
                    title=str(title),
                    directory=str(directory),
                    updated_ms=int(updated or 0),
                )
        except sqlite3.Error as exc:
            log.warning("session scan %s: %s", path.name, exc)
        finally:
            conn.close()

    ordered = sorted(found.values(), key=lambda s: s.updated_ms, reverse=True)
    return ordered[:limit]


# --------------------------------------------------------------------------
# 3. send "continue"
# --------------------------------------------------------------------------
def _desktop_cli_paths(cfg: dict) -> list[Path]:
    """The CLI shipped inside the desktop app, in configured order.

    That binary is built against the same schema as the database the app writes.
    A CLI found on PATH may be an older v1 build: it resolves the same database
    and then refuses it with "Database is not empty and has no session table",
    because it looks for `session` where the store has `session_v2`.
    """
    patterns = [str(p) for p in (config.get(cfg, "resume.cli_search", []) or [])]
    found: list[Path] = []
    seen: set[str] = set()
    for pattern in patterns:
        expanded = config.expand(pattern)
        if not expanded:
            continue
        matches = glob.glob(expanded) if any(ch in expanded for ch in "*?[") else [expanded]
        for match in matches:
            path = Path(match)
            key = str(path).lower()
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            found.append(path)
    return found


def cli_candidates(cfg: dict) -> list[str]:
    """Every opencode binary worth trying, best first."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if not value:
            return
        key = str(value).lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(str(value))

    explicit = config.expand(str(config.get(cfg, "resume.cli_path", "") or ""))
    if explicit and Path(explicit).is_file():
        add(explicit)
    for path in _desktop_cli_paths(cfg):
        add(str(path))
    for name in ("opencode.exe", "opencode.cmd", "opencode"):
        add(shutil.which(name))
    return ordered


def resolve_cli(cfg: dict) -> str | None:
    """The binary to use. The one that last worked is preferred."""
    with _cli_lock:
        cached = _cli_cache
    if cached and Path(cached).is_file():
        return cached
    candidates = cli_candidates(cfg)
    return candidates[0] if candidates else None


def _remember_cli(path: str) -> None:
    global _cli_cache
    with _cli_lock:
        if _cli_cache != path:
            _cli_cache = path
            log.info("resume CLI pinned to %s", path)


# A v1 build refuses the v2 store with one of these. Another binary is worth
# trying; any other failure would repeat identically.
_SCHEMA_ERRORS = (
    "has no session table",
    "no such table: session",
    "no such column",
)

# The server answers a spent meter with these. The CLI then exits non-zero, but
# the user message was already stored — the resume landed and the turn is what
# failed, which is precisely what a rotation is for.
_LIMIT_MARKERS = (
    "free usage limit",
    "usage limit",
    "rate limit",
    "too many requests",
    "quota",
)


def _classify(output: str) -> str:
    """What the CLI's combined output says about the dispatch."""
    lowered = output.lower()
    for marker in _SCHEMA_ERRORS:
        if marker in lowered:
            return "schema"
    for marker in _LIMIT_MARKERS:
        if marker in lowered:
            return "limit"
    return "other"


def _proxy_env(cfg: dict, proxy_url: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if proxy_url:
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
            env[name] = proxy_url
        env["NO_PROXY"] = "127.0.0.1,localhost,::1"
        env["https_proxy"] = proxy_url
        env["http_proxy"] = proxy_url
    return env


def _run_cli(
    cfg: dict,
    cli: str,
    session: SessionRef,
    text: str,
    extra: list[str],
    proxy_url: str | None,
    timeout: float,
) -> tuple[str, int, str]:
    """Run one dispatch.

    Returns (status, returncode, combined output). Status is one of:

        "ran"           the process finished; returncode is real
        "timeout"       it was still running at the deadline — it started, so the
                        binary is not the problem and trying another would only
                        pay the timeout twice
        "launch_failed" it never started; another candidate may work
    """
    cmd = [cli, "run", "--session", session.id, *extra, text]
    workdir = session.directory if session.directory and Path(session.directory).is_dir() else None
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    log.debug("resume dispatch: %s (cwd %s)", " ".join(cmd), workdir or "-")
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            env=_proxy_env(cfg, proxy_url),
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return "timeout", -1, ""
    except OSError as exc:
        return "launch_failed", -1, f"failed to start: {exc}"
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return "ran", proc.returncode, combined


def send_continue(
    cfg: dict,
    session: SessionRef,
    proxy_url: str | None,
) -> tuple[bool, str]:
    """Ask one session to carry on. Returns (dispatched, note).

    Dispatched means the message reached the store — not that the model answered.
    A spent free tier still stores the user row, and rotating the address is the
    fix for that, so treating it as a failure would skip the one case that
    matters.
    """
    candidates = cli_candidates(cfg)
    if not candidates:
        return False, "opencode CLI not found — set resume.cli_path in config.json"

    with _cli_lock:
        cached = _cli_cache
    if cached:
        candidates = [cached] + [c for c in candidates if c.lower() != cached.lower()]

    text = str(config.get(cfg, "resume.continue_text", "continue") or "continue")
    extra = [str(a) for a in (config.get(cfg, "resume.cli_args", []) or [])]
    if bool(config.get(cfg, "resume.auto_approve", True)) and "--auto" not in extra:
        extra.append("--auto")
    timeout = float(config.get(cfg, "resume.send_timeout_s", 180))
    since_ms = int(time.time() * 1000)

    notes: list[str] = []
    for cli in candidates:
        status, code, output = _run_cli(cfg, cli, session, text, extra, proxy_url, timeout)
        name = Path(cli).name

        if status == "launch_failed":
            notes.append(f"{name}: {output}")
            continue

        # A timeout still means the binary ran. The message may have landed, so
        # ask the store before deciding — and never retry the same dispatch on
        # another binary, which would double-send and pay the timeout twice.
        if status == "timeout":
            if arrived(cfg, session.id, since_ms):
                _remember_cli(cli)
                return True, "dispatched (turn still running at the deadline)"
            notes.append(f"{name}: timed out after {timeout:.0f}s")
            break

        kind = _classify(output)
        if code == 0:
            _remember_cli(cli)
            return True, "dispatched"

        # The row can land even when the turn fails, so ask the store rather than
        # trusting the exit code.
        if arrived(cfg, session.id, since_ms):
            _remember_cli(cli)
            if kind == "limit":
                return True, "dispatched (server still metered — rotation is the fix)"
            return True, "dispatched (turn errored but the message was stored)"

        tail = output.strip().splitlines()
        note = tail[-1][:200] if tail else f"exit {code}"
        notes.append(f"{name}: {note}")
        if kind != "schema":
            # Not a reader problem; another binary would fail the same way.
            break

    return False, "; ".join(notes) if notes else "dispatch failed"


# --------------------------------------------------------------------------
# 4. prove it landed
# --------------------------------------------------------------------------
def arrived(cfg: dict, session_id: str, since_ms: int) -> bool:
    """True when a user message newer than `since_ms` exists in the session."""
    timeout = float(config.get(cfg, "resume.db_timeout_s", 5.0))
    for path in session_db_paths(cfg):
        try:
            conn = _connect(path, readonly=True, timeout=timeout)
        except sqlite3.Error:
            continue
        try:
            cur = conn.execute(
                """
                SELECT 1
                  FROM session_message
                 WHERE session_id = ?
                   AND type = 'user'
                   AND time_created >= ?
                 LIMIT 1
                """,
                (session_id, since_ms),
            )
            if cur.fetchone():
                return True
        except sqlite3.Error as exc:
            log.warning("arrival check %s: %s", path.name, exc)
        finally:
            conn.close()
    return False


# --------------------------------------------------------------------------
# 5. the whole pipeline
# --------------------------------------------------------------------------
@dataclass
class ResumeReport:
    cleared: ClearResult = field(default_factory=ClearResult)
    sessions: list[SessionRef] = field(default_factory=list)
    dispatched: int = 0
    confirmed: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.sessions:
            return self.cleared.summary() + "; no recent session to resume"
        parts = [self.cleared.summary()]
        parts.append(f"resumed {self.dispatched}/{len(self.sessions)} session(s)")
        if self.dispatched and self.confirmed:
            parts.append(f"{self.confirmed} confirmed in the session database")
        return "; ".join(parts)


def resume(
    cfg: dict,
    proxy_url: str | None = None,
    session_ids: list[str] | None = None,
) -> ResumeReport:
    """Clear the lockout, nudge the sessions, verify arrival.

    `session_ids` narrows the targets when the caller already knows which chats
    hit the limit. Without it, the recently active sessions are used.
    """
    report = ResumeReport()
    if not bool(config.get(cfg, "resume.enabled", True)):
        report.notes.append("resume disabled in config")
        return report

    report.cleared = clear_free_limit(cfg)

    if session_ids:
        known = {s.id: s for s in recent_sessions(cfg)}
        chosen: list[SessionRef] = []
        for sid in session_ids:
            if sid in known:
                chosen.append(known[sid])
            else:
                chosen.append(SessionRef(id=sid, title="", directory="", updated_ms=0))
        report.sessions = chosen
    else:
        report.sessions = recent_sessions(cfg)

    if not report.sessions:
        log.info("nothing to resume: no recently active session")
        return report

    for session in report.sessions:
        since_ms = int(time.time() * 1000)
        sent, note = send_continue(cfg, session, proxy_url)
        label = session.title or session.id
        if not sent:
            report.notes.append(f"{label}: {note}")
            log.warning("resume %s failed: %s", label, note)
            continue
        report.dispatched += 1
        log.info("resume sent to %s (%s)", label, session.id)
        if note != "dispatched":
            report.notes.append(f"{label}: {note}")
            log.info("resume %s: %s", label, note)

        deadline = time.monotonic() + float(config.get(cfg, "resume.verify_timeout_s", 90))
        poll = max(1.0, float(config.get(cfg, "resume.verify_poll_s", 3)))
        confirmed = False
        while time.monotonic() < deadline:
            if arrived(cfg, session.id, since_ms):
                confirmed = True
                break
            time.sleep(poll)
        if confirmed:
            report.confirmed += 1
            log.info("resume confirmed in %s", label)
        else:
            report.notes.append(f"{label}: dispatched but not yet visible in the session database")
            log.warning("resume dispatched to %s but not confirmed yet", label)

    return report
