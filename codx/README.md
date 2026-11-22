# codx

Keeps an AI client that meters by IP address online. When the per-IP free limit
appears, codx moves the client's egress onto a fresh address, clears the client's
own lockout, and tells the chat to carry on.

Only the process codx launches is routed. Nothing else on the machine changes
path — the browser, `warp-cli`, Windows Update and this window all keep using the
normal connection.

---

## The two halves of a working bypass

Rotating the address on its own looks successful and changes nothing you can
see. Two separate things have to happen:

**Half one — the network.** The server sees your IP. Move it, and the server's
meter resets. That is WARP, and Tor behind it.

**Half two — the client.** OpenCode Desktop keeps its own copy of the rate-limit
state in a local SQLite database, and the composer stays disabled until that row
is gone. The chat is also simply stopped — nothing is going to send the next
prompt. codx clears the local lockout and sends `continue` to the sessions that
were active, then reads the message back out of the session database to prove it
landed.

codx does both, in that order, on every rotation.

---

## How the traffic flows

```
OpenCode  ──HTTPS_PROXY──▶  codx bridge        ──SOCKS5──▶  WARP  127.0.0.1:40000
(launched by codx)         127.0.0.1:8899                    (Cloudflare egress)

                                          └──SOCKS5──▶  Tor   127.0.0.1:19050
                                                          (circuit rotation)

Everything else on the machine ──────────────────────────▶  normal connection
```

The bridge is the reason the client never restarts. The upstream behind it swaps
between WARP and Tor while the client keeps talking to the same `127.0.0.1:8899`.
If no upstream is live the bridge answers **502** and refuses the request. It
never falls through to a direct connection.

---

## Requirements

| Piece | What | Where |
|---|---|---|
| OS | Windows 11 | — |
| Python | 3.10 or newer, no third-party packages | `winget install -e --id Python.Python.3.12` |
| WARP | Cloudflare WARP client, official | `winget install -e --id Cloudflare.Warp` |
| Tor | optional fallback | `build.ps1 -InstallTor` |

Python is used for the standard library only: `tkinter`, `ctypes`, `sqlite3`,
`ssl`, `socket`, `subprocess`. There is nothing to `pip install`.

---

## Install

```powershell
cd C:\Users\%USERNAME%\Desktop\codx
powershell -ExecutionPolicy Bypass -File .\build.ps1 -InstallTor
```

That single command:

1. finds a Python 3.10+ interpreter,
2. byte-compiles every module — a real compile gate, not a promise,
3. runs the self-test (config shape, `warp-cli`, `tor.exe`, bridge port free),
4. downloads the Tor Expert Bundle into `%LOCALAPPDATA%\codx\tor`,
5. creates `codx.lnk` on the Desktop.

Drop `-InstallTor` if Tor is already installed and on the search path. Add
`-Autostart` to register a logon task that runs headless.

---

## Run

```powershell
# the window
cd C:\Users\%USERNAME%\Desktop\codx
py -3.14 -m codx

# headless daemon, survives the window closing
py -3.14 -m codx --headless

# check config, warp-cli, tor.exe and ports without starting anything
py -3.14 -m codx --selftest

# a different config file
py -3.14 -m codx --config D:\somewhere\config.json
```

Or double-click `codx.lnk`.

---

## What you see

| Element | Meaning |
|---|---|
| Green banner | A transport is up and the bridge is carrying traffic |
| Amber banner | Tor is carrying traffic while WARP cools down |
| Red banner | No transport — the bridge is refusing everything (fail-closed) |
| Red `NOT PROTECTED — LEAKING` | The proxy accepted the connection but the edge saw your own IP |
| Connection type | `WARP`, `TOR`, `COOLDOWN` |
| Current IP | The egress address as reported through the live proxy |
| New IPs so far | Rotations recorded since first run, persisted across restarts |
| Last rotation | Clock time of the most recent rotation |
| Cooldown | `HH:MM` remaining before WARP is retried |
| Leak check | What Cloudflare's edge said about the last trace request |
| Resume | Result of the last lockout-clear + chat-resume pass |

