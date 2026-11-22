"""Minimal SOCKS5 client (RFC 1928, no auth).

Only what the bridge needs: CONNECT to a host:port through an upstream
SOCKS5 listener. Bounded reads, fail-closed on any deviation.
"""
from __future__ import annotations

import socket
import struct

SOCKS_VERSION = 5
CMD_CONNECT = 1
ATYP_IPV4 = 1
ATYP_DOMAIN = 3
ATYP_IPV6 = 4


class SocksError(RuntimeError):
    """Upstream SOCKS5 refused or misbehaved."""


_REPLY_TEXT = {
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise SocksError("upstream closed during SOCKS5 handshake")
        chunks.extend(chunk)
    return bytes(chunks)


def socks5_connect(
    proxy_host: str,
    proxy_port: int,
    dest_host: str,
    dest_port: int,
    timeout: float = 15.0,
) -> socket.socket:
    """Open a TCP stream to dest through the upstream SOCKS5 proxy.

    Returns a connected socket on success; raises SocksError or OSError.
    """
    if not (1 <= dest_port <= 65535):
        raise SocksError(f"bad destination port {dest_port}")

    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        sock.settimeout(timeout)

        # greeting: version, one method, no-auth
        sock.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(sock, 2)
        if ver != SOCKS_VERSION:
            raise SocksError(f"upstream is not SOCKS5 (version byte {ver})")
        if method != 0x00:
            raise SocksError("upstream requires authentication; this client supports none")

        # request
        host_bytes = dest_host.encode("idna", errors="strict")[:255]
        req = bytearray([SOCKS_VERSION, CMD_CONNECT, 0x00])
        try:
            packed = socket.inet_pton(socket.AF_INET, dest_host)
        except OSError:
            try:
                packed6 = socket.inet_pton(socket.AF_INET6, dest_host)
            except OSError:
                req.append(ATYP_DOMAIN)
                req.append(len(host_bytes))
                req.extend(host_bytes)
            else:
                req.append(ATYP_IPV6)
                req.extend(packed6)
        else:
            req.append(ATYP_IPV4)
            req.extend(packed)
        req.extend(struct.pack("!H", dest_port))
        sock.sendall(bytes(req))

        ver, rep, _rsv, atyp = _recv_exact(sock, 4)
        if ver != SOCKS_VERSION:
            raise SocksError(f"bad reply version {ver}")
        if rep != 0x00:
            raise SocksError(f"upstream refused: {_REPLY_TEXT.get(rep, f'code {rep:#x}')}")

        if atyp == ATYP_IPV4:
            _recv_exact(sock, 4 + 2)
        elif atyp == ATYP_IPV6:
            _recv_exact(sock, 16 + 2)
        elif atyp == ATYP_DOMAIN:
            (length,) = _recv_exact(sock, 1)
            _recv_exact(sock, length + 2)
        else:
            raise SocksError(f"unknown bound address type {atyp}")

        sock.settimeout(None)
        return sock
    except Exception:
        try:
            sock.close()
        except OSError:
            pass
        raise
