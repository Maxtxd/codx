"""Transport backends: Cloudflare WARP (SOCKS5 proxy mode) and Tor.

Both expose the same surface so the engine can swap them at runtime:

    available() -> bool
    start()     -> raises TransportError
    stop()
    is_up()     -> bool
    new_identity() -> str | None   (new public IP when it can be observed)
    info()      -> dict
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import config, httpc, logbus, socks

log = logbus.logger("transport")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


class TransportError(RuntimeError):
    """A transport could not be brought up."""


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a console program without flashing a window."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        encoding="utf-8",
        errors="replace",
    )


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_port(host: str, port: int, deadline: float, stop: threading.Event | None = None) -> bool:
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return False
        if _port_open(host, port, 1.0):
            return True
        time.sleep(0.35)
    return False


def _status_settings(status_obj: dict) -> dict:
    """Pull the applied settings out of whatever shape warp-cli status returns."""
    if not isinstance(status_obj, dict):
        return {}
    for key in ("current", "settings"):
        node = status_obj.get(key)
        if isinstance(node, dict):
            return node
    reason = status_obj.get("reason")
    if isinstance(reason, dict):
        for value in reason.values():
            if isinstance(value, dict):
                current = value.get("current")
                if isinstance(current, dict):
                    return current
    return status_obj


def _status_is_proxy(status_obj: dict) -> bool:
    settings = _status_settings(status_obj)
    mode = settings.get("operation_mode")
    if isinstance(mode, dict):
        return any(str(k).lower() in {"warpproxy", "proxy"} for k in mode)
    if isinstance(mode, str):
        return mode.lower() in {"proxy", "warpproxy"}
    return False


def _status_proxy_port(status_obj: dict) -> int | None:
    settings = _status_settings(status_obj)
    mode = settings.get("operation_mode")
    if isinstance(mode, dict):
        for key, value in mode.items():
            if str(key).lower() in {"warpproxy", "proxy"} and isinstance(value, int):
                return value
    return None


def _status_protocol(status_obj: dict) -> str:
    settings = _status_settings(status_obj)
    value = settings.get("warp_tunnel_protocol")
    return str(value) if value else ""


class BaseTransport:
    name = "base"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.last_error: str = ""
        self.upstream: tuple[str, int] | None = None
        self._lock = threading.RLock()

    # -- contract ---------------------------------------------------------
    def available(self) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def is_up(self) -> bool:
        raise NotImplementedError

    def new_identity(self) -> str | None:
        raise NotImplementedError

    def info(self) -> dict:
        return {"name": self.name, "up": False, "upstream": None, "error": self.last_error}

    # -- shared helpers ---------------------------------------------------
    def public_ip(self) -> str | None:
        url = config.get(self.cfg, "ip.v4_url")
        timeout = float(config.get(self.cfg, "ip.timeout_s", 12))
        try:
            return httpc.fetch_ip(self.upstream, url, timeout)
        except httpc.HttpError as exc:
            self.last_error = str(exc)
            return None


# ============================================================== WARP

class WarpTransport(BaseTransport):
    """Cloudflare WARP driven through the official warp-cli."""

    name = "WARP"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.cli: str | None = config.find_warp_cli(cfg)
        self.host = config.get(cfg, "warp.proxy_host", "127.0.0.1")
        self.port = int(config.get(cfg, "warp.proxy_port", 40000))
        self.timeout = float(config.get(cfg, "warp.connect_timeout_s", 30))
        self.upstream = (self.host, self.port)
        self._original_mode: str | None = None
        self._original_protocol: str | None = None
        self._address_attempts = 0
        self._settings_cache: dict = {}

    # -- cli plumbing -----------------------------------------------------
    def _cli(self, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
        if not self.cli:
            raise TransportError("warp-cli.exe not found — set warp.cli_path in config.json")
        cmd = [self.cli, *args]
        try:
            return _run(cmd, timeout or self.timeout)
        except FileNotFoundError as exc:
            raise TransportError(f"cannot execute {self.cli}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"warp-cli {' '.join(args)} timed out after {timeout or self.timeout}s") from exc

    def _json(self, args: list[str], timeout: float | None = None) -> dict:
        proc = self._cli(["-j", *args], timeout)
        out = (proc.stdout or "").strip()
        if not out:
            raise TransportError(
                f"warp-cli {' '.join(args)} produced no output "
                f"(exit {proc.returncode}): {(proc.stderr or '').strip()[:300]}"
            )
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise TransportError(f"warp-cli {' '.join(args)} returned non-JSON: {out[:300]!r}") from exc
        if not isinstance(data, dict):
            raise TransportError(f"warp-cli {' '.join(args)} returned {type(data).__name__}, expected object")
        return data

    def available(self) -> bool:
        return bool(self.cli and Path(self.cli).is_file())

    # -- mode enforcement -------------------------------------------------
    def settings(self) -> dict:
        data = self._json(["settings", "list"])
        settings = data.get("settings", data)
        if not isinstance(settings, dict):
            raise TransportError("settings list has no settings object")
        self._settings_cache = settings
        return settings

    def enforce_mode(self) -> None:
        """Force proxy mode on the MASQUE transport and verify it stuck.

        Order is not cosmetic: the service validates proxy mode against the
        tunnel protocol at startup. Switching to proxy while WireGuard is still
        selected makes it log `InvalidKey("Proxy mode only supports MASQUE")`
        and retry every 5s forever. Protocol first, then mode, then port.
        """
        if not config.get(self.cfg, "warp.enforce_mode", True):
            return
        want_mode = config.get(self.cfg, "warp.mode", "proxy")
        want_proto = config.get(self.cfg, "warp.tunnel_protocol", "MASQUE")

        if want_mode == "proxy" and want_proto and want_proto.lower() != "masque":
            log.warning(
                "proxy mode requires MASQUE; overriding tunnel_protocol %s -> MASQUE",
                want_proto,
            )
            want_proto = "MASQUE"

        try:
            current = self.settings()
        except TransportError as exc:
            log.warning("cannot read WARP settings yet: %s", exc)
            current = {}

        cur_mode = str(current.get("operation_mode", "")).lower()
        cur_proto = str(current.get("warp_tunnel_protocol", ""))

        if self._original_mode is None and cur_mode:
            self._original_mode = cur_mode
        if self._original_protocol is None and cur_proto:
            self._original_protocol = cur_proto

        if want_proto:
            if cur_proto.lower() != want_proto.lower():
                log.info("WARP tunnel protocol %s -> %s", cur_proto or "default", want_proto)
                proc = self._cli(["tunnel", "protocol", "set", want_proto])
                if proc.returncode != 0:
                    raise TransportError(
                        f"warp-cli tunnel protocol set {want_proto} failed: "
                        f"{(proc.stderr or proc.stdout).strip()[:300]}"
                    )
                cur_proto = want_proto
                # the service rebuilds its tunnel config on protocol change
                time.sleep(2.0)

        if cur_mode != want_mode:
            log.info("WARP mode %s -> %s", cur_mode or "unknown", want_mode)
            proc = self._cli(["mode", want_mode])
            if proc.returncode != 0:
                raise TransportError(
                    f"warp-cli mode {want_mode} failed: {(proc.stderr or proc.stdout).strip()[:300]}"
                )

        proc = self._cli(["proxy", "port", str(self.port)])
        if proc.returncode != 0:
            raise TransportError(
                f"warp-cli proxy port {self.port} failed: {(proc.stderr or proc.stdout).strip()[:300]}"
            )

        try:
            verified = self.status()
        except TransportError as exc:
            log.warning("could not verify WARP settings: %s", exc)
            return

        # Verification has to survive two quirks: `status` only carries
        # operation_mode inside a SettingsChanged reason (a healthy connection
        # reports just "NetworkHealthy"), and `settings list` can lag the service
        # by a few seconds. Accept either source, and let start() prove the
        # listener with a real SOCKS5 handshake.
        deadline = time.monotonic() + 20.0
        got_proto = ""
        mode_ok = False
        last_seen = ""
        while time.monotonic() < deadline:
            try:
                st = self.status()
            except TransportError:
                st = {}
            try:
                sett = self.settings()
            except TransportError:
                sett = {}

            st_mode = str(_status_settings(st).get("operation_mode", "")).lower()
            sett_mode = str(sett.get("operation_mode", "")).lower()
            last_seen = f"status={st_mode or '-'} settings={sett_mode or '-'}"
            mode_ok = _status_is_proxy(st) or sett_mode == "proxy"
            got_proto = (
                _status_protocol(st)
                or str(sett.get("warp_tunnel_protocol", ""))
                or got_proto
            )
            if mode_ok:
                break
            time.sleep(1.0)

        if not mode_ok:
            raise TransportError(
                "WARP refused proxy mode — check that the client is not locked by "
                f"policy ({last_seen})"
            )

        if want_proto and got_proto and got_proto.lower() != want_proto.lower():
            raise TransportError(f"WARP refused protocol change (still {got_proto})")
        log.info(
            "WARP enforced: mode=proxy(%d) protocol=%s socks=%s:%d",
            _status_proxy_port({}) or self.port, got_proto or want_proto, self.host, self.port,
        )

    def restore_original(self) -> None:
        """Put WARP back the way the user wants it. Best effort, never raises.

        The restore targets come from config, not from whatever we observed on
        startup: after a crash or a hard kill the "observed" state is our own
        proxy mode, and restoring that would leave the machine in proxy mode
        forever.
        """
        if not self.available():
            return
        target_mode = config.get(self.cfg, "warp.restore_mode", "warp")
        target_proto = config.get(self.cfg, "warp.restore_tunnel_protocol", "WireGuard")
        reconnect = bool(config.get(self.cfg, "warp.restore_connect", True))
        try:
            try:
                current = self.settings()
            except TransportError:
                current = {}

            cur_proto = str(current.get("warp_tunnel_protocol", ""))
            if target_proto and cur_proto.lower() != target_proto.lower():
                log.info("restoring WARP protocol %s -> %s", cur_proto or "?", target_proto)
                self._cli(["tunnel", "protocol", "set", target_proto], timeout=25)
                time.sleep(2.0)

            cur_mode = str(current.get("operation_mode", "")).lower()
            if target_mode and cur_mode != target_mode:
                log.info("restoring WARP mode %s -> %s", cur_mode or "?", target_mode)
                self._cli(["mode", target_mode], timeout=25)

            if reconnect:
                self._cli(["connect"], timeout=25)
                # `connect` returns as soon as the request is accepted; the
                # tunnel handshake takes a few seconds after a protocol change.
                deadline = time.monotonic() + 25.0
                while time.monotonic() < deadline:
                    if str(self.status().get("status", "")).lower() == "connected":
                        break
                    time.sleep(1.0)
            log.info(
                "WARP restored to mode=%s protocol=%s (status %s)",
                target_mode, target_proto, self.status().get("status"),
            )
        except (TransportError, OSError) as exc:
            log.warning("could not restore WARP settings: %s", exc)

    # -- status -----------------------------------------------------------
    def status(self) -> dict:
        try:
            return self._json(["status"], timeout=15)
        except TransportError as exc:
            self.last_error = str(exc)
            return {"status": "Unknown", "reason": str(exc)}

    def is_up(self) -> bool:
        if not self.upstream:
            return False
        if not _port_open(self.upstream[0], self.upstream[1], 1.0):
            return False
        try:
            socks.socks5_connect(self.upstream[0], self.upstream[1], "1.1.1.1", 443, timeout=6).close()
            return True
        except (socks.SocksError, OSError):
            return False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if not self.available():
                raise TransportError(
                    "warp-cli.exe not found — install Cloudflare WARP or set warp.cli_path"
                )
            self.close_gui()
            self.enforce_mode()

            # After a mode change the service can sit in "Unable" until the
            # tunnel is torn down and rebuilt. A bare `connect` against that
            # state returns Success and then does nothing at all.
            state = str(self.status().get("status", "")).lower()
            if state in {"unable", "disconnecting"}:
                log.info("WARP status %s — cycling the tunnel", state)
                self._cli(["disconnect"], timeout=20)
                time.sleep(2.0)

            proc = self._cli(["connect"])
            if proc.returncode != 0:
                text = (proc.stderr or proc.stdout).strip()
                if "already" not in text.lower():
                    raise TransportError(f"warp-cli connect failed: {text[:300]}")

            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                state = str(self.status().get("status", "")).lower()
                if state == "connected" and _port_open(self.host, self.port, 1.0):
                    ip = self.public_ip()
                    log.info("WARP up (socks %s:%d, ip %s)", self.host, self.port, ip or "unknown")
                    self.last_error = ""
                    return
                if state in {"disconnected", "disconnecting"} and time.monotonic() > deadline - self.timeout * 0.6:
                    break
                time.sleep(1.0)
            raise TransportError(
                f"WARP did not reach Connected with a SOCKS listener on {self.host}:{self.port} "
                f"within {self.timeout:.0f}s (status: {self.status().get('status')})"
            )

    def close_gui(self) -> None:
        """Close the Cloudflare WARP *GUI* so it stops rewriting operation_mode.

        The desktop client keeps its own copy of the settings and pushes it back
        to the service whenever its window is opened or focused. That silently
        flips operation_mode from `proxy` back to `warp`, which kills the SOCKS
        listener mid-session. The service (`warp-svc`) is what actually carries
        traffic and is not touched here — only the settings UI goes away.
        """
        if not bool(config.get(self.cfg, "warp.keep_gui_closed", True)):
            return
        name = str(config.get(self.cfg, "warp.gui_process", "Cloudflare WARP.exe"))
        try:
            proc = _run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"], 15)
        except (OSError, subprocess.TimeoutExpired):
            return
        if name.lower() not in (proc.stdout or "").lower():
            return
        log.info("closing the WARP settings GUI so it cannot reset proxy mode")
        # graceful first: the GUI flushes its state, then exits
        _run(["taskkill", "/IM", name], 15)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                check = _run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"], 10)
            except (OSError, subprocess.TimeoutExpired):
                return
            if name.lower() not in (check.stdout or "").lower():
                return
            time.sleep(0.5)
        _run(["taskkill", "/F", "/IM", name], 15)

    def repair(self) -> bool:
        """Cheap recovery: re-assert proxy mode and wait for the listener.

        Called by the watchdog before it gives up on WARP. Cheaper and far less
        disruptive than a full stop/start, and it fixes the common case where
        only the mode drifted while the tunnel stayed connected.
        """
        with self._lock:
            if not self.available():
                return False
            try:
                self.close_gui()
                self.enforce_mode()
                state = str(self.status().get("status", "")).lower()
                if state in {"unable", "disconnecting"}:
                    self._cli(["disconnect"], timeout=20)
                    time.sleep(2.0)
                self._cli(["connect"], timeout=25)
            except TransportError as exc:
                self.last_error = str(exc)
                log.warning("WARP repair failed: %s", exc)
                return False
            if _wait_port(self.host, self.port, time.monotonic() + 35.0):
                log.info("WARP repaired (socks %s:%d back up)", self.host, self.port)
                self.last_error = ""
                return True
            log.warning("WARP repair did not restore the SOCKS listener")
            return False


    def stop(self) -> None:
        with self._lock:
            if not self.available():
                return
            try:
                self._cli(["disconnect"], timeout=20)
                log.info("WARP disconnected")
            except TransportError as exc:
                log.warning("warp-cli disconnect: %s", exc)

    def new_identity(self) -> str | None:
        """Ask WARP for a fresh address; returns the new IP or None."""
        with self._lock:
            if not self.available():
                raise TransportError("warp-cli.exe not found")

            before = self.public_ip()
            settle = float(config.get(self.cfg, "warp.settle_s", 4))
            max_attempts = int(config.get(self.cfg, "warp.max_address_attempts", 4))
            allow_rereg = bool(config.get(self.cfg, "warp.allow_reregister", True))
            rotate_keys = bool(config.get(self.cfg, "warp.rotate_keys", True))

            for attempt in range(1, max_attempts + 1):
                self._address_attempts += 1
                method = "rotate-keys" if (rotate_keys and attempt <= 2) else "reregister"
                log.info("WARP new identity attempt %d/%d (%s)", attempt, max_attempts, method)
                try:
                    if method == "rotate-keys":
                        # Order matters. Rotate the keys and reset the cached
                        # Anycast peer while the tunnel is still up, then drop
                        # and re-establish. Doing the reset after disconnecting
                        # leaves the old peer in place often enough that the
                        # egress address never moves, which is what "pool
                        # exhausted" actually meant.
                        proc = self._cli(["tunnel", "rotate-keys"], timeout=30)
                        if proc.returncode != 0:
                            raise TransportError((proc.stderr or proc.stdout).strip()[:300])
                        if bool(config.get(self.cfg, "warp.reset_endpoint", True)):
                            proc = self._cli(["tunnel", "endpoint", "reset"], timeout=25)
                            if proc.returncode != 0:
                                log.debug(
                                    "WARP endpoint reset skipped: %s",
                                    (proc.stderr or proc.stdout).strip()[:160],
                                )
                        self._cli(["disconnect"], timeout=20)
                        self._cli(["connect"], timeout=25)
                    else:
                        if not allow_rereg:
                            raise TransportError("re-registration disabled by config")
                        self._cli(["disconnect"], timeout=20)
                        self._cli(["registration", "delete"], timeout=30)
                        proc = self._cli(["registration", "new"], timeout=60)
                        if proc.returncode != 0:
                            raise TransportError((proc.stderr or proc.stdout).strip()[:300])
                        self._cli(["connect"], timeout=25)
                except TransportError as exc:
                    self.last_error = str(exc)
                    log.warning("WARP identity attempt %d failed: %s", attempt, exc)
                    time.sleep(settle)
                    continue

                if not _wait_port(self.host, self.port, time.monotonic() + self.timeout):
                    log.warning("WARP SOCKS port did not return after identity change")
                    time.sleep(settle)
                    continue

                # WARP re-homes to a new colo after rotate-keys; the address does
                # not change the instant the listener is back. Poll until it moves
                # or the window closes, instead of sampling once and giving up.
                time.sleep(settle)
                after = before
                deadline = time.monotonic() + max(30.0, settle * 3)
                while time.monotonic() < deadline:
                    after = self.public_ip()
                    if after and after != before:
                        log.info("WARP new address %s -> %s", before, after)
                        self.last_error = ""
                        return after
                    time.sleep(2.0)
                if after and before is None:
                    self.last_error = ""
                    return after
                log.info("WARP returned the same address (%s); trying again", after or "unknown")

            raise TransportError(
                f"WARP address pool exhausted after {max_attempts} attempts"
            )

    def check_leak(self) -> dict:
        """Ask Cloudflare what it actually saw for traffic through this proxy.

        A dead listener and a lying listener look the same from the socket side.
        `/cdn-cgi/trace` reports the edge's view, so `warp=off` means the request
        left on the machine's own address even though the SOCKS hop succeeded.

        Returns {"ok": bool, "ip", "warp", "colo", "url", "reason"}. `ok` is None
        when the endpoint could not be reached at all — unknown, not a pass.
        """
        if not bool(config.get(self.cfg, "leak.enabled", True)):
            return {"ok": None, "reason": "leak check disabled", "ip": "", "warp": "", "colo": "", "url": ""}

        urls = [str(u) for u in (config.get(self.cfg, "leak.urls", []) or [])]
        timeout = float(config.get(self.cfg, "leak.timeout_s", 8))
        expected = {str(v).lower() for v in (config.get(self.cfg, "leak.expect_warp", []) or [])}

        last_reason = "no trace endpoint answered"
        for url in urls:
            try:
                data = httpc.trace(self.upstream, url, timeout=timeout)
            except httpc.HttpError as exc:
                last_reason = str(exc)
                continue
            if not data:
                last_reason = f"{url}: no trace fields in the response"
                continue
            warp = (data.get("warp") or "").lower()
            result = {
                "ip": data.get("ip", ""),
                "warp": warp,
                "colo": data.get("colo", ""),
                "url": url,
                "ok": None,
                "reason": "",
            }
            if not warp:
                result["reason"] = "trace carried no warp field"
                return result
            if warp in expected:
                result["ok"] = True
                result["reason"] = ""
                return result
            result["ok"] = False
            result["reason"] = f"warp={warp} — traffic is not going through WARP"
            return result

        return {"ok": None, "reason": last_reason, "ip": "", "warp": "", "colo": "", "url": ""}

    def info(self) -> dict:
        return {
            "name": self.name,
            "up": self.is_up(),
            "upstream": f"{self.host}:{self.port}",
            "mode": self._settings_cache.get("operation_mode", config.get(self.cfg, "warp.mode")),
            "protocol": self._settings_cache.get("warp_tunnel_protocol", config.get(self.cfg, "warp.tunnel_protocol")),
            "cli": self.cli or "not found",
            "error": self.last_error,
        }


# ============================================================== TOR

class TorTransport(BaseTransport):
    """Bundled or system tor.exe with SOCKS5 + control port NEWNYM rotation."""

    name = "TOR"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.exe: str | None = config.find_tor_exe(cfg)
        self.host = config.get(cfg, "tor.socks_host", "127.0.0.1")
        self.port = int(config.get(cfg, "tor.socks_port", 9050))
        self.control_host = config.get(cfg, "tor.control_host", "127.0.0.1")
        self.control_port = int(config.get(cfg, "tor.control_port", 9051))
        self.upstream = (self.host, self.port)
        self.bootstrap_timeout = float(config.get(cfg, "tor.bootstrap_timeout_s", 90))
        self.newnym_wait = float(config.get(cfg, "tor.newnym_wait_s", 12))
        self._proc: subprocess.Popen | None = None
        self._pid: int | None = None
        self._pidfile: Path | None = None
        self._logfile: Path | None = None
        self._stop = threading.Event()

    def available(self) -> bool:
        return bool(self.exe and Path(self.exe).is_file())

    # -- control port -----------------------------------------------------
    def _control(self, commands: list[str], timeout: float = 10.0) -> list[str]:
        """Speak the Tor control protocol and return 250-lines."""
        password = config.get(self.cfg, "tor.control_password", "") or ""
        try:
            sock = socket.create_connection((self.control_host, self.control_port), timeout=timeout)
        except OSError as exc:
            raise TransportError(f"cannot reach Tor control port {self.control_host}:{self.control_port}: {exc}") from exc

        replies: list[str] = []
        try:
            sock.settimeout(timeout)
            f = sock.makefile("rwb", buffering=0)

            def send(line: str) -> None:
                f.write((line + "\r\n").encode("utf-8"))

            def read_reply() -> list[str]:
                out: list[str] = []
                while True:
                    raw = f.readline()
                    if not raw:
                        raise TransportError("Tor control port closed")
                    text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    out.append(text)
                    if len(text) >= 4 and text[:3].isdigit() and text[3] == " ":
                        return out
                    if len(text) >= 4 and text[:3].isdigit() and text[3] == "-":
                        continue
                    if len(text) >= 4 and text[:3].isdigit() and text[3] == "+":
                        while True:
                            data = f.readline()
                            if not data or data in (b".\r\n", b".\n"):
                                break
                        return out
                return out

            if password:
                send(f'AUTHENTICATE "{password}"')
            else:
                send("AUTHENTICATE")
            auth = read_reply()
            if not auth or not auth[-1].startswith("250"):
                raise TransportError(f"Tor control auth failed: {auth[-1] if auth else 'no reply'}")

            for cmd in commands:
                send(cmd)
                reply = read_reply()
                replies.extend(reply)
                if reply and not reply[-1].startswith("250"):
                    raise TransportError(f"Tor control '{cmd}' failed: {reply[-1]}")
            return replies
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def bootstrap_percent(self) -> int:
        try:
            lines = self._control(["GETINFO status/bootstrap-phase"], timeout=6)
        except TransportError:
            return 0
        for line in lines:
            match = re.search(r"PROGRESS=(\d+)", line)
            if match:
                return int(match.group(1))
        return 0

    # -- lifecycle --------------------------------------------------------
    def _data_dir(self) -> Path:
        path = Path(config.expand(config.get(self.cfg, "tor.data_dir")))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _reap_stale(self) -> None:
        """Kill a tor we started in an earlier run and left behind."""
        data_dir = self._data_dir()
        self._pidfile = data_dir / "codx-tor.pid"
        self._logfile = data_dir / "tor.log"
        if not self._pidfile.is_file():
            return
        try:
            stale = int(self._pidfile.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            stale = 0
        self._pidfile.unlink(missing_ok=True)
        if not stale:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(stale), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            log.info("reaped stale tor pid %d", stale)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _free_port(self, host: str, preferred: int) -> int:
        """Return `preferred` if free, otherwise an ephemeral free port."""
        for candidate in (preferred, 0):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((host, candidate))
                port = probe.getsockname()[1]
            except OSError:
                continue
            finally:
                probe.close()
            if candidate != 0:
                return port
            return port
        raise TransportError(f"no free port available on {host}")

    def _pick_free_ports(self) -> None:
        old_socks, old_control = self.port, self.control_port
        self.port = self._free_port(self.host, self.port + 1)
        self.control_port = self._free_port(self.control_host, self.control_port + 1)
        self.upstream = (self.host, self.port)
        log.info(
            "tor ports %d/%d busy; using %d/%d instead",
            old_socks, old_control, self.port, self.control_port,
        )

    def _tor_log_tail(self, lines: int = 6) -> str:
        if not self._logfile or not self._logfile.is_file():
            return ""
        try:
            text = self._logfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
        return " | ".join(tail)

    def _build_args(self) -> list[str]:
        data_dir = Path(config.expand(config.get(self.cfg, "tor.data_dir")))
        data_dir.mkdir(parents=True, exist_ok=True)
        args = [
            self.exe or "tor.exe",
            "--SocksPort", f"{self.host}:{self.port}",
            "--ControlPort", f"{self.control_host}:{self.control_port}",
            "--CookieAuthentication", "0",
            "--DataDirectory", str(data_dir),
            "--Log", "notice stdout",
            "--ClientOnly", "1",
        ]
        # tor wants geoip files next to the binary when it can find them
        exe_dir = Path(self.exe).parent if self.exe else None
        if exe_dir:
            for name in ("geoip", "geoip6"):
                cand = exe_dir / name
                if cand.is_file():
                    flag = "--GeoIPFile" if name == "geoip" else "--GeoIPv6File"
                    args.extend([flag, str(cand)])
        password = config.get(self.cfg, "tor.control_password", "")
        if password:
            hashed = self._hash_password(password)
            if hashed:
                args.extend(["--HashedControlPassword", hashed])
        extra = config.get(self.cfg, "tor.extra_args", [])
        if isinstance(extra, list):
            args.extend(str(a) for a in extra)
        return args

    @staticmethod
    def _hash_password(password: str) -> str | None:
        """Tor's S2K hash for HashedControlPassword (RFC-style 16:8+salt+digest)."""
        import base64
        import hashlib
        import secrets

        salt = secrets.token_bytes(8)
        count = 65536
        digest = hashlib.sha1(password.encode("utf-8") + salt).digest()
        for _ in range(15):  # 2^16 / 2^12 - 1 extra rounds, per tor's s2k
            digest = hashlib.sha1(digest + password.encode("utf-8") + salt).digest()
        raw = bytes([0x60, 0x00]) + salt + digest
        return "16:" + base64.b64encode(raw).decode("ascii")

    def start(self) -> None:
        with self._lock:
            if not self.available():
                raise TransportError(
                    "tor.exe not found — set tor.exe_path, install Tor Browser, "
                    "or drop the Tor Expert Bundle into %LOCALAPPDATA%\\codx\\tor"
                )
            if self._proc is not None and self._proc.poll() is None and self.is_up():
                log.info("Tor already running")
                return

            self._stop.clear()
            self._reap_stale()

            if _port_open(self.host, self.port, 1.0):
                if not config.get(self.cfg, "tor.auto_port", True):
                    raise TransportError(
                        f"{self.host}:{self.port} is already in use by another program — "
                        f"set tor.socks_port to a free port in config.json"
                    )
                self._pick_free_ports()

            args = self._build_args()
            log.info("starting tor: %s", " ".join(args[:2]))
            try:
                handle = open(self._logfile, "a", encoding="utf-8", errors="replace")  # type: ignore[arg-type]
            except (OSError, TypeError):
                handle = None
            try:
                self._proc = subprocess.Popen(
                    args,
                    stdout=handle if handle else subprocess.DEVNULL,
                    stderr=subprocess.STDOUT if handle else subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                )
            except OSError as exc:
                if handle:
                    handle.close()
                raise TransportError(f"cannot start tor.exe: {exc}") from exc
            self._pid = self._proc.pid
            if self._pidfile:
                try:
                    self._pidfile.write_text(str(self._pid), encoding="utf-8")
                except OSError:
                    pass

            deadline = time.monotonic() + self.bootstrap_timeout
            last_pct = -1
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    detail = self._tor_log_tail()
                    raise TransportError(
                        f"tor.exe exited early with code {self._proc.returncode}"
                        + (f" — {detail}" if detail else "")
                    )
                if _port_open(self.host, self.port, 1.0):
                    pct = self.bootstrap_percent()
                    if pct >= 100:
                        ip = self.public_ip()
                        log.info("Tor up (socks %s:%d, bootstrap 100%%, ip %s)", self.host, self.port, ip or "unknown")
                        self.last_error = ""
                        return
                    if pct != last_pct:
                        log.info("Tor bootstrap %d%%", pct)
                        last_pct = pct
                time.sleep(0.6)

            self.stop()
            raise TransportError(f"Tor did not bootstrap within {self.bootstrap_timeout:.0f}s")

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            proc, self._proc = self._proc, None
            self._pid = None
            if proc is None:
                return
            if proc.poll() is not None:
                return
            try:
                # kill only the tree we started
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    creationflags=CREATE_NO_WINDOW,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                log.warning("taskkill failed for pid %s: %s", proc.pid, exc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
            if self._pidfile:
                self._pidfile.unlink(missing_ok=True)
            log.info("Tor stopped (pid %s)", proc.pid)

    def is_up(self) -> bool:
        if not _port_open(self.host, self.port, 1.0):
            return False
        try:
            socks.socks5_connect(self.host, self.port, "1.1.1.1", 443, timeout=8).close()
            return True
        except (socks.SocksError, OSError):
            return False

    def new_identity(self) -> str | None:
        with self._lock:
            if not self.is_up():
                self.start()
            before = self.public_ip()
            log.info("Tor NEWNYM (circuit rotation)")
            self._control(["SIGNAL NEWNYM"], timeout=15)
            time.sleep(self.newnym_wait)
            after = self.public_ip()
            if after:
                log.info("Tor new circuit address %s -> %s", before, after)
            self.last_error = ""
            return after

    def info(self) -> dict:
        return {
            "name": self.name,
            "up": self.is_up(),
            "upstream": f"{self.host}:{self.port}",
            "control": f"{self.control_host}:{self.control_port}",
            "exe": self.exe or "not found",
            "pid": self._pid if (self._proc is not None and self._proc.poll() is None) else None,
            "error": self.last_error,
        }