Buttons: **Launch app**, **Launch CLI**, **Get a new IP**. Toggles: **Background
protection**, **Auto-rotate on limit**.

Repair tab: restart transport, force WARP, force Tor, recheck health, run probe,
clear limit & resume, check for leak, re-enforce WARP mode, open config, open log.

---

## State machine

```
        ┌──────────┐   limit hit / manual   ┌──────────┐
        │   WARP   │ ─────────────────────▶ │   TOR    │
        └──────────┘   (pool exhausted)     └──────────┘
              ▲                                  │
              │        cooldown elapsed          │ arm cooldown
              └──────────────────────────────┐   │
                                             ▼   ▼
                                          ┌──────────┐
                                          │ COOLDOWN │
                                          └──────────┘

        neither transport usable ──▶ UNPROTECTED (bridge 502s)
```

- `connect_warp()` — enforce protocol → mode → port, connect, wait for the
  listener, sample the address.
- `connect_tor()` — start `tor.exe`, wait for bootstrap 100%, arm the cooldown.
- `get_new_ip()` — debounced rotation, transport-aware, falls back to Tor when
  the WARP pool is spent.
- `status()` / `snapshot()` — everything the UI draws, read under one lock.
- `health_check()` — liveness plus, for WARP, the leak verdict.

Rotations are debounced by `rotation.min_interval_s` (default 10 s) so a burst of
429s cannot cause flapping.

---

## WARP: the two constraints that matter

**Proxy mode requires MASQUE.** The service log is explicit:

```
Proxy mode only supports MASQUE
```

`warp-cli mode proxy` on WireGuard returns success and then does nothing at all.
codx sets `tunnel protocol set MASQUE` before `mode proxy`, every time, and
verifies the listener actually appears.

**The settings GUI fights proxy mode.** `Cloudflare WARP.exe` keeps its own copy
of the settings and pushes them back whenever it is opened or focused. That
silently flips `operation_mode` from `proxy` back to `warp` and kills the SOCKS
listener — the tunnel still reads `Connected`, so nothing looks wrong until a
request fails.

Two defences, both automatic:

1. `warp.keep_gui_closed` (default `true`) closes the settings window when proxy
   mode is enforced. Only that window — `warp-svc`, which actually carries
   traffic, is never touched, and the GUI can be reopened any time after codx
   exits.
2. The watchdog polls WARP every 8 s and, after 2 consecutive failures,
   re-asserts protocol → mode → port → connect before giving up. A drift repairs
   in roughly 10–25 s with the bridge still serving.

Rotating the address uses `tunnel endpoint reset` followed by `tunnel rotate-keys`
and a reconnect. `rotate-keys` alone frequently renegotiates against the same
peer and hands back the same address; the endpoint reset forces a fresh session.
If that fails repeatedly, re-registration is the next step, and Tor after that.

---

## Tor fallback

`tor.exe` runs on `127.0.0.1:19050` with its control port on `19051`. Those are
deliberately not the conventional 9050/9051 — an unrelated Tor instance on the
usual ports would otherwise be hijacked. `tor.auto_port` moves the pair again if
either is busy.

A new circuit is requested with `SIGNAL NEWNYM` on the control port, then the
address is re-read through the proxy to confirm it actually moved. Control auth
uses cookie auth when `tor.control_password` is empty, and `HashedControlPassword`
when it is set.

Tor is stopped with `taskkill /T /F` **on the PID codx started**, verified to
still be that process, so a separately installed Tor is never touched.

---

## Leak detection

A SOCKS listener that accepts connections and forwards them on the machine's own
address is indistinguishable from a working one at the socket level. The UI would
say protected while the target app is metered by your real IP.

codx asks Cloudflare directly:

```
GET https://opencode.ai/cdn-cgi/trace   (through the live proxy)
```

