"""Configuration load / validate / save.

Every path, port and threshold the program uses lives in config.json.
Nothing operational is hard-coded in the source.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

APP_NAME = "codx"
APP_VERSION = "1.0.0"
APP_TAGLINE = "IP rotation manager"
APP_ID = "codx.IpRotation"

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = APP_ROOT / "config.json"

DEFAULTS: dict[str, Any] = {
    "version": 2,
    "warp": {
        "cli_path": r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
        # mode proxy -> warp-cli opens a SOCKS5 listener on 127.0.0.1:{proxy_port}.
        # WARP hard-requires MASQUE for proxy mode ("Proxy mode only supports
        # MASQUE"); WireGuard is rejected and the service retries every 5s.
        # MASQUE is QUIC/HTTP3 over UDP, so it is still a UDP transport.
        "mode": "proxy",
        "tunnel_protocol": "MASQUE",
        "enforce_mode": True,
        "proxy_host": "127.0.0.1",
        "proxy_port": 40000,
        "connect_timeout_s": 30,
        "rotate_keys": True,
        # Reset the cached Anycast endpoint before rotating. rotate-keys alone
        # frequently negotiates against the same peer and hands back the same
        # egress address; the endpoint reset forces a fresh session.
        "reset_endpoint": True,
        "allow_reregister": True,
        "max_address_attempts": 4,
        "settle_s": 12,
        # The Cloudflare WARP desktop GUI re-pushes its own copy of the settings
        # to the service, which silently resets operation_mode from `proxy` to
        # `warp` and kills the SOCKS listener. The service is left alone; only
        # the settings window is closed while codx is running.
        "keep_gui_closed": True,
        "gui_process": "Cloudflare WARP.exe",
        # What to put WARP back to when codx exits. Deterministic beats
        # "whatever we found", because a previous run may have left proxy mode
        # behind and we would then faithfully restore our own leftovers.
        # MASQUE is QUIC over UDP — the same "Traffic and DNS (UDP)" behaviour
        # the GUI offers — and it is the protocol this box actually completes a
        # handshake on. Set restore_tunnel_protocol to "WireGuard" if your
        # install prefers the classic UDP tunnel.
        "restore_mode": "warp",
        "restore_tunnel_protocol": "MASQUE",
        "restore_connect": True,
    },
    "tor": {
        "exe_path": "",
        "search_globs": [
            r"C:\Program Files\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
            r"C:\Program Files (x86)\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
            r"%LOCALAPPDATA%\Tor Browser\Browser\TorBrowser\Tor\tor.exe",
            r"%LOCALAPPDATA%\codx\tor\tor.exe",
            r"%LOCALAPPDATA%\codx\tor\Tor\tor.exe",
        ],
        "socks_host": "127.0.0.1",
        "socks_port": 19050,
        "control_host": "127.0.0.1",
        "control_port": 19051,
        "auto_port": True,
        "control_password": "",
        "bootstrap_timeout_s": 90,
        "newnym_wait_s": 12,
        "data_dir": r"%LOCALAPPDATA%\codx\tor-data",
        "extra_args": [],
    },
    "bridge": {
        "listen_host": "127.0.0.1",
        "listen_port": 8899,
        "connect_timeout_s": 20,
        "idle_timeout_s": 180,
        "max_connections": 256,
        "max_header_bytes": 65536,
    },
    "rotation": {
        "min_interval_s": 10,
        "cooldown_minutes": 125,
        "auto_rotate": True,
        "background_protection": True,
        "warp_pool_attempts": 4,
        "exhaustion_repeats": 3,
    },
    "probe": {
        "url": "https://api.ipify.org?format=json",
        "method": "GET",
        "match_strings": [
            "free usage limit",
            "rate limit",
            "rate_limit",
            "too many requests",
            "limit reached",
            "quota exceeded",
            "usage limit",
        ],
        "timeout_s": 12,
        "interval_s": 30,
        "max_body_bytes": 65536,
    },
    # Rotating the address does not clear OpenCode's own lockout or restart the
    # chat. This block covers the other half of the bypass: purge the client-side
    # limit rows, then nudge the recently active sessions to carry on.
    "resume": {
        "enabled": True,
        # Run the clear + resume pass automatically after every rotation. This is
        # the half of the bypass that makes the new address visible to the user.
        "auto_after_rotation": True,
        "drafts_db": [
            r"%APPDATA%\ai.opencode.desktop\drafts.sqlite",
            r"%USERPROFILE%\.config\ai.opencode.desktop\drafts.sqlite",
            r"%USERPROFILE%\.local\share\opencode\drafts.sqlite",
        ],
        "session_db": [
            r"%USERPROFILE%\.local\share\opencode\opencode.db",
            r"%LOCALAPPDATA%\opencode\opencode.db",
        ],
        "lock_keys": ["free-limit", "go-upsell"],
        "lock_like": ["%free%limit%", "%quota%exceeded%", "%upsell%"],
        "recent_minutes": 120,
        "max_sessions": 3,
        "continue_text": "continue",
        # The CLI on PATH is often a v1 build that looks for a `session` table and
        # cannot read the v2 store the desktop app writes, so the desktop app's own
        # bundled CLI is searched first. Globs are expanded in order.
        "cli_path": "",
        "cli_search": [
            r"%LOCALAPPDATA%\Programs\ai.opencode.desktop\resources\opencode-cli.exe",
            r"%LOCALAPPDATA%\Programs\*\resources\opencode-cli.exe",
            r"%LOCALAPPDATA%\Programs\*\opencode-cli.exe",
        ],
        # A resumed turn must not stall on a permission prompt: nobody is watching
        # a background resume, so `--auto` is passed to the CLI.
        "auto_approve": True,
        "cli_args": [],
        # A background nudge either lands quickly or it is the meter answering.
        # Waiting three minutes holds the resume worker for no benefit, and the
        # message row is written before the model call anyway.
        "send_timeout_s": 75,
        "verify_timeout_s": 90,
        "verify_poll_s": 3,
        "db_timeout_s": 5,
    },
    # A proxy that accepts the connection and then forwards the request on the
    # machine's own address looks perfectly healthy from the socket side. The
    # Cloudflare trace endpoint reports what the edge actually saw, so `warp=off`
    # is the only honest test that WARP is carrying the traffic.
    "leak": {
        "enabled": True,
        # The trace request costs one small round trip, so it is not on every
        # health tick — one minute is enough to catch a listener that lies.
        "interval_s": 60,
        "urls": [
            "https://opencode.ai/cdn-cgi/trace",
            "https://cloudflare.com/cdn-cgi/trace",
            "https://1.1.1.1/cdn-cgi/trace",
        ],
        "timeout_s": 8,
        "expect_warp": ["on", "plus"],
    },
    "ip": {
        "v4_url": "https://api.ipify.org?format=json",
        "v6_url": "https://api64.ipify.org?format=json",
        "timeout_s": 12,
        "history_limit": 200,
    },
    "health": {
        "interval_s": 20,
        # WARP is polled faster: its listener disappears when the vendor GUI
        # rewrites operation_mode, and a 20s blind window is long enough to lose
        # a whole request cycle.
        "warp_interval_s": 8,
        "failures_before_switch": 2,
    },
    "target": {
        "name": "OpenCode",
        "app_path": "",
        "app_args": [],
        "cli_path": "opencode",
        "cli_args": [],
        "proxy_env_var": "HTTPS_PROXY",
        "proxy_env_var_alt": "HTTP_PROXY",
    },
    "ui": {
        "start_minimized": False,
        "close_to_tray": True,
        "refresh_ms": 1000,
    },
    "logging": {
        "file": r"%LOCALAPPDATA%\codx\codx.log",
        "max_bytes": 2097152,
        "backup_count": 3,
        "ring_size": 2000,
    },
    "state_file": r"%LOCALAPPDATA%\codx\state.json",
}


class ConfigError(RuntimeError):
    """Raised when config.json cannot be read or is structurally wrong."""


def expand(value: str) -> str:
    """Expand %VARS% and ~ in a path-ish string."""
    if not isinstance(value, str) or not value:
        return value
    return os.path.expandvars(os.path.expanduser(value))


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


CONFIG_VERSION = 3

# The exact phrase the free tier returns. Older configs matched only the generic
# wording ("rate limit", "usage limit") and would miss the real one.
_REQUIRED_LIMIT_PHRASE = "free usage limit"

# (old default, new default) for keys whose shipped default changed. A value
# that still equals the old default was never tuned, so it is safe to move; a
# value someone chose is left exactly as it is.
_DEFAULT_MOVES: tuple[tuple[str, Any, Any], ...] = (
    ("resume.send_timeout_s", 180, 75),
)


def _set_dotted(cfg: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            return
        node = nxt
    node[parts[-1]] = value


def _migrate(cfg: dict) -> bool:
    """Bring an older config.json forward. Returns True when it changed.

    Only semantics are rewritten here. New keys need no handling: the deep merge
    above already fills anything the file does not carry.
    """
    changed = False
    version = int(cfg.get("version", 1) or 1)

    if version < 2:
        probe = cfg.get("probe")
        if isinstance(probe, dict):
            patterns = probe.get("match_strings")
            if isinstance(patterns, list) and _REQUIRED_LIMIT_PHRASE not in patterns:
                patterns.insert(0, _REQUIRED_LIMIT_PHRASE)
                changed = True
        changed = True

    if version < 3:
        for dotted, old, new in _DEFAULT_MOVES:
            if config_get(cfg, dotted) == old:
                _set_dotted(cfg, dotted, new)
                changed = True

    if version < CONFIG_VERSION:
        cfg["version"] = CONFIG_VERSION
        changed = True

    return changed


def config_get(cfg: dict, dotted: str, default: Any = None) -> Any:
    """`get` is defined further down; migration needs it before then."""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def load(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        cfg = copy.deepcopy(DEFAULTS)
        save(cfg, cfg_path)
        return cfg
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{cfg_path}: invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    except OSError as exc:
        raise ConfigError(f"{cfg_path}: cannot read: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: root must be an object")
    cfg = _deep_merge(DEFAULTS, raw)
    if _migrate(cfg):
        save(cfg, cfg_path)
    validate(cfg)
    return cfg


def save(cfg: dict, path: str | Path | None = None) -> Path:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, cfg_path)
    return cfg_path


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _require_port(cfg: dict, dotted: str) -> int:
    val = get(cfg, dotted)
    if not isinstance(val, int) or not (1 <= val <= 65535):
        raise ConfigError(f"{dotted} must be an integer 1-65535, got {val!r}")
    return val


def validate(cfg: dict) -> None:
    """Structural checks only. Absent executables are reported at runtime."""
    _require_port(cfg, "warp.proxy_port")
    _require_port(cfg, "tor.socks_port")
    _require_port(cfg, "tor.control_port")
    _require_port(cfg, "bridge.listen_port")
    if get(cfg, "tor.socks_port") == get(cfg, "warp.proxy_port"):
        raise ConfigError("tor.socks_port and warp.proxy_port must differ")
    if get(cfg, "rotation.min_interval_s") < 0:
        raise ConfigError("rotation.min_interval_s must be >= 0")
    if get(cfg, "rotation.cooldown_minutes") < 0:
        raise ConfigError("rotation.cooldown_minutes must be >= 0")
    if not get(cfg, "probe.url"):
        raise ConfigError("probe.url is required")
    if not isinstance(get(cfg, "probe.match_strings"), list):
        raise ConfigError("probe.match_strings must be a list")
    if not isinstance(get(cfg, "resume.session_db"), list):
        raise ConfigError("resume.session_db must be a list of paths")
    if not isinstance(get(cfg, "resume.drafts_db"), list):
        raise ConfigError("resume.drafts_db must be a list of paths")
    if not isinstance(get(cfg, "resume.cli_search"), list):
        raise ConfigError("resume.cli_search must be a list of paths")
    if not isinstance(get(cfg, "resume.cli_args"), list):
        raise ConfigError("resume.cli_args must be a list")
    if int(get(cfg, "resume.max_sessions", 0)) < 0:
        raise ConfigError("resume.max_sessions must be >= 0")
    if not str(get(cfg, "resume.continue_text", "") or "").strip():
        raise ConfigError("resume.continue_text must not be empty")
    if not isinstance(get(cfg, "leak.urls"), list) or not get(cfg, "leak.urls"):
        raise ConfigError("leak.urls must be a non-empty list")
    if float(get(cfg, "leak.interval_s", 60)) < 0:
        raise ConfigError("leak.interval_s must be >= 0")
    mode = get(cfg, "warp.mode")
    if mode not in {"proxy", "warp", "warp+doh", "tunnel_only"}:
        raise ConfigError(f"warp.mode unsupported: {mode!r}")
    proto = get(cfg, "warp.tunnel_protocol")
    if proto and proto.lower() not in {"wireguard", "masque"}:
        raise ConfigError(f"warp.tunnel_protocol must be WireGuard or MASQUE, got {proto!r}")
    # The service refuses to start proxy mode on WireGuard; catch it here with a
    # message that names the fix instead of letting it retry forever in the log.
    if mode == "proxy" and proto and proto.lower() != "masque":
        raise ConfigError(
            "warp.mode 'proxy' requires warp.tunnel_protocol 'MASQUE' "
            f"(got {proto!r}) — Cloudflare WARP: 'Proxy mode only supports MASQUE'"
        )


def ensure_dirs(cfg: dict) -> None:
    """Create every directory the program writes into."""
    for dotted in ("tor.data_dir",):
        raw = get(cfg, dotted)
        if raw:
            Path(expand(raw)).mkdir(parents=True, exist_ok=True)
    log_file = get(cfg, "logging.file")
    if log_file:
        Path(expand(log_file)).parent.mkdir(parents=True, exist_ok=True)
    state = get(cfg, "state_file")
    if state:
        Path(expand(state)).parent.mkdir(parents=True, exist_ok=True)


def find_tor_exe(cfg: dict) -> str | None:
    """Resolve tor.exe from explicit config, then the search globs."""
    explicit = expand(get(cfg, "tor.exe_path", "") or "")
    if explicit and Path(explicit).is_file():
        return explicit
    for pattern in get(cfg, "tor.search_globs", []):
        cand = Path(expand(pattern))
        if cand.is_file():
            return str(cand)
    return None


def find_warp_cli(cfg: dict) -> str | None:
    explicit = expand(get(cfg, "warp.cli_path", "") or "")
    if explicit and Path(explicit).is_file():
        return explicit
    for pattern in (
        r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
        r"C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe",
    ):
        cand = Path(pattern)
        if cand.is_file():
            return str(cand)
    return None


def python_exe() -> str:
    return sys.executable
