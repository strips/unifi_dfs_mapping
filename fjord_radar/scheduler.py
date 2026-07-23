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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .config import AppConfig
from .planner import Trial, build_trials, order
from .storage import Storage
from .unifi_client import UnifiClient, UnifiError

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _seconds_until_hour(target_hour: int) -> float:
    """Seconds from now until the next local clock hour == target_hour."""
    now = datetime.now()
    candidate = now.replace(
        hour=target_hour, minute=0, second=0, microsecond=0
    )
    if now >= candidate:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


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

    def status(self) -> dict[str, Any]:
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
        q: deque[Trial] = deque(ordered)

        # Resume round-robin after a restart.
        # - Completed trial (dwell_complete / radar): advance past it.
        # - Interrupted trial (SIGTERM mid-dwell): put it back at the front
        #   so the dwell accumulates rather than being wasted.
        last = self._storage.last_trial()
        if last:
            target = Trial(last["channel"], last["width_mhz"])
            interrupted = last.get("ended_by") in (None, "interrupted")
            for _ in range(len(q)):
                if q[0] == target:
                    if interrupted:
                        log.info(
                            "queue resumed at interrupted trial: %s",
                            target.label(),
                        )
                        # target is already at the front — do not rotate.
                    else:
                        q.rotate(-1)   # step past it to the next unseen trial
                        log.info(
                            "queue resumed after completed trial: %s",
                            target.label(),
                        )
                    break
                q.rotate(-1)
            # If target wasn't found (removed from plan), the full rotation
            # brought the queue back to the beginning — that's fine.

        return q

    def _run(self) -> None:
        try:
            queue = self._build_queue()
        except Exception:
            log.exception("failed to build trial plan; scheduler exiting")
            return

        try:
            self._client.ensure_logged_in()
        except UnifiError:
            log.exception(
                "could not authenticate to UniFi controller; scheduler exiting"
            )
            return

        ap_name = self._cfg.target.ap_name
        radio = self._cfg.target.radio

        # On restart: if the last recorded trial completed cleanly, we may
        # have missed the transition window while the app was down.  Wait
        # for it now before applying the first channel of this session.
        last = self._storage.last_trial()
        if last and last.get("ended_by") == "dwell_complete":
            self._wait_for_transition_window()

        while not self._stop.is_set():
            trial = queue[0]
            queue.rotate(-1)  # round-robin: move it to the back for next time
            ended_by = self._run_trial(ap_name, radio, trial)
            if ended_by == "dwell_complete":
                self._wait_for_transition_window()

    def _run_trial(self, ap_name: str, radio: str, trial: Trial) -> str:
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
            return "apply_failed"

        started_at = _now_iso()
        trial_id = self._storage.start_trial(
            ap_name, trial.channel, trial.width_mhz
        )
        with self._lock:
            self._current = trial
            self._current_started = started_at

        dwell_seconds = scan.dwell_hours * 3600.0
        # Credit hours already served in consecutive prior interrupted trials
        # for this combo so a restart doesn't reset the full dwell clock.
        prior_h = self._storage.prior_interrupted_hours(
            ap_name, trial.channel, trial.width_mhz
        )
        remaining_seconds = max(0.0, dwell_seconds - prior_h * 3600.0)
        if prior_h > 0:
            log.info(
                "trial %s crediting %.2fh from prior interrupted run(s); "
                "remaining dwell %.2fh",
                trial.label(), prior_h, remaining_seconds / 3600.0,
            )
        deadline = time.monotonic() + remaining_seconds
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
            # If the stop signal fired before the dwell or radar conditions
            # triggered a natural break, the trial was cut short — record
            # that so the queue can resume it rather than skip past it.
            if self._stop.is_set() and ended_by == "dwell_complete":
                ended_by = "interrupted"
            self._storage.end_trial(
                trial_id, ended_by, radar_count=radar_count
            )

        if ended_by == "radar":
            cooldown_s = scan.cooldown_after_radar_minutes * 60.0
            log.info("cooldown for %.0fs", cooldown_s)
            self._sleep(cooldown_s)

        return ended_by

    def _wait_for_transition_window(self) -> None:
        """Block until local time is within the configured transition_hour.

        If transition_hour < 0 (the default), returns immediately so existing
        behaviour is preserved.  Only called after a clean dwell_complete —
        radar-forced recoveries still rotate immediately after cooldown.

        The >=24 h guard is satisfied implicitly: dwell_hours is 24 h, so
        the current trial must have been running for at least that long before
        we arrive here.  We then wait until the next transition window, which
        adds at most another 24 h — so the AP always dwells >=24 h between
        scheduler-driven switches even if a radar hit shortened an earlier
        trial.
        """
        hour = self._cfg.scan.transition_hour
        if hour < 0:
            return
        logged = False
        while not self._stop.is_set():
            now = datetime.now()  # local time
            if now.hour == hour:
                log.info(
                    "transition window open (%02d:xx local), advancing to next trial",
                    hour,
                )
                return
            secs = _seconds_until_hour(hour)
            if not logged:
                log.info(
                    "dwell complete — waiting %.1fh for transition window "
                    "(local hour %02d:00)",
                    secs / 3600.0,
                    hour,
                )
                logged = True
            # Sleep in up-to-1-hour chunks; wake briefly before the window
            # to avoid overshooting into the following hour.
            self._sleep(max(1.0, min(secs - 10.0, 3600.0)))

    def _sleep(self, seconds: float) -> None:
        """Interruptible sleep that returns early on stop."""
        end = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(timeout=min(remaining, 1.0))
