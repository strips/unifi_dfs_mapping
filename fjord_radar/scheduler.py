"""Trial scheduler.

Drives the AP through a sequence of (channel, width) trials. For each
trial:

  1. Tell the controller to apply (channel, width) to the target radio.
  2. Open a `trials` row with `started_at = now()`.
  3. Poll SQLite every `poll_seconds` for new radar events. If radar
     fires while this trial is open, end the trial as `'radar'` and
     enter cooldown.
  4. If no radar fires within `dwell_hours`, end the trial as
     `'dwell_complete'` (the channel survived).
  5. Move to the next trial in the queue and repeat forever.

The scheduler is the single writer of `trials`; the listener is the
single writer of `events`/`sessions`. They synchronise only through
SQLite.

Concurrency model: one thread, started by the orchestrator. Stops on
the shared `threading.Event`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from .config import AppConfig
from .planner import Trial, build_trials, order
from .storage import Storage
from .unifi_client import UnifiClient, UnifiError

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Scheduler:
    def __init__(
        self,
        cfg: AppConfig,
        storage: Storage,
        client: UnifiClient,
        stop_event: threading.Event,
    ) -> None:
        self._cfg = cfg
        self._storage = storage
        self._client = client
        self._stop = stop_event
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[Trial] = None
        self._current_started: Optional[str] = None
        self._lock = threading.Lock()

    # -- public ------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="scheduler", daemon=True
        )
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def status(self) -> dict:
        with self._lock:
            return {
                "current_trial": (
                    {
                        "channel": self._current.channel,
                        "width_mhz": self._current.width_mhz,
                        "label": self._current.label(),
                        "started_at": self._current_started,
                    }
                    if self._current
                    else None
                ),
            }

    # -- internals ---------------------------------------------------------

    def _build_queue(self) -> deque[Trial]:
        scan = self._cfg.scan
        trials = build_trials(
            channels=scan.channels,
            widths=scan.widths,
            blacklist_channels=scan.blacklist_channels,
            blacklist_combos=scan.blacklist_combos,
        )
        if not trials:
            raise RuntimeError(
                "planner produced 0 trials — check scan.channels / widths "
                "/ blacklist in config.yaml"
            )
        ordered = order(trials, scan.strategy)
        log.info("plan has %d trials (%s)", len(ordered), scan.strategy)
        return deque(ordered)

    def _run(self) -> None:
        try:
            queue = self._build_queue()
        except Exception:
            log.exception("failed to build trial plan; scheduler exiting")
            return

        try:
            self._client.login()
        except UnifiError:
            log.exception(
                "could not authenticate to UniFi controller; scheduler exiting"
            )
            return

        ap_name = self._cfg.target.ap_name
        radio = self._cfg.target.radio

        while not self._stop.is_set():
            trial = queue[0]
            queue.rotate(-1)  # round-robin: move it to the back for next time
            self._run_trial(ap_name, radio, trial)

    def _run_trial(self, ap_name: str, radio: str, trial: Trial) -> None:
        scan = self._cfg.scan
        log.info("=== trial %s on AP %s/%s ===", trial.label(), ap_name, radio)

        # Apply channel via the controller.
        trial_id: Optional[int] = None
        try:
            device = self._client.find_ap(ap_name)
            self._client.set_radio(
                device, radio, trial.channel, trial.width_mhz
            )
        except UnifiError as e:
            log.error("could not apply %s: %s", trial.label(), e)
            tid = self._storage.start_trial(
                ap_name, trial.channel, trial.width_mhz,
                notes=f"apply failed: {e}",
            )
            self._storage.end_trial(tid, "apply_failed", radar_count=0)
            self._sleep(min(60.0, scan.poll_seconds * 2))
            return

        started_at = _now_iso()
        trial_id = self._storage.start_trial(
            ap_name, trial.channel, trial.width_mhz
        )
        with self._lock:
            self._current = trial
            self._current_started = started_at

        dwell_seconds = scan.dwell_hours * 3600.0
        deadline = time.monotonic() + dwell_seconds
        radar_count = 0
        ended_by = "dwell_complete"

        try:
            while not self._stop.is_set():
                # Check radar count strictly newer than trial start.
                radar_count = self._storage.count_radar_since(started_at)
                if radar_count > 0:
                    ended_by = "radar"
                    log.info(
                        "trial %s hit by radar (%d events)",
                        trial.label(), radar_count,
                    )
                    break
                if time.monotonic() >= deadline:
                    log.info("trial %s survived dwell", trial.label())
                    break
                self._sleep(scan.poll_seconds)
        finally:
            with self._lock:
                self._current = None
                self._current_started = None
            self._storage.end_trial(
                trial_id, ended_by, radar_count=radar_count
            )

        if ended_by == "radar":
            cooldown_s = scan.cooldown_after_radar_minutes * 60.0
            log.info("cooldown for %.0fs", cooldown_s)
            self._sleep(cooldown_s)

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep that returns early on stop."""
        end = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(timeout=min(remaining, 1.0))