`warp=on` means the edge saw WARP. `warp=off` means the request left on your own
address, and that is reported as a leak, counted as a health failure, and
repaired. The check runs at most once per `leak.interval_s` (default 60 s) so it
does not add a request to every watchdog tick.

`opencode.ai` is first in the list because it is the exact host whose meter
matters. `cloudflare.com` and `1.1.1.1` are fallbacks.

---

## Limit clearing and chat resume

Paths and keys are config, not code.

**Lockout rows** — deleted from `%APPDATA%\ai.opencode.desktop\drafts.sqlite`,
table `state`:

```sql
DELETE FROM state
 WHERE key  IN ('free-limit','go-upsell')
    OR name IN ('free-limit','go-upsell')
    OR key  LIKE '%free%limit%'  OR name LIKE '%free%limit%'
    OR key  LIKE '%quota%exceeded%' OR name LIKE '%quota%exceeded%'
    OR key  LIKE '%upsell%'      OR name LIKE '%upsell%'
```

Both `key` and `name` are matched because the table carries two identifying
columns and older builds populate only one. The delete runs inside a transaction
with a busy timeout, so a running desktop app serialises against it instead of
failing.

**Chat resume** — reads `session_v2` from
`%USERPROFILE%\.local\share\opencode\opencode.db` for sessions touched within
`resume.recent_minutes`, skipping archived sessions and sub-agent children, then
runs one command per session through the live proxy:

```
opencode run --session <id> continue
```

**Proof** — after dispatch, codx polls `session_message` for a `type='user'` row
newer than the dispatch time. A session that was dispatched but never appeared is
reported as unconfirmed rather than claimed as done.

The whole pass runs on a worker thread so a rotation returns immediately.

---

## Config

`config.json` sits next to the package. Every port, path, threshold and string is
here — none of it is in the source.

### warp

| Key | Default | Meaning |
|---|---|---|
| `cli_path` | `C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe` | The CLI to drive |
| `mode` | `proxy` | `proxy` gives the SOCKS listener; requires MASQUE |
| `tunnel_protocol` | `MASQUE` | Validated against `mode` at load time |
| `enforce_mode` | `true` | Re-assert protocol/mode/port before every connect |
| `proxy_host` / `proxy_port` | `127.0.0.1` / `40000` | The WARP SOCKS listener |
| `connect_timeout_s` | `30` | Wait for `Connected` **and** the listener |
| `rotate_keys` | `true` | Use `tunnel rotate-keys` for a new address |
| `reset_endpoint` | `true` | `tunnel endpoint reset` before rotating |
| `allow_reregister` | `true` | Fall back to delete + new registration |
| `max_address_attempts` | `4` | Attempts before declaring the pool spent |
| `settle_s` | `12` | Wait after rotation before sampling the address |
| `keep_gui_closed` | `true` | Close the settings window so it cannot reset proxy mode |
| `gui_process` | `Cloudflare WARP.exe` | Image name to close (service untouched) |
| `restore_mode` | `warp` | Deterministic restore target on exit |
| `restore_tunnel_protocol` | `MASQUE` | Restore target protocol |
| `restore_connect` | `true` | Reconnect on exit |

### tor

| Key | Default | Meaning |
|---|---|---|
| `exe_path` | *(empty)* | Explicit path; empty means use `search_globs` |
| `search_globs` | Tor Browser + `%LOCALAPPDATA%\codx\tor` | Where to look for `tor.exe` |
| `socks_host` / `socks_port` | `127.0.0.1` / `19050` | SOCKS listener |
| `control_host` / `control_port` | `127.0.0.1` / `19051` | Control listener |
| `auto_port` | `true` | Pick another pair if these are busy |
| `control_password` | *(empty)* | Empty means cookie auth |
| `bootstrap_timeout_s` | `90` | Wait for 100% bootstrap |
| `newnym_wait_s` | `12` | Wait after NEWNYM before re-reading the address |
| `data_dir` | `%LOCALAPPDATA%\codx\tor-data` | Tor state directory |
| `extra_args` | `[]` | Appended to the `tor.exe` command line |

