"""Prove the bridge drops live tunnels when the egress changes.

The failure this guards against: OpenCode holds its API connection open for
minutes. WARP rotates on the same SOCKS port, so the upstream tuple does not
change and a naive change-detector never fires. The app's existing tunnel then
carries the resume out on the spent address and the free-tier limit returns
immediately.

The test opens a real CONNECT tunnel through the bridge to a live host, confirms
bytes flow, calls drop_tunnels() the way a rotation does, and confirms the tunnel
is dead while a fresh request still succeeds.
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from codx import config, logbus  # noqa: E402
from codx.bridge import HttpBridge  # noqa: E402

PROBE_HOST = "www.cloudflare.com"
PROBE_PORT = 443


def wait_listening(host: str, port: int, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def open_tunnel(host: str, port: int) -> socket.socket | None:
    """CONNECT through the bridge and return the client-side socket."""
    try:
        sock = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        print(f"  connect failed: {exc}")
        return None
    request = (
        f"CONNECT {PROBE_HOST}:{PROBE_PORT} HTTP/1.1\r\n"
        f"Host: {PROBE_HOST}:{PROBE_PORT}\r\n\r\n"
    )
    sock.sendall(request.encode("latin-1"))
    sock.settimeout(15)
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            print(f"  no response head: {head!r}")
            sock.close()
            return None
        head += chunk
        if len(head) > 65536:
            sock.close()
            return None
    status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    print(f"  tunnel head: {status}")
    if " 200 " not in status:
        sock.close()
        return None
    return sock


def tunnel_is_dead(sock: socket.socket, timeout_s: float = 5.0) -> bool:
    """A dropped tunnel reads EOF or errors; a live one answers or times out."""
    sock.settimeout(timeout_s)
    try:
        sock.sendall(b"HEAD / HTTP/1.1\r\nHost: " + PROBE_HOST.encode() + b"\r\n\r\n")
    except OSError:
        return True
    try:
        chunk = sock.recv(4096)
    except (ConnectionResetError, ConnectionAbortedError, OSError):
        return True
    except socket.timeout:
        # No answer and no error: the tunnel is still open, just quiet.
        return False
    return chunk == b""


def port_is_held(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def main() -> int:
    cfg = config.load(ROOT / "config.json")
    logbus.setup(cfg)
    failures = 0

    upstream_host = str(config.get(cfg, "warp.proxy_host", "127.0.0.1"))
    upstream_port = int(config.get(cfg, "warp.proxy_port", 40000))
    listen_host = str(config.get(cfg, "bridge.listen_host", "127.0.0.1"))
    listen_port = int(config.get(cfg, "bridge.listen_port", 8899))

    print(f"upstream : {upstream_host}:{upstream_port}")
    print(f"bridge   : {listen_host}:{listen_port}")
    print()

    # A stale codx (or an orphaned earlier run) would answer on the port and the
    # test would then be measuring that process instead of this one.
    if port_is_held(listen_host, listen_port):
        print(f"FAIL: {listen_host}:{listen_port} is already listening — stop the other codx first")
        return 1

    bridge = HttpBridge(cfg)
    try:
        bridge.set_upstream((upstream_host, upstream_port))
        bridge.start()
        if not wait_listening(listen_host, listen_port):
            print("FAIL: bridge never started listening")
            return 1

        print("--- open a tunnel through the bridge ---")
        tunnel = open_tunnel(listen_host, listen_port)
        if tunnel is None:
            print("FAIL: could not open a CONNECT tunnel (is WARP up?)")
            return 1

        print()
        print("--- drop_tunnels() as a rotation does ---")
        dropped = bridge.drop_tunnels("test rotation")
        print(f"  dropped: {dropped}")
        print(f"  counter: {bridge.tunnels_dropped}")
        if dropped < 1:
            print("FAIL: the bridge did not know about the live tunnel")
            failures += 1
        else:
            print("  PASS: the tunnel was registered and closed")

        print()
        print("--- the tunnel must now be dead ---")
        dead = tunnel_is_dead(tunnel)
        print(f"  dead: {dead}")
        if not dead:
            print("FAIL: the tunnel survived the drop")
            failures += 1
        else:
            print("  PASS: the old tunnel can no longer carry traffic")
        try:
            tunnel.close()
        except OSError:
            pass

        print()
        print("--- a fresh tunnel must still work ---")
        fresh = open_tunnel(listen_host, listen_port)
        if fresh is None:
            print("FAIL: the bridge stopped serving after the drop")
            failures += 1
        else:
            print("  PASS: the bridge reconnects through the current upstream")
            try:
                fresh.close()
            except OSError:
                pass

        # The worker needs a moment to notice the closed client and untrack it.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with bridge._tunnels_lock:
                if not bridge._tunnels:
                    break
            time.sleep(0.1)

        print()
        print("--- registry must be empty ---")
        with bridge._tunnels_lock:
            remaining = len(bridge._tunnels)
        print(f"  tracked: {remaining}")
        if remaining:
            print("FAIL: tunnels leaked in the registry")
            failures += 1
        else:
            print("  PASS: registry drained")

        print()
        print("--- a second bind must be refused ---")
        try:
            bridge.start()
            print("  PASS: start() is idempotent on the same instance")
        except Exception as exc:  # noqa: BLE001 - diagnostic
            print(f"  note: start() raised {exc}")
        import socket as _socket

        rival = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            rival.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                rival.bind((listen_host, listen_port))
                print("FAIL: a second process could bind the bridge port")
                failures += 1
            except OSError:
                print("  PASS: the port is exclusive; a stale codx cannot share it")
        finally:
            rival.close()
    finally:
        bridge.stop()
        time.sleep(0.5)

    print()
    print(f"RESULT: {'PASS' if failures == 0 else f'{failures} failure(s)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
