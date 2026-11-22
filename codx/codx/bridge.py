"""Local HTTP proxy ("bridge") that chains to the active upstream SOCKS5.

The target app is pointed at 127.0.0.1:<bridge.listen_port>. Only traffic that
opts in through that proxy is redirected; the rest of the machine is untouched.

Fail-closed: with no upstream, every request is answered 502 and nothing is
sent direct.
"""
from __future__ import annotations

import errno
import os
import select
import socket
import threading
import time
from typing import Callable

from . import config, logbus, socks

log = logbus.logger("bridge")

CRLF = b"\r\n"
HOP_BY_HOP = {
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "connection",
    "keep-alive",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class BridgeError(RuntimeError):
    pass


class HttpBridge:
    """Threaded HTTP/CONNECT proxy with a hot-swappable SOCKS5 upstream."""

    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        self.host: str = config.get(cfg, "bridge.listen_host", "127.0.0.1")
        self.port: int = int(config.get(cfg, "bridge.listen_port", 8899))
        self.connect_timeout = float(config.get(cfg, "bridge.connect_timeout_s", 20))
        self.idle_timeout = float(config.get(cfg, "bridge.idle_timeout_s", 180))
        self.max_conns = int(config.get(cfg, "bridge.max_connections", 256))
        self.max_header = int(config.get(cfg, "bridge.max_header_bytes", 65536))

        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._upstream_lock = threading.Lock()
        self._upstream: tuple[str, int] | None = None
        self._active = 0
        self._active_lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._workers_lock = threading.Lock()

        # Live upstream sockets. A CONNECT tunnel lives as long as its client
        # wants it, so it survives an upstream swap and keeps riding the old exit
        # unless it is closed on purpose.
        self._tunnels: set[socket.socket] = set()
        self._tunnels_lock = threading.Lock()
        self.tunnels_dropped = 0

        self.requests_served = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.refusals = 0
        self._stat_lock = threading.Lock()

    # ---------------------------------------------------------------- upstream

    def set_upstream(self, upstream: tuple[str, int] | None) -> None:
        with self._upstream_lock:
            changed = self._upstream != upstream
            self._upstream = upstream
        log.info("bridge upstream -> %s", "none (fail-closed)" if upstream is None else f"{upstream[0]}:{upstream[1]}")
        if changed:
            self.drop_tunnels("upstream changed")

    def get_upstream(self) -> tuple[str, int] | None:
        with self._upstream_lock:
            return self._upstream

    def _track(self, sock: socket.socket) -> None:
        with self._tunnels_lock:
            self._tunnels.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._tunnels_lock:
            self._tunnels.discard(sock)

    def drop_tunnels(self, reason: str = "") -> int:
        """Close every live tunnel so nothing keeps riding the old exit.

        OpenCode holds its API connection open for minutes. Rotating the address
        on the same WARP port leaves the upstream tuple unchanged, so the app's
        existing tunnel would carry the resume out on the spent address and the
        limit would come straight back. Closing the tunnels forces a reconnect
        through whatever is upstream now.

        Returns how many were closed.
        """
        with self._tunnels_lock:
            live = list(self._tunnels)
            self._tunnels.clear()
        for sock in live:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if live:
            with self._stat_lock:
                self.tunnels_dropped += len(live)
            log.info(
                "bridge dropped %d live tunnel(s)%s",
                len(live),
                f" — {reason}" if reason else "",
            )
        return len(live)

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._listener is not None:
            return
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # On Windows SO_REUSEADDR lets a second process bind a port that is
        # already listening, so a stale codx would silently take half the
        # traffic. SO_EXCLUSIVEADDRUSE makes the second bind fail instead, which
        # is the behaviour a listener wants.
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
        except OSError as exc:
            listener.close()
            if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)):
                raise BridgeError(
                    f"port {self.port} on {self.host} is already in use — "
                    f"change bridge.listen_port in config.json"
                ) from exc
            raise BridgeError(f"cannot bind {self.host}:{self.port}: {exc}") from exc
        listener.listen(128)
        listener.settimeout(0.5)
        self._listener = listener
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, name="bridge-accept", daemon=True)
        self._thread.start()
        log.info("bridge listening on http://%s:%d", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout=1.0)
        with self._workers_lock:
            self._workers.clear()
        log.info("bridge stopped")

    @property
    def running(self) -> bool:
        return self._listener is not None

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stats(self) -> dict:
        with self._stat_lock:
            return {
                "requests": self.requests_served,
                "bytes_up": self.bytes_up,
                "bytes_down": self.bytes_down,
                "refusals": self.refusals,
                "active": self._active,
            }

    # ------------------------------------------------------------------ accept

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                client, addr = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._active_lock:
                if self._active >= self.max_conns:
                    over = True
                else:
                    self._active += 1
                    over = False
            if over:
                log.warning("connection cap %d reached, refusing %s", self.max_conns, addr)
                self._reject(client, 503, "too many concurrent connections")
                continue
            worker = threading.Thread(target=self._handle_client, args=(client, addr), daemon=True)
            with self._workers_lock:
                self._workers.add(worker)
            worker.start()

    def _finish(self) -> None:
        with self._active_lock:
            if self._active > 0:
                self._active -= 1
        current = threading.current_thread()
        with self._workers_lock:
            self._workers.discard(current)

    # ----------------------------------------------------------------- helpers

    def _reject(self, client: socket.socket, code: int, message: str) -> None:
        body = message.encode("utf-8", errors="replace")
        head = (
            f"HTTP/1.1 {code} {message}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            client.sendall(head + body)
        except OSError:
            pass
        finally:
            self._close(client)
        with self._stat_lock:
            self.refusals += 1

    @staticmethod
    def _close(sock: socket.socket | None) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _read_head(self, client: socket.socket) -> bytes | None:
        buf = bytearray()
        client.settimeout(self.connect_timeout)
        while b"\r\n\r\n" not in buf:
            try:
                chunk = client.recv(4096)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            buf.extend(chunk)
            if len(buf) > self.max_header:
                return None
        return bytes(buf)

    # ------------------------------------------------------------- client path

    def _handle_client(self, client: socket.socket, addr) -> None:
        upstream_sock: socket.socket | None = None
        try:
            head = self._read_head(client)
            if head is None:
                return
            head_bytes, _, leftover = head.partition(b"\r\n\r\n")
            lines = head_bytes.decode("iso-8859-1", errors="replace").split("\r\n")
            if not lines:
                return
            parts = lines[0].split(" ")
            if len(parts) < 3:
                self._reject(client, 400, "malformed request line")
                return
            method, target, version = parts[0].upper(), parts[1], parts[2]

            headers: list[tuple[str, str]] = []
            for line in lines[1:]:
                if not line or ":" not in line:
                    continue
                name, _, value = line.partition(":")
                headers.append((name.strip(), value.strip()))

            upstream = self.get_upstream()
            if upstream is None:
                self._reject(client, 502, "codx: no transport available — refusing to send traffic direct")
                return

            if method == "CONNECT":
                self._handle_connect(client, target, upstream, leftover)
                return

            upstream_sock = self._handle_plain(
                client, method, target, version, headers, upstream, leftover
            )
            with self._stat_lock:
                self.requests_served += 1
        except Exception as exc:  # a single bad client must not kill the loop
            log.debug("client %s failed: %s", addr, exc)
            try:
                self._reject(client, 502, f"codx: {exc}")
            except Exception:
                pass
        finally:
            if upstream_sock is not None:
                self._untrack(upstream_sock)
            self._close(upstream_sock)
            self._close(client)
            self._finish()

    def _handle_connect(
        self,
        client: socket.socket,
        target: str,
        upstream: tuple[str, int],
        leftover: bytes,
    ) -> None:
        host, _, port_raw = target.rpartition(":")
        if not host:
            host, port_raw = target, "443"
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        try:
            port = int(port_raw)
        except ValueError:
            self._reject(client, 400, "bad CONNECT port")
            return

        try:
            remote = socks.socks5_connect(
                upstream[0], upstream[1], host, port, timeout=self.connect_timeout
            )
        except (socks.SocksError, OSError) as exc:
            log.warning("CONNECT %s:%d failed: %s", host, port, exc)
            self._reject(client, 502, f"codx: upstream refused {host}:{port}: {exc}")
            return

        try:
            # Register before the client is told the tunnel is up. A rotation
            # that lands between connect and register would otherwise leave this
            # socket riding the old exit with nobody holding a reference to it.
            self._track(remote)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: codx\r\n\r\n")
            if leftover:
                remote.sendall(leftover)
            up, down = self._relay(client, remote)
            with self._stat_lock:
                self.requests_served += 1
                self.bytes_up += up
                self.bytes_down += down
        finally:
            self._untrack(remote)
            self._close(remote)

    def _handle_plain(
        self,
        client: socket.socket,
        method: str,
        target: str,
        version: str,
        headers: list[tuple[str, str]],
        upstream: tuple[str, int],
        leftover: bytes,
    ) -> socket.socket:
        from urllib.parse import urlsplit

        if not target.lower().startswith(("http://", "https://")):
            self._reject(client, 400, "absolute-form URL required for plain HTTP proxying")
            raise BridgeError("non-absolute target")

        parts = urlsplit(target)
        host = parts.hostname
        if not host:
            self._reject(client, 400, "no host in request target")
            raise BridgeError("no host")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        remote = socks.socks5_connect(
            upstream[0], upstream[1], host, port, timeout=self.connect_timeout
        )
        try:
            if parts.scheme == "https":
                import ssl

                ctx = ssl.create_default_context()
                remote = ctx.wrap_socket(remote, server_hostname=host)

            out = [f"{method} {path} HTTP/1.1"]
            for name, value in headers:
                if name.lower() in HOP_BY_HOP:
                    continue
                out.append(f"{name}: {value}")
            out.append("Connection: close")
            remote.sendall(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
            if leftover:
                remote.sendall(leftover)

            self._track(remote)
            up, down = self._relay(client, remote)
            with self._stat_lock:
                self.bytes_up += up
                self.bytes_down += down
            return remote
        except Exception:
            self._close(remote)
            raise

    def _relay(self, client: socket.socket, remote: socket.socket) -> tuple[int, int]:
        """Pump bytes both ways until either side closes or goes idle."""
        client.settimeout(self.idle_timeout)
        remote.settimeout(self.idle_timeout)
        up = down = 0
        peers = {client: remote, remote: client}
        try:
            while not self._stop.is_set():
                readable, _, errored = select.select(list(peers), [], list(peers), 1.0)
                if errored:
                    break
                if not readable:
                    continue
                for src in readable:
                    dst = peers[src]
                    try:
                        chunk = src.recv(32768)
                    except (socket.timeout, OSError):
                        return up, down
                    if not chunk:
                        return up, down
                    try:
                        dst.sendall(chunk)
                    except OSError:
                        return up, down
                    if src is client:
                        up += len(chunk)
                    else:
                        down += len(chunk)
        finally:
            pass
        return up, down
