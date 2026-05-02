"""SQLite + CSV storage.

Tables:
  events    — append-only log of every parsed message
  sessions  — derived "AP X was on channel Y from t0 to t1" rows from
              syslog (informational once the scheduler is driving things)
  trials    — scheduler-driven experiments (we *commanded* the AP to a
              specific channel/width and timed how long it survived)

Trials are the primary input for the MTBD ranking once the scheduler is
enabled, because they account for time the AP was actually on a channel
*under our control*.

Concurrency: a single connection is shared across threads with
`check_same_thread=False`. A reentrant lock guards write operations.
SQLite WAL mode lets readers proceed without blocking the writer.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]

from .parser import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    host        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    channel     INTEGER,
    freq_mhz    INTEGER,
    width_mhz   INTEGER,
    raw         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts_idx      ON events(ts);
CREATE INDEX IF NOT EXISTS events_host_ch_idx ON events(host, channel);
CREATE INDEX IF NOT EXISTS events_kind_idx    ON events(kind);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host        TEXT NOT NULL,
    channel     INTEGER NOT NULL,
    width_mhz   INTEGER,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    ended_by    TEXT
);
CREATE INDEX IF NOT EXISTS sessions_host_idx     ON sessions(host);
CREATE INDEX IF NOT EXISTS sessions_open_idx     ON sessions(host, ended_at);
CREATE INDEX IF NOT EXISTS sessions_channel_idx  ON sessions(channel);

CREATE TABLE IF NOT EXISTS trials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ap_name     TEXT NOT NULL,
    channel     INTEGER NOT NULL,
    width_mhz   INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    ended_by    TEXT,
    radar_count INTEGER NOT NULL DEFAULT 0,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS trials_ap_idx     ON trials(ap_name);
CREATE INDEX IF NOT EXISTS trials_open_idx   ON trials(ap_name, ended_at);
CREATE INDEX IF NOT EXISTS trials_combo_idx  ON trials(channel, width_mhz);
"""

CSV_HEADERS = ["ts", "host", "kind", "channel", "freq_mhz", "width_mhz", "raw"]


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


