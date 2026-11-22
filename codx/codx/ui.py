"""Tkinter UI: status banner, counters, actions, toggles, log + repair panels."""
from __future__ import annotations

import os
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import config, logbus
from .engine import Engine, format_hms
from .launcher import Launcher

log = logbus.logger("ui")

BG = "#11151a"
PANEL = "#1a2028"
FG = "#e7edf3"
MUTED = "#8b98a6"
GREEN = "#1f8a4c"
GREEN_DIM = "#14603a"
AMBER = "#9a7411"
RED = "#a02020"
ACCENT = "#2f81f7"


class CodxUI:
    def __init__(self, cfg: dict, engine: Engine, cfg_path: Path) -> None:
        self.cfg = cfg
        self.engine = engine
        self.cfg_path = cfg_path
        self.launcher = Launcher(cfg, engine.bridge.proxy_url)

        self.root = tk.Tk()
        self.root.title(f"{config.APP_NAME} {config.APP_VERSION} — {config.APP_TAGLINE}")
        self.root.geometry("880x680")
        self.root.minsize(820, 620)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.tray = None
        self._closing = False
        self._last_log_ts = 0.0
        self._log_lines: list[str] = []

        self._build_styles()
        self._build_layout()
        self._start_tray()
        self.root.after(int(config.get(cfg, "ui.refresh_ms", 1000)), self._tick)

    # ------------------------------------------------------------------ styles

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=PANEL, foreground=FG, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Key.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Value.TLabel", background=PANEL, foreground=FG, font=("Segoe UI Semibold", 10))
        style.configure("TCheckbutton", background=PANEL, foreground=FG, font=("Segoe UI", 10))
        style.map("TCheckbutton", background=[("active", PANEL)], foreground=[("active", FG)])
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(12, 7))
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=("Segoe UI", 10), padding=(14, 7))
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                        font=("Consolas", 9), rowheight=22, borderwidth=0)
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#243040")])

    # ------------------------------------------------------------------ layout

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="TFrame", padding=14)
        outer.pack(fill="both", expand=True)

        # banner -----------------------------------------------------------
        self.banner = tk.Frame(outer, bg=RED, height=92)
        self.banner.pack(fill="x")
        self.banner.pack_propagate(False)
        self.banner_title = tk.Label(
            self.banner, text="NOT PROTECTED", bg=RED, fg="white",
            font=("Segoe UI Semibold", 20), anchor="w",
        )
        self.banner_title.pack(fill="x", padx=18, pady=(14, 0))
        self.banner_sub = tk.Label(
            self.banner, text="starting…", bg=RED, fg="#e8e8e8",
            font=("Segoe UI", 10), anchor="w",
        )
        self.banner_sub.pack(fill="x", padx=18, pady=(2, 12))

        # stats ------------------------------------------------------------
        stats = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        stats.pack(fill="x", pady=(12, 0))
        for col in range(4):
            stats.columnconfigure(col, weight=1)

        self.vals: dict[str, ttk.Label] = {}
        cells = [
            ("connection", "Connection type", 0, 0),
            ("ip", "Current public IP", 0, 1),
            ("newips", "New IPs so far", 0, 2),
            ("lastrot", "Last rotation", 0, 3),
            ("proxy", "App proxy (HTTP)", 1, 0),
            ("upstream", "Upstream SOCKS5", 1, 1),
            ("cooldown", "Cooldown to WARP", 1, 2),
            ("probe", "Last probe", 1, 3),
        ]
        for key, label, row, col in cells:
            cell = ttk.Frame(stats, style="Panel.TFrame")
            cell.grid(row=row, column=col, sticky="nsew", padx=(0, 10), pady=(0, 10))
            ttk.Label(cell, text=label, style="Key.TLabel").pack(anchor="w")
            value = ttk.Label(cell, text="—", style="Value.TLabel")
            value.pack(anchor="w")
            self.vals[key] = value

        # actions ----------------------------------------------------------
        actions = ttk.Frame(outer, style="TFrame")
        actions.pack(fill="x", pady=(12, 0))

        self.btn_launch = ttk.Button(actions, text="Launch app", style="Accent.TButton",
                                     command=self._on_launch_app)
        self.btn_launch.pack(side="left")
        self.btn_cli = ttk.Button(actions, text="Launch CLI", command=self._on_launch_cli)
        self.btn_cli.pack(side="left", padx=(8, 0))
        self.btn_newip = ttk.Button(actions, text="Get a new IP", command=self._on_new_ip)
        self.btn_newip.pack(side="left", padx=(8, 0))
        self.btn_repair = ttk.Button(actions, text="Repair", command=self._on_repair)
        self.btn_repair.pack(side="left", padx=(8, 0))

        toggles = ttk.Frame(actions, style="TFrame")
        toggles.pack(side="right")
        self.var_background = tk.BooleanVar(value=bool(config.get(self.cfg, "rotation.background_protection", True)))
        self.var_auto = tk.BooleanVar(value=bool(config.get(self.cfg, "rotation.auto_rotate", True)))
        ttk.Checkbutton(toggles, text="Background protection", variable=self.var_background,
                        command=self._on_toggle_background).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(toggles, text="Auto-rotate on limit", variable=self.var_auto,
                        command=self._on_toggle_auto).pack(side="left")

        # notebook ---------------------------------------------------------
        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill="both", expand=True, pady=(12, 0))

        self._build_logs_tab()
        self._build_connection_tab()
        self._build_history_tab()
        self._build_repair_tab()

        self.status_bar = ttk.Label(outer, text=f"{config.APP_NAME} {config.APP_VERSION} · config: {self.cfg_path}",
                                    style="TLabel", foreground=MUTED)
        self.status_bar.pack(fill="x", pady=(8, 0))

    def _build_logs_tab(self) -> None:
        frame = ttk.Frame(self.tabs, style="Panel.TFrame", padding=8)
        self.tabs.add(frame, text="Logs")
        self.log_text = tk.Text(frame, bg=PANEL, fg=FG, insertbackground=FG, wrap="none",
                                font=("Consolas", 9), borderwidth=0, highlightthickness=0)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_connection_tab(self) -> None:
        frame = ttk.Frame(self.tabs, style="Panel.TFrame", padding=14)
        self.tabs.add(frame, text="Connection")
        self.conn_text = tk.Text(frame, bg=PANEL, fg=FG, wrap="word", font=("Consolas", 9),
                                 borderwidth=0, highlightthickness=0, height=18)
        self.conn_text.pack(fill="both", expand=True)
        self.conn_text.configure(state="disabled")

    def _build_history_tab(self) -> None:
        frame = ttk.Frame(self.tabs, style="Panel.TFrame", padding=8)
        self.tabs.add(frame, text="IP history")
        cols = ("time", "ip", "transport", "reason")
        self.history = ttk.Treeview(frame, columns=cols, show="headings", height=14)
        widths = {"time": 90, "ip": 170, "transport": 100, "reason": 380}
        heads = {"time": "Time", "ip": "Public IP", "transport": "Transport", "reason": "Reason"}
        for col in cols:
            self.history.heading(col, text=heads[col])
            self.history.column(col, width=widths[col], anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.history.yview)
        self.history.configure(yscrollcommand=scroll.set)
        self.history.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _build_repair_tab(self) -> None:
        frame = ttk.Frame(self.tabs, style="Panel.TFrame", padding=14)
        self.tabs.add(frame, text="Repair")
        ttk.Label(frame, text="Recovery tools", style="Panel.TLabel",
                  font=("Segoe UI Semibold", 11)).pack(anchor="w")
        ttk.Label(frame, text="Use these when a transport wedges or the state looks wrong.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        row = ttk.Frame(frame, style="Panel.TFrame")
        row.pack(fill="x", pady=(0, 10))
        ttk.Button(row, text="Restart transport", command=self._repair_restart).pack(side="left")
        ttk.Button(row, text="Force WARP", command=self._repair_warp).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Force Tor", command=self._repair_tor).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Recheck health", command=self._repair_recheck).pack(side="left", padx=(8, 0))

        row2 = ttk.Frame(frame, style="Panel.TFrame")
        row2.pack(fill="x", pady=(0, 10))
        ttk.Button(row2, text="Run probe now", command=self._repair_probe).pack(side="left")
        ttk.Button(row2, text="Clear limit & resume", command=self._repair_resume).pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="Open config.json", command=self._open_config).pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="Open log file", command=self._open_log).pack(side="left", padx=(8, 0))

        row3 = ttk.Frame(frame, style="Panel.TFrame")
        row3.pack(fill="x", pady=(0, 10))
        ttk.Button(row3, text="Re-enforce WARP mode", command=self._repair_enforce).pack(side="left")
        ttk.Button(row3, text="Check for leak", command=self._repair_leak).pack(side="left", padx=(8, 0))

        self.repair_out = tk.Text(frame, bg=PANEL, fg=MUTED, wrap="word", font=("Consolas", 9),
                                  borderwidth=0, highlightthickness=0, height=10)
        self.repair_out.pack(fill="both", expand=True, pady=(10, 0))
        self.repair_out.configure(state="disabled")

    # -------------------------------------------------------------------- tray

    def _start_tray(self) -> None:
        try:
            from .tray import TrayIcon

            self.tray = TrayIcon(
                title=config.APP_NAME,
                tooltip=f"{config.APP_NAME} — starting",
                on_show=lambda: self.root.after(0, self._show_window),
                on_new_ip=lambda: self.root.after(0, self._on_new_ip),
                on_toggle_auto=lambda: self.root.after(0, self._toggle_auto_from_tray),
                on_quit=lambda: self.root.after(0, self._quit),
            )
            if not self.tray.start():
                self.tray = None
        except Exception as exc:
            log.warning("tray unavailable: %s", exc)
            self.tray = None

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _toggle_auto_from_tray(self) -> None:
        self.var_auto.set(not self.var_auto.get())
        self._on_toggle_auto()

    # ------------------------------------------------------------------ events

    def _on_launch_app(self) -> None:
        ok, msg = self.launcher.launch_app()
        self._flash_repair(msg)
        if not ok:
            messagebox.showwarning(config.APP_NAME, msg, parent=self.root)

    def _on_launch_cli(self) -> None:
        ok, msg = self.launcher.launch_cli()
        self._flash_repair(msg)
        if not ok:
            messagebox.showwarning(config.APP_NAME, msg, parent=self.root)

    def _on_new_ip(self) -> None:
        if self.engine.busy:
            return
        self.btn_newip.state(["disabled"])
        self.btn_newip.configure(text="Working…")

        def work() -> None:
            ok, msg = self.engine.get_new_ip("manual")
            self.root.after(0, lambda: self._rotation_done(ok, msg))

        threading.Thread(target=work, name="ui-rotate", daemon=True).start()

    def _rotation_done(self, ok: bool, msg: str) -> None:
        self.btn_newip.state(["!disabled"])
        self.btn_newip.configure(text="Get a new IP")
        self._flash_repair(msg)
        log.info("manual rotation: %s", msg)

    def _on_toggle_auto(self) -> None:
        self.engine.set_auto_rotate(self.var_auto.get())
        if self.tray is not None:
            self.tray.set_auto_state(self.var_auto.get())
        self._save_config_quietly()

    def _on_toggle_background(self) -> None:
        self.engine.set_background(self.var_background.get())
        self._save_config_quietly()

    def _on_repair(self) -> None:
        self.tabs.select(3)

    def _save_config_quietly(self) -> None:
        try:
            config.save(self.cfg, self.cfg_path)
        except OSError as exc:
            log.warning("could not save config: %s", exc)

    def _open_config(self) -> None:
        try:
            os.startfile(self.cfg_path)  # noqa: S606 - user-requested open
        except OSError as exc:
            messagebox.showerror(config.APP_NAME, f"cannot open {self.cfg_path}: {exc}", parent=self.root)

    def _open_log(self) -> None:
        path = config.expand(config.get(self.cfg, "logging.file", "") or "")
        if not path or not Path(path).is_file():
            messagebox.showinfo(config.APP_NAME, "No log file yet.", parent=self.root)
            return
        try:
            os.startfile(path)  # noqa: S606
        except OSError as exc:
            messagebox.showerror(config.APP_NAME, f"cannot open {path}: {exc}", parent=self.root)

    # ---------------------------------------------------------------- repairs

    def _flash_repair(self, message: str) -> None:
        self.repair_out.configure(state="normal")
        self.repair_out.insert("end", f"{time.strftime('%H:%M:%S')}  {message}\n")
        self.repair_out.see("end")
        self.repair_out.configure(state="disabled")

    def _run_repair(self, fn, label: str) -> None:
        self._flash_repair(f"{label}…")

        def work() -> None:
            try:
                ok, msg = fn()
            except Exception as exc:
                ok, msg = False, f"{label} raised: {exc}"
            self.root.after(0, lambda: self._flash_repair(f"{'OK' if ok else 'FAILED'}: {msg}"))

        threading.Thread(target=work, name="ui-repair", daemon=True).start()

    def _repair_restart(self) -> None:
        self._run_repair(self.engine.repair_restart_transport, "Restart transport")

    def _repair_warp(self) -> None:
        self._run_repair(self.engine.repair_force_warp, "Force WARP")

    def _repair_tor(self) -> None:
        self._run_repair(self.engine.repair_force_tor, "Force Tor")

    def _repair_recheck(self) -> None:
        self._run_repair(self.engine.repair_recheck, "Recheck health")

    def _repair_probe(self) -> None:
        def probe():
            result = self.engine.probe()
            if result["status"] is None:
                return False, result["detail"]
            state = "LIMIT HIT" if result["limited"] else "clear"
            return True, f"probe {result['status']} {result['detail']} — {state}"

        self._run_repair(probe, "Probe")

    def _repair_enforce(self) -> None:
        def enforce():
            self.engine.warp.enforce_mode()
            settings = self.engine.warp.settings()
            return True, (
                f"mode={settings.get('operation_mode')} "
                f"protocol={settings.get('warp_tunnel_protocol')}"
            )

        self._run_repair(enforce, "Enforce WARP mode")

    def _repair_resume(self) -> None:
        def run():
            ok, msg = self.engine.repair_clear_lockout()
            return ok, msg

        self._run_repair(run, "Clear limit & resume")

    def _repair_leak(self) -> None:
        def check():
            result = self.engine.warp.check_leak()
            if result.get("ok") is True:
                return True, (
                    f"no leak — warp={result.get('warp')} colo={result.get('colo')} "
                    f"ip={result.get('ip')}"
                )
            if result.get("ok") is False:
                return False, (
                    f"LEAK — {result.get('reason')} "
                    f"(colo {result.get('colo')}, ip {result.get('ip')})"
                )
            return False, f"inconclusive — {result.get('reason')}"

        self._run_repair(check, "Leak check")

    # ------------------------------------------------------------------- tick

    def _tick(self) -> None:
        if self._closing:
            return
        try:
            snap = self.engine.snapshot()
            self._render(snap)
        except Exception as exc:
            log.debug("render failed: %s", exc)
        delay = int(config.get(self.cfg, "ui.refresh_ms", 1000))
        self.root.after(delay, self._tick)

    def _render(self, snap: dict) -> None:
        mode = snap["mode"]
        protected = snap["protected"]
        busy = snap["busy"]

        if protected and mode == "WARP":
            colour, title = GREEN, "PROTECTED — WARP"
        elif protected and mode == "TOR":
            colour, title = AMBER, "PROTECTED — TOR (WARP cooling down)"
        elif protected and mode == "COOLDOWN":
            colour, title = AMBER, "PROTECTED — TOR (WARP cooling down)"
        else:
            colour, title = RED, "NOT PROTECTED"

        sub_parts: list[str] = []
        if busy:
            sub_parts.append(f"{busy}…")
        if mode == "WARP":
            sub_parts.append("Traffic and DNS (UDP) · proxy mode · auto-rotates on limit")
        elif mode in {"TOR", "COOLDOWN"}:
            remaining = snap["cooldown_remaining_s"]
            if remaining > 0:
                sub_parts.append(f"auto-switch back to WARP in {format_hms(remaining)}")
            else:
                sub_parts.append("Tor circuit rotation active")
        else:
            sub_parts.append(snap["last_error"] or "no working transport — bridge is refusing traffic")

        # A green banner on a leaking WARP listener is the one lie this app must
        # never tell, so the leak verdict outranks the transport state.
        leak = snap.get("leak") or {}
        if leak.get("ok") is False:
            colour = RED
            title = "NOT PROTECTED — LEAKING"
            sub_parts = [leak.get("reason") or "WARP is not carrying the traffic"]
        elif snap.get("resume_running"):
            sub_parts.append("clearing the limit and resuming chats…")

        self.banner.configure(bg=colour)
        self.banner_title.configure(bg=colour, text=title)
        self.banner_sub.configure(bg=colour, text=" · ".join(sub_parts))

        self.vals["connection"].configure(text=mode)
        self.vals["ip"].configure(text=snap["ip"] or "—")
        self.vals["newips"].configure(text=str(snap["new_ip_count"]))
        last = snap["last_rotation_ts"]
        self.vals["lastrot"].configure(
            text=time.strftime("%H:%M:%S", time.localtime(last)) if last else "never"
        )
        self.vals["proxy"].configure(text=snap["proxy_url"])
        self.vals["upstream"].configure(text=snap["upstream"] or "none")
        remaining = snap["cooldown_remaining_s"]
        self.vals["cooldown"].configure(text=format_hms(remaining) if remaining > 0 else "—")
        self.vals["probe"].configure(text=snap["last_probe"])

        if busy:
            self.btn_newip.state(["disabled"])
            self.btn_newip.configure(text=f"{busy}…")
        else:
            self.btn_newip.state(["!disabled"])
            eta = ""
            self.btn_newip.configure(text=f"Get a new IP{eta}")

        self._render_logs()
        self._render_connection(snap)
        self._render_history(snap)

        if self.tray is not None:
            tip = f"{config.APP_NAME} — {mode}"
            if snap["ip"]:
                tip += f" · {snap['ip']}"
            self.tray.set_tooltip(tip)

    def _render_logs(self) -> None:
        ring = logbus.get_ring()
        new = ring.since(self._last_log_ts)
        if not new:
            return
        self._last_log_ts = new[-1][0]
        lines = logbus.extend(new)
        self.log_text.configure(state="normal")
        for line in lines:
            self.log_text.insert("end", line + "\n")
        total = int(self.log_text.index("end-1c").split(".")[0])
        if total > 4000:
            self.log_text.delete("1.0", f"{total - 4000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _render_connection(self, snap: dict) -> None:
        info = snap["transport"]
        lines = [
            f"Mode            : {snap['mode']}",
            f"Protected       : {'yes' if snap['protected'] else 'NO'}",
            f"Current IP      : {snap['ip'] or 'unknown'}",
            f"Bridge          : {'listening' if snap['bridge_running'] else 'stopped'} on {snap['proxy_url']}",
            f"Upstream SOCKS5 : {snap['upstream'] or 'none'}",
            f"Transport       : {info.get('name')}",
        ]
        for key in ("upstream", "mode", "protocol", "control", "exe", "cli", "pid", "error"):
            if key in info:
                lines.append(f"  {key:<13}: {info[key]}")
        lines += [
            "",
            f"New IPs         : {snap['new_ip_count']}",
            f"Limit hits      : {snap['limit_hits']}",
            f"Transport swaps : {snap['transport_switches']}",
            f"Cooldown total  : {format_hms(snap['cooldown_total_s'])}",
            f"Cooldown left   : {format_hms(snap['cooldown_remaining_s'])}",
            f"Uptime          : {format_hms(snap['uptime_s'])}",
            "",
            f"Bridge requests : {snap['bridge']['requests']}",
            f"Bridge refusals : {snap['bridge']['refusals']}",
            f"Bytes up/down   : {snap['bridge']['bytes_up']} / {snap['bridge']['bytes_down']}",
            f"Live conns      : {snap['bridge']['active']}",
            "",
            f"Auto-rotate     : {'ON' if snap['auto_rotate'] else 'OFF'}",
            f"Background      : {'ON' if snap['background'] else 'OFF'}",
        ]
        leak = snap.get("leak") or {}
        if leak:
            if leak.get("ok") is True:
                leak_text = f"clear (warp={leak.get('warp')} colo={leak.get('colo')})"
            elif leak.get("ok") is False:
                leak_text = f"LEAK — {leak.get('reason')}"
            else:
                leak_text = f"inconclusive — {leak.get('reason') or 'not run yet'}"
            lines.append(f"Leak check      : {leak_text}")
        else:
            lines.append("Leak check      : not run yet")
        resume_state = snap.get("last_resume") or "not run yet"
        if snap.get("resume_running"):
            resume_state = "running…"
        lines += [
            f"Resume          : {resume_state}",
            f"Last error      : {snap['last_error'] or 'none'}",
        ]
        text = "\n".join(lines)
        self.conn_text.configure(state="normal")
        if self.conn_text.get("1.0", "end").strip() != text.strip():
            self.conn_text.delete("1.0", "end")
            self.conn_text.insert("1.0", text)
        self.conn_text.configure(state="disabled")

    def _render_history(self, snap: dict) -> None:
        rows = list(reversed(snap["ip_history"]))
        existing = self.history.get_children()
        if len(existing) == len(rows):
            newest = rows[0]["ts"] if rows else 0
            first_ts = float(self.history.item(existing[0], "values")[0].replace(":", "")) if existing else -1
            if existing and abs(first_ts - float(time.strftime("%H%M%S", time.localtime(newest)))) < 0.5:
                return
        for item in existing:
            self.history.delete(item)
        for entry in rows:
            self.history.insert(
                "", "end",
                values=(
                    time.strftime("%H:%M:%S", time.localtime(entry.get("ts", 0))),
                    entry.get("ip", "unknown"),
                    entry.get("transport", "?"),
                    entry.get("reason", ""),
                ),
            )

    # ------------------------------------------------------------------- close

    def _on_close(self) -> None:
        if bool(config.get(self.cfg, "ui.close_to_tray", True)) and self.tray is not None:
            self.root.withdraw()
            log.info("window hidden; background protection continues")
            return
        self._quit()

    def _quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        log.info("shutting down")
        try:
            self.launcher.reap(force=False)
        except Exception as exc:
            log.warning("child teardown: %s", exc)
        try:
            self.engine.stop()
        except Exception as exc:
            log.warning("engine teardown: %s", exc)
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    @property
    def closing(self) -> bool:
        """True once a real quit has been requested (not a hide-to-tray)."""
        return self._closing

    # -------------------------------------------------------------------- run

    def run(self) -> None:
        if bool(config.get(self.cfg, "ui.start_minimized", False)) and self.tray is not None:
            self.root.withdraw()
        self.root.mainloop()
