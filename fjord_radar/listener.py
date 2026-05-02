"""UDP syslog listener.

Reads RFC3164/5424-ish syslog datagrams, parses them, persists structured
events. Designed to run as a thread inside the orchestrator (`app.py`)
or stand-alone via `python -m fjord_radar.listener`.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
from types import FrameType
from typing import Any, Optional

from .parser import parse
from .storage import Storage

log = logging.getLogger(__name__)


class SyslogListener:
    def __init__(
        self, storage: Storage, host: str, port: int
    ) -> None:
        self._storage = storage
        self._host = host
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._packets_total = 0
        self._lines_total = 0
        self._events_total = 0
        self._last_packet_ts: Optional[float] = None
        self._last_packet_from: Optional[str] = None
        self._last_event_ts: Optional[float] = None
        self._started_at: Optional[float] = None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "packets_total": self._packets_total,
                "lines_total": self._lines_total,
                "events_total": self._events_total,
                "ignored_total": self._lines_total - self._events_total,
                "last_packet_ts": self._last_packet_ts,
                "last_packet_from": self._last_packet_from,
                "last_event_ts": self._last_event_ts,
                "started_at": self._started_at,
                "bind": f"{self._host}:{self._port}",
            }

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self._host, self._port))
        s.settimeout(1.0)  # so we can check the stop flag
        self._sock = s
        self._started_at = time.time()
        log.info("syslog listening on %s:%d/udp", self._host, self._port)
        self._thread = threading.Thread(
            target=self._serve, name="syslog-listener", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                log.exception("recvfrom failed")
                continue

            try:
                line = data.decode("utf-8", errors="replace")
            except Exception:
                log.exception("decode failed from %s", addr)
                continue

            with self._lock:
                self._packets_total += 1
                self._last_packet_ts = time.time()
                self._last_packet_from = f"{addr[0]}:{addr[1]}"

            for raw in line.splitlines():
                with self._lock:
                    self._lines_total += 1
                ev = parse(raw)
                if ev is None:
                    log.debug("ignored: %s", raw)
                    continue
                try:
                    self._storage.record(ev)
                except Exception:
                    log.exception("failed to record event: %s", raw)
                    continue
                with self._lock:
                    self._events_total += 1
                    self._last_event_ts = time.time()
                log.info(
                    "event kind=%s host=%s ch=%s freq=%s width=%s",
                    ev.kind, ev.host, ev.channel, ev.freq_mhz, ev.width_mhz,
                )


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> int:
    """Stand-alone entry point. Use `app.py` for the full orchestrator."""
    _setup_logging(os.environ.get("FJORD_LOG_LEVEL", "INFO"))
    host = os.environ.get("FJORD_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("FJORD_SYSLOG_PORT", "5514"))
    data_dir = os.environ.get("FJORD_DATA_DIR", "./data")

    storage = Storage(data_dir)
    listener = SyslogListener(storage, host, port)
    stop = threading.Event()

    def _stop(signum: int, _frame: Optional[FrameType]) -> None:
        log.info("signal %d received", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    listener.start()
    try:
        while not stop.wait(timeout=1.0):
            pass
    finally:
        listener.stop()
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
