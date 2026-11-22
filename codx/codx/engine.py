"""The state machine: WARP <-> TOR <-> COOLDOWN, rotation, watchdog, detector.

Thread-safe. The UI only reads snapshots; all mutation happens on engine
threads under one re-entrant lock.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import config, httpc, logbus, resume
from .bridge import HttpBridge
from .transports import BaseTransport, TorTransport, TransportError, WarpTransport

log = logbus.logger("engine")

Mode = Literal["WARP", "TOR", "COOLDOWN", "UNPROTECTED"]


@dataclass
class Stats:
    new_ip_count: int = 0
    last_rotation_ts: float = 0.0
    ip_history: list[dict] = field(default_factory=list)
    limit_hits: int = 0
    transport_switches: int = 0
    started_ts: float = field(default_factory=time.time)

    def record(self, ip: str | None, transport: str, reason: str, history_limit: int) -> None:
        self.new_ip_count += 1
        self.last_rotation_ts = time.time()
        self.ip_history.append(
            {
                "ip": ip or "unknown",
                "transport": transport,
                "reason": reason,
                "ts": self.last_rotation_ts,
            }
        )
        if len(self.ip_history) > history_limit:
            self.ip_history = self.ip_history[-history_limit:]


class Engine:
    """Owns both transports, the bridge, and every background loop."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.lock = threading.RLock()

        self.warp = WarpTransport(cfg)
        self.tor = TorTransport(cfg)
        self.bridge = HttpBridge(cfg)

        self.mode: Mode = "UNPROTECTED"
        self.current_ip: str | None = None
        self.last_error: str = ""
        self.cooldown_until: float = 0.0          # monotonic deadline
        self.cooldown_started: float = 0.0
        self.cooldown_total_s: float = 0.0

        self.stats = Stats()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._rotate_lock = threading.Lock()
        self._last_rotation_mono: float = 0.0
        self._health_failures = 0
        self._cooldown_event = threading.Event()
        self._busy: str = ""
        self._last_probe_ts: float = 0.0
        self._last_probe_status: str = "n/a"
        self._started_at = time.time()
        self._last_leak_mono: float = 0.0
        self._last_leak: dict = {}
        self._resume_lock = threading.Lock()
        self._resume_running = False
        self._last_resume: str = ""

        self.auto_rotate = bool(config.get(cfg, "rotation.auto_rotate", True))
        self.background = bool(config.get(cfg, "rotation.background_protection", True))

        self._load_state()

    # ------------------------------------------------------------ persistence

    def _state_path(self) -> Path | None:
        raw = config.get(self.cfg, "state_file")
        return Path(config.expand(raw)) if raw else None

    def _load_state(self) -> None:
        path = self._state_path()
        if not path or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("state file unreadable (%s): %s", path, exc)
            return
        self.stats.new_ip_count = int(data.get("new_ip_count", 0) or 0)
        self.stats.last_rotation_ts = float(data.get("last_rotation_ts", 0) or 0)
        self.stats.limit_hits = int(data.get("limit_hits", 0) or 0)
        self.stats.transport_switches = int(data.get("transport_switches", 0) or 0)
        history = data.get("ip_history", [])
        if isinstance(history, list):
            self.stats.ip_history = [h for h in history if isinstance(h, dict)][-200:]

    def _save_state(self) -> None:
        path = self._state_path()
        if not path:
            return
        payload = {
            "new_ip_count": self.stats.new_ip_count,
            "last_rotation_ts": self.stats.last_rotation_ts,
            "limit_hits": self.stats.limit_hits,
            "transport_switches": self.stats.transport_switches,
            "ip_history": self.stats.ip_history[-200:],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("could not persist state: %s", exc)

    # ---------------------------------------------------------------- accessors

    def active_transport(self) -> BaseTransport | None:
        if self.mode == "WARP":
            return self.warp
        # COOLDOWN means "Tor is carrying traffic while WARP's pool refills", so
        # the live transport is still Tor — the UI must not report "none".
        if self.mode in {"TOR", "COOLDOWN"}:
            return self.tor
        return None

    def upstream(self) -> tuple[str, int] | None:
        transport = self.active_transport()
        return transport.upstream if transport else None

    @property
    def busy(self) -> str:
        with self.lock:
            return self._busy

    def _set_busy(self, text: str) -> None:
        with self.lock:
            self._busy = text

    # -------------------------------------------------------------- transitions

    def connect_warp(self) -> bool:
        """Bring WARP up and point the bridge at it."""
        with self.lock:
            self._set_busy("connecting WARP")
            self._cooldown_event.set()
        try:
            if self.tor.is_up():
                self.tor.stop()
            self.warp.start()
            ip = self.warp.public_ip()
            with self.lock:
                self.mode = "WARP"
                self.current_ip = ip
                self.last_error = ""
                self.cooldown_until = 0.0
                self.cooldown_total_s = 0.0
                self._health_failures = 0
                self.bridge.set_upstream(self.warp.upstream)
            log.info("mode -> WARP (ip %s)", ip or "unknown")
            return True
        except (TransportError, OSError) as exc:
            with self.lock:
                self.last_error = f"WARP: {exc}"
            log.error("WARP connect failed: %s", exc)
            return False
        finally:
            self._set_busy("")

    def connect_tor(self, start_cooldown: bool = True) -> bool:
        """Bring Tor up, point the bridge at it, arm the return-to-WARP timer."""
        with self.lock:
            self._set_busy("connecting Tor")
        try:
            if self.warp.is_up():
                self.warp.stop()
            self.tor.start()
            ip = self.tor.public_ip()
            with self.lock:
                if self.mode != "TOR":
                    self.stats.transport_switches += 1
                self.mode = "TOR"
                self.current_ip = ip
                self.last_error = ""
                self._health_failures = 0
                self.bridge.set_upstream(self.tor.upstream)
            log.info("mode -> TOR (ip %s)", ip or "unknown")
            if start_cooldown:
                self._arm_cooldown()
            return True
        except (TransportError, OSError) as exc:
            with self.lock:
                self.last_error = f"TOR: {exc}"
            log.error("Tor connect failed: %s", exc)
            self._go_unprotected(f"Tor unavailable: {exc}")
            return False
        finally:
            self._set_busy("")

    def _go_unprotected(self, why: str) -> None:
        """No working transport: cut the bridge so nothing leaks direct."""
        with self.lock:
            self.mode = "UNPROTECTED"
            self.current_ip = None
            self.last_error = why
            self.bridge.set_upstream(None)
            self.cooldown_until = 0.0
        log.error("NOT PROTECTED — %s", why)

    # ----------------------------------------------------------------- cooldown

    def cooldown_remaining(self) -> float:
        with self.lock:
            if self.mode != "COOLDOWN" or self.cooldown_until <= 0:
                return 0.0
            return max(0.0, self.cooldown_until - time.monotonic())

    def _arm_cooldown(self) -> None:
        minutes = float(config.get(self.cfg, "rotation.cooldown_minutes", 125))
        if minutes <= 0:
            log.info("cooldown disabled; staying on Tor")
            with self.lock:
                self.mode = "TOR"
                self.cooldown_until = 0.0
            return
        with self.lock:
            self.mode = "COOLDOWN"
            self.cooldown_started = time.monotonic()
            self.cooldown_total_s = minutes * 60.0
            self.cooldown_until = self.cooldown_started + self.cooldown_total_s
        log.info("cooldown armed: %s until WARP retry", format_hms(self.cooldown_total_s))
        self._cooldown_event.set()

    def _cooldown_loop(self) -> None:
        while not self._stop.is_set():
            self._cooldown_event.wait(timeout=1.0)
            if self._stop.is_set():
                return
            with self.lock:
                armed = self.mode == "COOLDOWN" and self.cooldown_until > 0
                deadline = self.cooldown_until
            if not armed:
                self._cooldown_event.clear()
                continue
            if time.monotonic() < deadline:
                continue
            self._cooldown_event.clear()
            log.info("cooldown elapsed; trying WARP again")
            if not self.connect_warp():
                # stay on Tor and re-arm rather than dropping the user to direct
                if self.tor.is_up():
                    with self.lock:
                        self.mode = "TOR"
                        self.bridge.set_upstream(self.tor.upstream)
                    self._arm_cooldown()
                else:
                    self._go_unprotected("WARP retry failed and Tor is down")

    # ---------------------------------------------------------------- rotation

    def get_new_ip(self, reason: str = "manual") -> tuple[bool, str]:
        """Rotate the egress address. Debounced, transport-aware, fail-closed."""
        if not self._rotate_lock.acquire(blocking=False):
            return False, "rotation already in progress"

        try:
            min_interval = float(config.get(self.cfg, "rotation.min_interval_s", 10))
            elapsed = time.monotonic() - self._last_rotation_mono
            if self._last_rotation_mono and elapsed < min_interval:
                wait = min_interval - elapsed
                return False, f"debounced — {wait:.0f}s until next rotation allowed"

            with self.lock:
                mode = self.mode
            self._set_busy("rotating")
            log.info("new IP requested (%s) from %s", reason, mode)

            try:
                if mode in {"WARP", "COOLDOWN", "UNPROTECTED"}:
                    if mode == "COOLDOWN":
                        log.info("rotation during cooldown; WARP pool considered exhausted")
                        if self.connect_tor(start_cooldown=False):
                            ip = self.tor.new_identity()
                            self._record_rotation(ip, "TOR", reason)
                            self._arm_cooldown()
                            return True, f"rotated on TOR (ip {ip or 'unknown'})"
                        return False, self.last_error or "Tor unavailable"

                    try:
                        ip = self.warp.new_identity()
                    except TransportError as exc:
                        log.warning("WARP pool exhausted (%s); falling back to Tor", exc)
                        if not self.connect_tor(start_cooldown=True):
                            return False, self.last_error or "both transports failed"
                        ip = self.tor.public_ip()
                        self._record_rotation(ip, "TOR", f"{reason} (warp exhausted)")
                        return True, f"WARP exhausted, rotated on TOR (ip {ip or 'unknown'})"
                    self._record_rotation(ip, "WARP", reason)
                    return True, f"rotated on WARP (ip {ip or 'unknown'})"

                if mode == "TOR":
                    ip = self.tor.new_identity()
                    self._record_rotation(ip, "TOR", reason)
                    return True, f"rotated on TOR (ip {ip or 'unknown'})"

                if not self.connect_warp():
                    if not self.connect_tor():
                        return False, self.last_error or "no transport available"
                    ip = self.tor.public_ip()
                    self._record_rotation(ip, "TOR", reason)
                    return True, f"rotated on TOR (ip {ip or 'unknown'})"
                ip = self.warp.public_ip()
                self._record_rotation(ip, "WARP", reason)
                return True, f"rotated on WARP (ip {ip or 'unknown'})"
            except (TransportError, OSError) as exc:
                log.error("rotation failed: %s", exc)
                with self.lock:
                    self.last_error = str(exc)
                return False, str(exc)
            finally:
                self._set_busy("")
        finally:
            self._rotate_lock.release()

    def _record_rotation(self, ip: str | None, transport: str, reason: str) -> None:
        limit = int(config.get(self.cfg, "ip.history_limit", 200))
        with self.lock:
            self.stats.record(ip, transport, reason, limit)
            self.current_ip = ip
            self._last_rotation_mono = time.monotonic()
        self._save_state()
        log.info("rotation #%d recorded (%s, ip %s)", self.stats.new_ip_count, transport, ip or "unknown")
        # The app keeps its API connection open for minutes. Left alone, that
        # tunnel would carry the resume out on the address that was just spent
        # and the limit would come straight back. Drop it before nudging.
        try:
            self.bridge.drop_tunnels("rotation")
        except Exception as exc:  # a bridge hiccup must not block the rotation
            log.debug("bridge tunnel drop failed: %s", exc)
        # A new address on its own changes nothing the user can see: the desktop
        # app still holds its own lockout and the chat is still stopped. Clear the
        # lockout and nudge the sessions on a worker so the rotation call returns
        # immediately.
        if bool(config.get(self.cfg, "resume.auto_after_rotation", True)):
            self.resume_in_background(reason)

    # ------------------------------------------------------------------ resume

    def resume_in_background(self, reason: str = "") -> bool:
        """Start the lockout-clear + chat-resume pass without blocking the caller."""
        if not bool(config.get(self.cfg, "resume.enabled", True)):
            return False
        with self._resume_lock:
            if self._resume_running:
                log.debug("resume already running; not stacking another")
                return False
            self._resume_running = True

        def work() -> None:
            try:
                report = resume.resume(self.cfg, self._proxy_url())
                text = report.summary()
                with self.lock:
                    self._last_resume = text
                log.info("resume: %s", text)
                for note in report.notes:
                    log.warning("resume note: %s", note)
            except Exception as exc:  # a resume must never take the engine down
                log.error("resume failed: %s", exc)
                with self.lock:
                    self._last_resume = f"failed: {exc}"
            finally:
                with self._resume_lock:
                    self._resume_running = False

        threading.Thread(target=work, name="engine-resume", daemon=True).start()
        return True

    def _proxy_url(self) -> str | None:
        try:
            return self.bridge.proxy_url
        except Exception:  # bridge not up yet
            return None


    # ------------------------------------------------------------------ health

    def health_check(self) -> bool:
        """One health pass. Returns True when the active transport is usable."""
        with self.lock:
            mode = self.mode
        transport = self.active_transport()
        if transport is None:
            return False
        try:
            ok = transport.is_up()
        except Exception as exc:  # never let a transport kill the watchdog
            log.debug("health probe raised: %s", exc)
            ok = False
        if ok and mode == "WARP":
            ok = self._leak_pass()
        if ok:
            with self.lock:
                self._health_failures = 0
            return True
        with self.lock:
            self._health_failures += 1
            failures = self._health_failures
        log.warning("health check failed for %s (%d)", mode, failures)
        return False

    def _leak_pass(self) -> bool:
        """Confirm Cloudflare's edge actually saw WARP, not the machine.

        A SOCKS listener that accepts connections and forwards them on the local
        address is indistinguishable from a working one at the socket level, and
        the target app would then be rate-limited by the user's own IP while the
        UI claimed protection. This is the only check that catches it.

        Returns True when there is no evidence of a leak. Runs at most once per
        `leak.interval_s` so it does not add a request to every health tick.
        """
        if not bool(config.get(self.cfg, "leak.enabled", True)):
            return True
        interval = float(config.get(self.cfg, "leak.interval_s", 60))
        now = time.monotonic()
        with self.lock:
            last = self._last_leak_mono
        if last and (now - last) < interval:
            return True

        result = self.warp.check_leak()
        with self.lock:
            self._last_leak_mono = now
            self._last_leak = result
        if result.get("ok") is True:
            log.debug(
                "leak check ok (warp=%s colo=%s ip=%s)",
                result.get("warp"),
                result.get("colo"),
                result.get("ip"),
            )
            return True
        if result.get("ok") is False:
            log.warning("LEAK: %s (colo %s, ip %s)", result.get("reason"), result.get("colo"), result.get("ip"))
            return False
        log.debug("leak check inconclusive: %s", result.get("reason"))
        return True


    def _health_loop(self) -> None:
        interval = float(config.get(self.cfg, "health.interval_s", 20))
        warp_interval = float(config.get(self.cfg, "health.warp_interval_s", interval))
        threshold = int(config.get(self.cfg, "health.failures_before_switch", 3))
        while True:
            with self.lock:
                mode_now = self.mode
            # WARP's listener can vanish when the vendor GUI rewrites settings,
            # so it is watched more often than the Tor circuit, which only dies
            # when the process does.
            wait = warp_interval if mode_now == "WARP" else interval
            if self._stop.wait(wait):
                return
            with self.lock:
                background = self.background
            if not background:
                continue
            if self.health_check():
                continue
            with self.lock:
                failures = self._health_failures
                mode = self.mode
            if failures < threshold:
                continue
            log.warning("%d consecutive health failures on %s; recovering", failures, mode)
            if mode == "WARP":
                # Repair first: the usual cause is mode drift from the WARP GUI,
                # which a cheap re-assert fixes in seconds. Only a failed repair
                # justifies dropping the whole transport and rebuilding it.
                if self.warp.repair():
                    with self.lock:
                        self._health_failures = 0
                        self.bridge.set_upstream(self.warp.upstream)
                        self.mode = "WARP"
                    continue
                if self.connect_warp():
                    continue
                log.warning("WARP restart failed; switching to Tor")
                self.connect_tor(start_cooldown=True)
            elif mode in {"TOR", "COOLDOWN"}:
                if self.tor.is_up():
                    with self.lock:
                        self.mode = "TOR"
                        self.bridge.set_upstream(self.tor.upstream)
                        self._health_failures = 0
                    continue
                if not self.connect_tor(start_cooldown=(mode == "COOLDOWN")):
                    self._go_unprotected("Tor watchdog could not restart the transport")
            else:
                if not self.connect_warp() and not self.connect_tor():
                    self._go_unprotected("no transport recovered")

    # ---------------------------------------------------------------- detector

    def probe(self) -> dict:
        """Cheap request through the active proxy; reports limit state."""
        url = config.get(self.cfg, "probe.url")
        method = str(config.get(self.cfg, "probe.method", "GET")).upper()
        timeout = float(config.get(self.cfg, "probe.timeout_s", 12))
        max_body = int(config.get(self.cfg, "probe.max_body_bytes", 65536))
        matches = [m.lower() for m in config.get(self.cfg, "probe.match_strings", [])]
        upstream = self.upstream()

        result = {
            "ts": time.time(),
            "status": None,
            "limited": False,
            "detail": "",
        }
        if upstream is None:
            result["detail"] = "no transport"
            with self.lock:
                self._last_probe_status = "no transport"
            return result

        try:
            resp = httpc.request(
                upstream,
                url,
                method=method,
                timeout=timeout,
                max_body=max_body,
                extra_headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            )
        except httpc.HttpError as exc:
            result["detail"] = str(exc)
            with self.lock:
                self._last_probe_status = f"error: {exc}"
            return result

        body = resp.text.lower()
        hit = resp.status == 429 or any(m and m in body for m in matches)
        result.update(
            status=resp.status,
            limited=hit,
            detail=(resp.reason or "")[:120],
        )
        with self.lock:
            self._last_probe_ts = result["ts"]
            self._last_probe_status = f"{resp.status} {'LIMIT' if hit else 'ok'}"
        if hit:
            with self.lock:
                self.stats.limit_hits += 1
            log.warning("rate limit detected (%d %s)", resp.status, resp.reason[:60])
            self._save_state()
        return result

    def _detector_loop(self) -> None:
        interval = float(config.get(self.cfg, "probe.interval_s", 30))
        while not self._stop.wait(interval):
            with self.lock:
                if not self.background:
                    continue
                auto = self.auto_rotate
            try:
                result = self.probe()
            except Exception as exc:  # detector must never die
                log.debug("probe raised: %s", exc)
                continue
            if result["limited"] and auto:
                ok, msg = self.get_new_ip("rate limit")
                log.info("auto-rotate: %s", msg)

    # ----------------------------------------------------------------- control

    def start(self) -> None:
        """Bring the bridge up and pick a transport."""
        log.info("engine starting")
        self.bridge.start()
        with self.lock:
            self.background = bool(config.get(self.cfg, "rotation.background_protection", True))

        connected = self.connect_warp()
        if not connected:
            log.warning("WARP unavailable at startup; trying Tor")
            connected = self.connect_tor(start_cooldown=False)
        if not connected:
            self._go_unprotected("neither WARP nor Tor could be started — see logs")

        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._cooldown_loop, name="engine-cooldown", daemon=True),
            threading.Thread(target=self._health_loop, name="engine-health", daemon=True),
            threading.Thread(target=self._detector_loop, name="engine-detector", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Ordered teardown: loops, bridge, transports, restore WARP, state."""
        log.info("engine stopping")
        self._stop.set()
        self._cooldown_event.set()
        for thread in self._threads:
            thread.join(timeout=3.0)
        self._threads.clear()

        self.bridge.stop()

        try:
            self.tor.stop()
        except Exception as exc:
            log.warning("Tor teardown: %s", exc)

        try:
            if self.warp.available():
                self.warp.stop()
                self.warp.restore_original()
        except Exception as exc:
            log.warning("WARP teardown: %s", exc)

        self._save_state()
        with self.lock:
            self.mode = "UNPROTECTED"
            self.current_ip = None
            self.bridge.set_upstream(None)
        log.info("engine stopped")

    # ------------------------------------------------------------------- views

    def set_auto_rotate(self, value: bool) -> None:
        with self.lock:
            self.auto_rotate = bool(value)
        self.cfg.setdefault("rotation", {})["auto_rotate"] = bool(value)
        log.info("auto-rotate %s", "ON" if value else "OFF")

    def set_background(self, value: bool) -> None:
        with self.lock:
            self.background = bool(value)
        self.cfg.setdefault("rotation", {})["background_protection"] = bool(value)
        log.info("background protection %s", "ON" if value else "OFF")

    def repair_restart_transport(self) -> tuple[bool, str]:
        with self.lock:
            mode = self.mode
        if mode in {"TOR", "COOLDOWN"}:
            self.tor.stop()
            return (True, "Tor restarted") if self.connect_tor(start_cooldown=(mode == "COOLDOWN")) else (False, self.last_error)
        self.warp.stop()
        return (True, "WARP restarted") if self.connect_warp() else (False, self.last_error)

    def repair_force_tor(self) -> tuple[bool, str]:
        return (True, "switched to TOR") if self.connect_tor(start_cooldown=True) else (False, self.last_error)

    def repair_force_warp(self) -> tuple[bool, str]:
        return (True, "switched to WARP") if self.connect_warp() else (False, self.last_error)

    def repair_recheck(self) -> tuple[bool, str]:
        ok = self.health_check()
        if ok:
            transport = self.active_transport()
            ip = transport.public_ip() if transport else None
            with self.lock:
                self.current_ip = ip
            return True, f"transport healthy (ip {ip or 'unknown'})"
        return False, "transport is not answering"

    def repair_clear_lockout(self) -> tuple[bool, str]:
        """Clear the client-side limit rows and resume the chats, synchronously."""
        try:
            report = resume.resume(self.cfg, self._proxy_url())
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            with self.lock:
                self._last_resume = f"failed: {exc}"
            return False, str(exc)
        text = report.summary()
        with self.lock:
            self._last_resume = text
        log.info("resume: %s", text)
        return (report.cleared.ok and not report.notes), text

    def snapshot(self) -> dict:
        with self.lock:
            mode = self.mode
            remaining = self.cooldown_remaining() if mode == "COOLDOWN" else 0.0
            transport = self.active_transport()
            info = transport.info() if transport else {"name": "none", "up": False}
            bridge_stats = self.bridge.stats()
            return {
                "mode": mode,
                "protected": mode in {"WARP", "TOR", "COOLDOWN"} and self.bridge.get_upstream() is not None,
                "busy": self._busy,
                "ip": self.current_ip,
                "transport": info,
                "upstream": f"{self.bridge.get_upstream()[0]}:{self.bridge.get_upstream()[1]}"
                if self.bridge.get_upstream() else None,
                "proxy_url": self.bridge.proxy_url,
                "bridge_running": self.bridge.running,
                "bridge": bridge_stats,
                "cooldown_remaining_s": remaining,
                "cooldown_total_s": self.cooldown_total_s,
                "new_ip_count": self.stats.new_ip_count,
                "last_rotation_ts": self.stats.last_rotation_ts,
                "limit_hits": self.stats.limit_hits,
                "transport_switches": self.stats.transport_switches,
                "ip_history": list(self.stats.ip_history[-40:]),
                "auto_rotate": self.auto_rotate,
                "background": self.background,
                "last_error": self.last_error,
                "last_probe": self._last_probe_status,
                "leak": dict(self._last_leak),
                "resume_running": self._resume_running,
                "last_resume": self._last_resume,
                "tunnels_dropped": getattr(self.bridge, "tunnels_dropped", 0),
                "uptime_s": time.time() - self._started_at,
            }


def format_hms(seconds: float) -> str:
    """HH:MM, or HH:MM:SS under an hour so short cooldowns stay readable."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours == 0 and minutes == 0:
        return f"{secs:02d}s"
    if hours == 0:
        return f"{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}"
