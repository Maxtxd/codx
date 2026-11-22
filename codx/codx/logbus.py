"""Logging: rotating file + bounded in-memory ring the UI reads."""
from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterable

from . import config


class RingBuffer(logging.Handler):
    """Keeps the last N formatted records for the UI panel."""

    def __init__(self, size: int = 2000) -> None:
        super().__init__()
        self._records: deque[tuple[float, str, str]] = deque(maxlen=max(50, size))
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # a broken formatter must never kill a worker
            return
        with self._lock:
            self._records.append((record.created, record.levelname, text))

    def snapshot(self, limit: int = 400) -> list[tuple[float, str, str]]:
        with self._lock:
            items = list(self._records)
        return items[-limit:]

    def since(self, ts: float) -> list[tuple[float, str, str]]:
        with self._lock:
            items = list(self._records)
        return [r for r in items if r[0] > ts]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_ring: RingBuffer | None = None
_configured = False


def setup(cfg: dict) -> RingBuffer:
    global _ring, _configured
    if _configured and _ring is not None:
        return _ring

    size = int(config.get(cfg, "logging.ring_size", 2000))
    _ring = RingBuffer(size)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%H:%M:%S",
    )
    _ring.setFormatter(fmt)

    root = logging.getLogger("codx")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(_ring)

    log_file = config.get(cfg, "logging.file")
    if log_file:
        path = Path(config.expand(log_file))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=int(config.get(cfg, "logging.max_bytes", 2 * 1024 * 1024)),
                backupCount=int(config.get(cfg, "logging.backup_count", 3)),
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            fh.setLevel(logging.DEBUG)
            root.addHandler(fh)
        except OSError as exc:
            root.warning("file log disabled (%s): %s", path, exc)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)
    root.addHandler(console)

    _configured = True
    return _ring


def get_ring() -> RingBuffer:
    if _ring is None:
        raise RuntimeError("logbus.setup() not called")
    return _ring


def logger(name: str) -> logging.Logger:
    return logging.getLogger(f"codx.{name}")


def tail(limit: int = 200) -> list[str]:
    if _ring is None:
        return []
    return [f"{time.strftime('%H:%M:%S', time.localtime(ts))} {lvl:<7} {msg.split(' ', 1)[-1]}"
            for ts, lvl, msg in _ring.snapshot(limit)]


def extend(records: Iterable[tuple[float, str, str]]) -> list[str]:
    return [f"{time.strftime('%H:%M:%S', time.localtime(ts))} {lvl:<7} {msg.split(' ', 1)[-1]}"
            for ts, lvl, msg in records]