### bridge

| Key | Default | Meaning |
|---|---|---|
| `listen_host` / `listen_port` | `127.0.0.1` / `8899` | The address the client is pointed at |
| `connect_timeout_s` | `20` | Upstream connect timeout |
| `idle_timeout_s` | `180` | Drop an idle tunnel |
| `max_connections` | `256` | Concurrent tunnel ceiling |
| `max_header_bytes` | `65536` | Header cap; oversized requests are refused |

### rotation

| Key | Default | Meaning |
|---|---|---|
| `min_interval_s` | `10` | Debounce between rotations |
| `cooldown_minutes` | `125` | How long Tor carries traffic before retrying WARP |
| `auto_rotate` | `true` | Rotate automatically when the limit is detected |
| `background_protection` | `true` | Keep the daemon alive with the window closed |
| `warp_pool_attempts` | `4` | WARP address attempts before Tor |
| `exhaustion_repeats` | `3` | Consecutive exhausted pools before giving up |

### probe

| Key | Default | Meaning |
|---|---|---|
| `url` | `https://api.ipify.org?format=json` | Cheap endpoint, no tokens burned |
| `method` | `GET` | `HEAD` also works |
| `match_strings` | `rate limit`, `too many requests`, … | Body substrings meaning "limit hit" |
| `timeout_s` | `12` | Probe timeout |
| `interval_s` | `30` | Detector cadence |
| `max_body_bytes` | `65536` | Body read cap |

An HTTP `429` is a limit hit regardless of body.

### resume

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch |
| `auto_after_rotation` | `true` | Run the pass automatically after every rotation |
| `drafts_db` | `%APPDATA%\ai.opencode.desktop\drafts.sqlite` | Where the lockout rows live |
| `session_db` | `%USERPROFILE%\.local\share\opencode\opencode.db` | Where sessions live |
| `lock_keys` | `free-limit`, `go-upsell` | Exact row names to delete |
| `lock_like` | `%free%limit%`, `%quota%exceeded%`, `%upsell%` | Pattern matches |
| `recent_minutes` | `120` | How far back to look for sessions |
| `max_sessions` | `3` | How many to resume |
| `continue_text` | `continue` | What is sent |
| `cli_path` | *(empty)* | Explicit `opencode` path; empty means resolve from PATH |
| `cli_args` | `[]` | Extra arguments |
| `send_timeout_s` | `180` | Per-session dispatch timeout |
| `verify_timeout_s` | `90` | How long to wait for arrival |
| `verify_poll_s` | `3` | Arrival poll interval |
| `db_timeout_s` | `5` | SQLite busy timeout |

### leak

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Master switch |
| `interval_s` | `60` | Minimum gap between trace requests |
| `urls` | `opencode.ai`, `cloudflare.com`, `1.1.1.1` | Trace endpoints, tried in order |
| `timeout_s` | `8` | Per-endpoint timeout |
| `expect_warp` | `on`, `plus` | Values of `warp=` that mean protected |

### health, ip, target, ui, logging

| Key | Default | Meaning |
|---|---|---|
| `health.interval_s` | `20` | Watchdog cadence for Tor |
| `health.warp_interval_s` | `8` | Faster WARP poll — its listener is the fragile one |
| `health.failures_before_switch` | `2` | Failures before repair, then fallback |
| `ip.v4_url` / `ip.v6_url` | ipify | Address lookup |
| `ip.history_limit` | `200` | Rotations kept |
| `target.app_path` | *(empty)* | Desktop app to launch |
| `target.cli_path` | `opencode` | CLI to launch |
| `target.proxy_env_var` | `HTTPS_PROXY` | Variable injected at launch |
| `ui.close_to_tray` | `true` | Close hides rather than exits |
| `ui.refresh_ms` | `1000` | UI repaint |
| `logging.file` | `%LOCALAPPDATA%\codx\codx.log` | Rotating log |

