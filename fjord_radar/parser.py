"""Parse UniFi (and Linux/hostapd) syslog lines into structured events.

We aim to recognise:
  * radar detection (`DFS-RADAR-DETECTED`, kernel `radar detected`,
    "moved to a new channel because radar was detected")
  * channel changes (`DFS-NEW-CHANNEL`, `CTRL-EVENT-CHANNEL-SWITCH`)
  * CAC start / complete (`DFS-CAC-START`, `DFS-CAC-COMPLETED`)

These are the four event types that let us reconstruct "AP X was on
channel Y from t0 to t1", which is what MTBD requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# RFC3164: <PRI>Mmm dd HH:MM:SS HOST TAG: MSG
_SYSLOG_RE = re.compile(
    r"^<(?P<pri>\d+)>"
    r"(?P<ts>[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<rest>.*)$"
)

# Frequency / channel extractors
_FREQ_RE = re.compile(r"freq[=\s](\d{4,5})")
_CHAN_RE = re.compile(r"chan(?:nel)?[=\s](\d{1,3})\b")
_WIDTH_RE = re.compile(r"chan_width=(\d+)")  # hostapd: 0=20,1=40,2=80,3=160

# Event signatures
_RADAR_PATTERNS = [
    re.compile(r"DFS-RADAR-DETECTED", re.I),
    re.compile(r"radar detected", re.I),
    re.compile(r"moved to a new channel because radar was detected", re.I),
]
_NEW_CHAN_PATTERNS = [
    re.compile(r"DFS-NEW-CHANNEL", re.I),
    re.compile(r"CTRL-EVENT-CHANNEL-SWITCH", re.I),
    re.compile(r"channel switch", re.I),
]
_CAC_START_RE = re.compile(r"DFS-CAC-START", re.I)
_CAC_DONE_RE = re.compile(r"DFS-CAC-COMPLETED", re.I)


@dataclass
class Event:
    ts: datetime           # UTC
    host: str              # AP hostname as reported by syslog
    kind: str              # "radar" | "new_channel" | "cac_start" | "cac_done"
    channel: Optional[int]
    freq_mhz: Optional[int]
    width_mhz: Optional[int]
    raw: str


_WIDTH_MAP = {"0": 20, "1": 40, "2": 80, "3": 160}


def _freq_to_chan(freq: int) -> Optional[int]:
    if 5000 < freq < 6000:
        return (freq - 5000) // 5
    if 2400 < freq < 2500:
        return (freq - 2407) // 5
    return None


def _parse_ts(s: str) -> datetime:
    # RFC3164 has no year. Assume current year; if that lands in the future
    # by more than a day, roll back one year (handles Dec/Jan boundary).
    now = datetime.now(timezone.utc)
    try:
        ts = datetime.strptime(f"{now.year} {s}", "%Y %b %d %H:%M:%S")
    except ValueError:
        # Some senders use double-space day padding which strptime handles,
        # but fall back to "now" if the format is unexpected.
        return now
    ts = ts.replace(tzinfo=timezone.utc)
    if (ts - now).total_seconds() > 86400:
        ts = ts.replace(year=now.year - 1)
    return ts


def parse(line: str) -> Optional[Event]:
    """Return an `Event` if the line looks DFS-relevant, else None."""
    line = line.strip().rstrip("\x00")
    if not line:
        return None

    m = _SYSLOG_RE.match(line)
    if m:
        ts = _parse_ts(m.group("ts"))
        host = m.group("host")
        body = m.group("rest")
    else:
        # Some senders skip the BSD header; treat the whole thing as the body.
        ts = datetime.now(timezone.utc)
        host = "unknown"
        body = line

    kind: Optional[str] = None
    if any(p.search(body) for p in _RADAR_PATTERNS):
        kind = "radar"
    elif _CAC_DONE_RE.search(body):
        kind = "cac_done"
    elif _CAC_START_RE.search(body):
        kind = "cac_start"
    elif any(p.search(body) for p in _NEW_CHAN_PATTERNS):
        kind = "new_channel"

    if kind is None:
        return None

    channel: Optional[int] = None
    freq: Optional[int] = None
    if (fm := _FREQ_RE.search(body)) is not None:
        freq = int(fm.group(1))
        channel = _freq_to_chan(freq)
    if (cm := _CHAN_RE.search(body)) is not None:
        channel = int(cm.group(1))

    width: Optional[int] = None
    if (wm := _WIDTH_RE.search(body)) is not None:
        width = _WIDTH_MAP.get(wm.group(1))

    return Event(
        ts=ts,
        host=host,
        kind=kind,
        channel=channel,
        freq_mhz=freq,
        width_mhz=width,
        raw=line,
    )
