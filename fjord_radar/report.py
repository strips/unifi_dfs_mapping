"""CLI MTBD report.

Two views:

* ``--source trials`` (default): rank by completed scheduler trials. This
  is what you want once `scan.enabled=true`.
* ``--source sessions``: rank from passive syslog observation only.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path

try:
    from tabulate import tabulate
except ImportError:  # pragma: no cover
    tabulate = None  # type: ignore[assignment]


def _open(data_dir: str) -> sqlite3.Connection:
    db = Path(data_dir) / "events.db"
    if not db.exists():
        raise SystemExit(f"no database at {db} — has the listener run yet?")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _from_trials(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
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
         ORDER BY width_mhz, ap_name, channel
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        h = float(r["hours"] or 0.0)
        d = int(r["detections"] or 0)
        out.append(
            {
                "ap": r["ap_name"],
                "width": r["width_mhz"],
                "channel": r["channel"],
                "active_h": round(h, 2),
                "detections": d,
                "trials": int(r["trials"]),
                "clean": int(r["clean"]),
                "mtbd_h": round(h / d, 2) if d else None,
                "last_started": r["last_started"],
            }
        )
    return out


def _from_sessions(conn: sqlite3.Connection) -> list[dict]:
    sessions = conn.execute(
        """
        SELECT host, channel,
               (julianday(COALESCE(ended_at, datetime('now'))) -
                julianday(started_at)) * 24 AS hours
        FROM sessions
        """
    ).fetchall()
    hours: dict[tuple[str, int], float] = {}
    for r in sessions:
        h = r["hours"]
        if h is None or h < 0:
            continue
        hours[(r["host"], r["channel"])] = (
            hours.get((r["host"], r["channel"]), 0.0) + h
        )

    detections = {
        (r["host"], r["channel"]): r["c"]
        for r in conn.execute(
            "SELECT host, channel, COUNT(*) AS c FROM events "
            "WHERE kind='radar' AND channel IS NOT NULL GROUP BY host, channel"
        )
    }
    out: list[dict] = []
    for (host, ch), h in hours.items():
        d = int(detections.get((host, ch), 0))
        out.append(
            {
                "ap": host,
                "channel": ch,
                "active_h": round(h, 2),
                "detections": d,
                "mtbd_h": round(h / d, 2) if d else None,
            }
        )
    out.sort(key=lambda r: (r["ap"], r["channel"]))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Fjord-Radar MTBD report")
    p.add_argument(
        "--data-dir",
        default=os.environ.get("FJORD_DATA_DIR", "./data"),
    )
    p.add_argument("--source", choices=("trials", "sessions"), default="trials")
    p.add_argument("--format", choices=("table", "csv"), default="table")
    args = p.parse_args()

    with _open(args.data_dir) as conn:
        rows = _from_trials(conn) if args.source == "trials" else _from_sessions(conn)

    if not rows:
        print(f"no rows from source={args.source}")
        return 0

    if args.format == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        return 0

    if tabulate is None:
        for r in rows:
            print(r)
        return 0
    print(tabulate(rows, headers="keys", floatfmt=".2f"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