---

## Safety

| Property | How it is guaranteed |
|---|---|
| Only the target app is routed | The proxy is injected into the launched process's environment; no system proxy, no route table, no firewall rule, no TAP adapter |
| No silent direct traffic | The bridge answers `502` when no upstream is live; it has no direct-connect path at all |
| No lying about protection | The `warp=` field from Cloudflare's edge outranks the transport state in the banner |
| WARP is restored on exit | Deterministic restore from config, not "whatever was found" — a crashed run cannot leave proxy mode behind forever |
| Only codx's Tor is stopped | `taskkill /T /F` on the PID codx started, re-verified before killing |
| Only the settings window is closed | `warp-svc` is never touched; the GUI can be reopened after exit |
| No account damage | The WARP registration is only deleted when `allow_reregister` is on and rotate-keys has already failed |
| Bounded everything | Every loop over outside data has a ceiling; every buffer has a size gate; every network call has a timeout |
| Ordered teardown | Stop loops → stop bridge → stop Tor → disconnect WARP → restore → save state |

codx does not touch your WARP account beyond the device registration it is
already using, your Tor circuits beyond its own process, or any file outside the
paths in `config.json`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `WARP refused proxy mode` | Protocol is WireGuard | codx sets MASQUE itself; a policy-locked client cannot be fixed locally |
| Port 40000 never appears | WARP GUI rewrote the mode | `keep_gui_closed` should prevent it; check the log for `WARP mode warp -> proxy` |
| `NOT PROTECTED — LEAKING` | Proxy accepted but the edge saw your IP | Repair → Check for leak, then Repair → Force Tor |
| `Tor bootstrap 0%` forever | Outbound blocked or a stale data dir | Repair → Restart transport; delete `%LOCALAPPDATA%\codx\tor-data` if it persists |
| `bridge bind` fails | Port 8899 taken | Change `bridge.listen_port` |
| Resume says "not confirmed" | The CLI is not on PATH or the session is busy | Set `resume.cli_path`; check the log for the `opencode run` error |
| `opencode CLI not found` | Not installed for this user | `resume.cli_path` → full path to `opencode.cmd` |
| Nothing happens on double-click | Python not on PATH | Run `build.ps1` again; it bakes the resolved interpreter into the shortcut |

The log is `%LOCALAPPDATA%\codx\codx.log` and the same lines appear in the Logs
tab.

---

## Files

```
codx\
  config.py       defaults, validation, path resolution
  logbus.py       logging + the ring buffer the UI reads
  socks.py        SOCKS5 client (CONNECT, no auth, user/pass)
  httpc.py        minimal HTTP/1.1 client + Cloudflare trace parser
  bridge.py       local HTTP/CONNECT proxy with a hot-swappable upstream
  transports.py   WarpTransport, TorTransport
  resume.py       lockout clearing, session discovery, continue, verification
  engine.py       state machine, rotation, cooldown, watchdog, detector
  launcher.py     launches the target app/CLI with the proxy environment
  tray.py         notification-area icon
  ui.py           tkinter window
  __main__.py     argparse, single-instance mutex, selftest
build.ps1         one-command build / install
run.ps1           launcher
config.json       generated on first run, then yours to edit
tests\smoke.py        live: Tor bootstrap, NEWNYM, debounce, fail-closed, WARP
tests\cooldown.py     live: WARP → COOLDOWN → automatic return to WARP
tests\resume_check.py live: lockout clear, session discovery, leak verdict
```

Run the tests with:

```powershell
py -3.14 tests\smoke.py
py -3.14 tests\cooldown.py
py -3.14 tests\resume_check.py
```

They drive the real transports, not mocks. `cooldown.py` uses a 9-second cooldown
so the return happens inside the test window.