class Storage:
    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "events.db"
        self.csv_path = self.dir / "events.csv"
        self._conn = sqlite3.connect(
            self.db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.executescript(SCHEMA)
        self._write_lock = threading.RLock()
        self._csv_lock = threading.Lock()
        self._init_csv()

    def _init_csv(self) -> None:
        new = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        if new:
            with self.csv_path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADERS)

    def close(self) -> None:
        with self._write_lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            try:
                self._conn.execute("BEGIN")
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # -- events / sessions -------------------------------------------------

    def record(self, ev: Event) -> None:
        ts_iso = _iso(ev.ts)
        with self._tx() as c:
            c.execute(
                "INSERT INTO events(ts, host, kind, channel, freq_mhz, "
                "width_mhz, raw) VALUES (?,?,?,?,?,?,?)",
                (ts_iso, ev.host, ev.kind, ev.channel, ev.freq_mhz,
                 ev.width_mhz, ev.raw),
            )
            self._update_sessions(c, ev, ts_iso)

        with self._csv_lock:
            with self.csv_path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    [ts_iso, ev.host, ev.kind, ev.channel or "",
                     ev.freq_mhz or "", ev.width_mhz or "", ev.raw]
                )

    def _update_sessions(
        self, c: sqlite3.Connection, ev: Event, ts_iso: str
    ) -> None:
        if ev.channel is None:
            return
        open_row = c.execute(
            "SELECT id, channel FROM sessions "
            "WHERE host=? AND ended_at IS NULL",
            (ev.host,),
        ).fetchone()

        if ev.kind == "radar":
            if open_row is not None:
                c.execute(
                    "UPDATE sessions SET ended_at=?, ended_by='radar' "
                    "WHERE id=?",
                    (ts_iso, open_row[0]),
                )
            return

        if ev.kind in ("new_channel", "cac_done"):
            if open_row is not None:
                if open_row[1] == ev.channel:
                    return
                c.execute(
                    "UPDATE sessions SET ended_at=?, ended_by='switch' "
                    "WHERE id=?",
                    (ts_iso, open_row[0]),
                )
            c.execute(
                "INSERT INTO sessions(host, channel, width_mhz, started_at) "
                "VALUES (?,?,?,?)",
                (ev.host, ev.channel, ev.width_mhz, ts_iso),
            )

    def open_session(self, host: str) -> Optional[tuple]:
        return self._conn.execute(
            "SELECT id, channel, started_at FROM sessions "
            "WHERE host=? AND ended_at IS NULL",
            (host,),
        ).fetchone()

    # -- trials ------------------------------------------------------------

    def start_trial(
        self, ap_name: str, channel: int, width_mhz: int, notes: str = ""
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO trials(ap_name, channel, width_mhz, started_at, "
                "notes) VALUES (?,?,?,?,?)",
                (ap_name, channel, width_mhz, _now_iso(), notes),
            )
            return int(cur.lastrowid or 0)

    def end_trial(
        self,
        trial_id: int,
        ended_by: str,
        radar_count: int = 0,
        notes_append: str = "",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE trials SET ended_at=?, ended_by=?, radar_count=?, "
                "notes = CASE WHEN ? = '' THEN notes "
                "             ELSE COALESCE(notes,'') || ? END "
                "WHERE id=?",
                (_now_iso(), ended_by, radar_count,
                 notes_append, ("\n" + notes_append) if notes_append else "",
                 trial_id),
            )

    def count_radar_since(self, since_iso: str) -> int:
        """Count radar events since `since_iso` across ALL hosts.

        AP names reported by syslog and the controller don't always agree
        (case, whitespace, model strings), so we count globally during a
        trial. The scheduler ensures only one trial is active at a time.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind='radar' AND ts >= ?",
            (since_iso,),
        ).fetchone()
        return int(row[0]) if row else 0

    def open_trial(self, ap_name: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM trials WHERE ap_name=? AND ended_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (ap_name,),
        ).fetchone()

    # -- stats -------------------------------------------------------------

    def trial_stats(self) -> list[dict[str, Any]]:
        """Aggregate MTBD-style stats from trials."""
        rows = self._conn.execute(
            """
            SELECT ap_name, channel, width_mhz,
                   SUM((julianday(COALESCE(ended_at, datetime('now'))) -
                        julianday(started_at)) * 24) AS hours,
                   SUM(radar_count)               AS detections,
                   COUNT(*)                       AS trials,
                   SUM(CASE WHEN ended_by='dwell_complete' THEN 1 ELSE 0 END)
                                                  AS clean,
                   MAX(started_at)                AS last_started
              FROM trials
             GROUP BY ap_name, channel, width_mhz
             ORDER BY ap_name, width_mhz, channel
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            hours = float(r["hours"] or 0.0)
            detections = int(r["detections"] or 0)
            mtbd = (hours / detections) if detections else None
            out.append(
                {
                    "ap_name": r["ap_name"],
                    "channel": r["channel"],
                    "width_mhz": r["width_mhz"],
                    "active_hours": round(hours, 2),
                    "detections": detections,
                    "trials": int(r["trials"]),
                    "clean_trials": int(r["clean"]),
                    "mtbd_hours": round(mtbd, 2) if mtbd is not None else None,
                    "last_started": r["last_started"],
                }
            )
        return out

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts, host, kind, channel, width_mhz "
            "FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_trials(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, ap_name, channel, width_mhz, started_at, ended_at, "
            "ended_by, radar_count FROM trials ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def radar_timing(self, tz_name: Optional[str] = None) -> dict[str, Any]:
        """Histogram radar events by hour-of-day and day-of-week in the
        requested local TZ. UTC if `tz_name` is unset / unknown / zoneinfo
        unavailable. Day numbering: 0=Monday ... 6=Sunday (Python convention).
        """
        tz = timezone.utc
        if tz_name and ZoneInfo is not None:
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = timezone.utc

        rows = self._conn.execute(
            "SELECT ts FROM events WHERE kind='radar'"
        ).fetchall()

        by_hour = [0] * 24
        by_dow = [0] * 7
        total = 0
        for r in rows:
            try:
                dt = datetime.fromisoformat(r["ts"])
            except Exception:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(tz)
            by_hour[local.hour] += 1
            by_dow[local.weekday()] += 1
            total += 1

        return {
            "tz": str(tz),
            "total": total,
            "by_hour": by_hour,
            "by_dow": by_dow,
        }

    def conn(self) -> sqlite3.Connection:
        return self._conn
