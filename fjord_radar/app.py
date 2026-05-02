"""Orchestrator entrypoint.

Runs in a single process:

  * SyslogListener  — receives & parses UDP syslog from the UDR
  * Scheduler       — applies trials via the UniFi controller
  * WebServer       — built-in stats page

All three share one `Storage`. Shutdown is coordinated through a
`threading.Event`. Stop with SIGINT / SIGTERM.

Usage:
    python -m fjord_radar.app
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from types import FrameType
from typing import Optional

from .config import AppConfig, load
from .listener import SyslogListener
from .scheduler import Scheduler
from .storage import Storage
from .unifi_client import UnifiClient
from .web import WebServer

log = logging.getLogger("fjord_radar")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # urllib3's "InsecureRequestWarning" is intentionally suppressed in
    # the client; quiet its other noise too.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def run(cfg: AppConfig) -> int:
    storage = Storage(cfg.data_dir)
    log.info("storage at %s", storage.db_path)

    listener = SyslogListener(
        storage, cfg.syslog.bind_host, cfg.syslog.bind_port
    )
    listener.start()

    stop = threading.Event()
    scheduler: Optional[Scheduler] = None
    if cfg.scan.enabled:
        client = UnifiClient(
            url=cfg.controller.url,
            username=cfg.controller.username,
            password=cfg.controller.password,
            site=cfg.controller.site,
            verify_tls=cfg.controller.verify_tls,
        )
        scheduler = Scheduler(cfg, storage, client, stop)
        scheduler.start()
        log.info("scan enabled — controller=%s ap=%s",
                 cfg.controller.url, cfg.target.ap_name)
    else:
        log.info("scan disabled — listener-only mode")

    web: Optional[WebServer] = None
    if cfg.web.enabled:
        web = WebServer(
            storage, scheduler, cfg.web.bind_host, cfg.web.bind_port,
            listener_stats=listener.stats,
        )
        web.start()

    def _stop(signum: int, _frame: Optional[FrameType]) -> None:
        log.info("signal %d received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    try:
        while not stop.wait(timeout=1.0):
            pass
    finally:
        log.info("shutdown sequence")
        if web is not None:
            web.stop()
        if scheduler is not None:
            scheduler.join(timeout=10)
        listener.stop()
        storage.close()
    return 0


def main() -> int:
    cfg = load()
    _setup_logging(cfg.log_level)
    log.info("config loaded; controller=%s", cfg.controller.redacted())
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
