"""Launch the target app / CLI with the bridge injected as its proxy.

This is the "route only this app" mechanism: the child process is the only
thing that receives the proxy environment, so the rest of the machine is
untouched. Proxifier-style per-process rules are not required.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import config, logbus

log = logbus.logger("launcher")

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class Launched:
    name: str
    pid: int
    command: list[str]
    kind: str
    proc: subprocess.Popen = field(repr=False, default=None)  # type: ignore[assignment]


class Launcher:
    """Spawns children with the proxy env and reaps them on shutdown."""

    def __init__(self, cfg: dict, proxy_url: str) -> None:
        self.cfg = cfg
        self.proxy_url = proxy_url
        self._children: dict[int, Launched] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ env

    def child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        var = config.get(self.cfg, "target.proxy_env_var", "HTTPS_PROXY") or "HTTPS_PROXY"
        alt = config.get(self.cfg, "target.proxy_env_var_alt", "HTTP_PROXY") or "HTTP_PROXY"
        env[var] = self.proxy_url
        env[alt] = self.proxy_url
        env["ALL_PROXY"] = self.proxy_url
        # keep loopback and the WARP control plane off the bridge
        existing = env.get("NO_PROXY", "")
        local = "localhost,127.0.0.1,::1"
        env["NO_PROXY"] = f"{existing},{local}" if existing else local
        env["NO_PROXY"] = env["NO_PROXY"].strip(",")
        return env

    # -------------------------------------------------------------- resolve

    def _resolve(self, raw: str) -> str | None:
        if not raw:
            return None
        expanded = config.expand(raw)
        candidate = Path(expanded)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(expanded)
        return found

    # --------------------------------------------------------------- launch

    def launch_app(self) -> tuple[bool, str]:
        raw = config.get(self.cfg, "target.app_path", "") or ""
        args = config.get(self.cfg, "target.app_args", []) or []
        exe = self._resolve(raw)
        if not exe:
            return False, (
                "target.app_path is not set or does not exist — "
                "point it at the client's .exe in config.json (target.app_path)"
            )
        cmd = [exe, *[str(a) for a in args]]
        return self._spawn(cmd, config.get(self.cfg, "target.name", "app"), "app", console=False)

    def launch_cli(self) -> tuple[bool, str]:
        raw = config.get(self.cfg, "target.cli_path", "") or ""
        args = config.get(self.cfg, "target.cli_args", []) or []
        exe = self._resolve(raw)
        if not exe:
            return False, (
                f"'{raw}' not found on PATH — set target.cli_path in config.json "
                f"to the full path of the client's CLI"
            )
        cmd = [exe, *[str(a) for a in args]]
        return self._spawn(cmd, config.get(self.cfg, "target.name", "cli") + " CLI", "cli", console=True)

    def _spawn(self, cmd: list[str], name: str, kind: str, console: bool) -> tuple[bool, str]:
        env = self.child_env()
        flags = CREATE_NEW_PROCESS_GROUP
        if console:
            flags |= CREATE_NEW_CONSOLE
        try:
            proc = subprocess.Popen(cmd, env=env, creationflags=flags, cwd=str(Path(cmd[0]).parent))
        except OSError as exc:
            log.error("launch %s failed: %s", name, exc)
            return False, f"cannot launch {cmd[0]}: {exc}"
        with self._lock:
            self._children[proc.pid] = Launched(name=name, pid=proc.pid, command=cmd, kind=kind, proc=proc)
        log.info("launched %s (pid %d) with proxy %s", name, proc.pid, self.proxy_url)
        return True, f"{name} started (pid {proc.pid}) via {self.proxy_url}"

    # -------------------------------------------------------------- teardown

    def reap(self, force: bool = False) -> int:
        """Stop children we started. Returns how many were signalled."""
        with self._lock:
            children = list(self._children.values())
            self._children.clear()
        killed = 0
        for child in children:
            proc = child.proc
            if proc is None or proc.poll() is not None:
                continue
            try:
                if force:
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        creationflags=0x08000000,
                    )
                else:
                    proc.terminate()
                killed += 1
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("could not stop %s (pid %d): %s", child.name, child.pid, exc)
        if killed:
            log.info("stopped %d child process(es)", killed)
        return killed

    def running(self) -> list[dict]:
        with self._lock:
            items = list(self._children.values())
        out = []
        for child in items:
            alive = child.proc is not None and child.proc.poll() is None
            out.append({"name": child.name, "pid": child.pid, "kind": child.kind, "alive": alive})
        return out
