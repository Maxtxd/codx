"""Tiny HTTP/1.1 client that rides the upstream SOCKS5 proxy.

Used for the public-IP lookup and the rate-limit probe. No third-party
dependencies, bounded body reads, TLS verified against the system store.
"""
from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import socks

USER_AGENT = "codx/1.0 (+ip-rotation)"
MAX_HEADER_BYTES = 65536


class HttpError(RuntimeError):
    """Transport-level failure talking to the probe target."""


@dataclass
class Response:
    status: int
    reason: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    @property
    def text(self) -> str:
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        for part in ctype.split(";"):
            part = part.strip()
            if part.lower().startswith("charset="):
                charset = part.split("=", 1)[1].strip("\"' ")
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


def _read_until(sock: socket.socket, marker: bytes, cap: int) -> bytes:
    buf = bytearray()
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > cap:
            raise HttpError(f"response headers exceeded {cap} bytes")
    return bytes(buf)


def _parse_head(head: bytes) -> tuple[int, str, dict[str, str]]:
    text = head.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        raise HttpError("empty status line")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise HttpError(f"bad status line: {lines[0]!r}")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise HttpError(f"bad status code in {lines[0]!r}") from exc
    reason = parts[2] if len(parts) > 2 else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, reason, headers


def _read_body(sock: socket.socket, headers: dict[str, str], cap: int, prebuffered: bytes) -> bytes:
    body = bytearray(prebuffered)
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while len(body) < cap:
            chunk = sock.recv(min(8192, cap - len(body)))
            if not chunk:
                break
            body.extend(chunk)
            if b"\r\n0\r\n\r\n" in body:
                break
        return bytes(body[:cap])

    length_raw = headers.get("content-length")
    if length_raw and length_raw.isdigit():
        want = min(int(length_raw), cap)
        while len(body) < want:
            chunk = sock.recv(min(8192, want - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        return bytes(body[:want])

    while len(body) < cap:
        chunk = sock.recv(min(8192, cap - len(body)))
        if not chunk:
            break
        body.extend(chunk)
    return bytes(body[:cap])


def request(
    upstream: tuple[str, int] | None,
    url: str,
    method: str = "GET",
    timeout: float = 12.0,
    max_body: int = 65536,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    """Perform one request through `upstream` (a SOCKS5 host:port).

    upstream=None means "no proxy available" and raises HttpError; this client
    never falls back to a direct connection.
    """
    if upstream is None:
        raise HttpError("no transport available (fail-closed)")

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise HttpError(f"unsupported scheme {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise HttpError(f"no host in {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    try:
        raw = socks.socks5_connect(
            upstream[0], upstream[1], host, port, timeout=timeout
        )
    except (socks.SocksError, OSError) as exc:
        raise HttpError(f"upstream connect to {host}:{port} failed: {exc}") from exc

    sock: socket.socket = raw
    try:
        if parts.scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=host)

        headers = {
            "Host": host if port in (80, 443) else f"{host}:{port}",
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if extra_headers:
            headers.update(extra_headers)

        head = f"{method.upper()} {path} HTTP/1.1\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        head += "\r\n"
        sock.sendall(head.encode("latin-1"))

        raw_head = _read_until(sock, b"\r\n\r\n", MAX_HEADER_BYTES)
        if b"\r\n\r\n" not in raw_head:
            raise HttpError("connection closed before response headers")
        head_bytes, _, rest = raw_head.partition(b"\r\n\r\n")
        status, reason, resp_headers = _parse_head(head_bytes)
        body = _read_body(sock, resp_headers, max_body, rest)
        return Response(status=status, reason=reason, headers=resp_headers, body=body, url=url)
    except ssl.SSLError as exc:
        raise HttpError(f"TLS failure for {host}: {exc}") from exc
    except socket.timeout as exc:
        raise HttpError(f"timeout talking to {host}") from exc
    except OSError as exc:
        raise HttpError(f"socket error talking to {host}: {exc}") from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass


def fetch_ip(upstream: tuple[str, int] | None, url: str, timeout: float) -> str | None:
    """Return the public address as reported by `url`, or None."""
    import json
    import re

    resp = request(upstream, url, timeout=timeout, max_body=4096)
    if resp.status != 200:
        return None
    text = resp.text.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        for key in ("ip", "IPv4", "IPv6", "address"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val.strip()
        return None
    match = re.match(r"^[0-9a-fA-F:.]+$", text)
    return text if match else None


# Cloudflare's /cdn-cgi/trace answers with plain key=value lines describing what
# the edge actually saw. `warp=off` means the request left this machine on its
# own address, i.e. the proxy claimed to carry it and did not.
TRACE_KEYS = ("ip", "warp", "colo", "loc")


def trace(
    upstream: tuple[str, int] | None,
    url: str,
    timeout: float = 8.0,
) -> dict[str, str]:
    """Fetch a Cloudflare trace endpoint through `upstream` and parse it.

    Returns a dict with any of TRACE_KEYS that were present. An empty dict means
    the endpoint answered but carried no trace fields — treat that as unknown,
    not as a pass.
    """
    resp = request(upstream, url, timeout=timeout, max_body=8192)
    if resp.status != 200:
        raise HttpError(f"trace {url} -> HTTP {resp.status}")
    out: dict[str, str] = {}
    for line in resp.text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in TRACE_KEYS:
            out[key] = value.strip()
    return out

