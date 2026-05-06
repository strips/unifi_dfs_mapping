"""Built-in stats web page (Flask).

Single page that shows:
  * the current trial (if any)
  * the MTBD ranking, sorted best → worst per width
  * a visual 5 GHz channel spectrum map (all widths, all global channels)
  * the last 30 trials
  * the last 50 raw events

A `/api/*` JSON surface is exposed for ad-hoc tooling. No write
endpoints, no auth — bind to the LAN only or front it with your
existing reverse proxy if you need access from outside.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any, Callable, Optional

from flask import Flask, jsonify, render_template_string
from werkzeug.serving import make_server

from .config import ScanConfig
from .planner import GROUPS_40, GROUPS_80, GROUPS_160
from .scheduler import Scheduler
from .storage import Storage

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 5 GHz channel taxonomy
# ---------------------------------------------------------------------------

# All standard 5 GHz 20 MHz channels in spectrum order (ITU-R M.1450).
_ALL_5GHZ = [
    36, 40, 44, 48,               # UNII-1
    52, 56, 60, 64,               # UNII-2A
    100, 104, 108, 112,           # UNII-2C low
    116, 120, 124, 128,           # UNII-2C mid  (weather radar zone)
    132, 136, 140, 144,           # UNII-2C high
    149, 153, 157, 161, 165,      # UNII-3
    169, 173, 177,                # UNII-3/4
]

# DFS-required channels (must listen for radar before transmitting).
_DFS_CHANNELS: frozenset[int] = frozenset({
    52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
})

# Weather-radar overlap — shown with diagonal stripe overlay.
# ch 120 (5600 MHz) is on the edge of S-band meteorological radar;
# ch 124 (5610-5630) and ch 128 (5630-5650) are in the core of it.
_WEATHER_RADAR_CHANNELS: frozenset[int] = frozenset({120, 124, 128})

# Band layout for the spectrum table header row.
# Each entry: (label, [channels], has_gap_before)
# UNII-1 (36-48) and UNII-2A (52-64) are adjacent (no frequency gap) and
# share a 160 MHz group, so they are shown as one merged band.
# Note: 52-64 (UNII-2A) are DFS channels just like UNII-2C.
# Each entry: (label, channels, gap_before, sub_labels)
# sub_labels: list of (header_text, col_count) to render multiple header cells
# for one logical band (used for UNII-1/2A which share a 160 MHz group).
_BAND_DEFS = [
    ("UNII-1/2A",  [36, 40, 44, 48, 52, 56, 60, 64],                       False, [("UNII-1", 4), ("UNII-2A", 4)]),
    ("UNII-2C",    [100,104,108,112,116,120,124,128,132,136,140,144],        True,  None),
    ("UNII-3",     [149, 153, 157, 161, 165],                                True,  None),
    ("UNII-3/4",   [169, 173, 177],                                          False, None),
]

# Regulatory availability by ISO 3166-1 numeric country code.
# Maps code → frozenset of available 20 MHz channels.
# Groups of countries that share a regulatory domain are listed together.
def _make_regulatory_db() -> dict[int, frozenset[int]]:
    # EU/ETSI: 36-64 (indoor only above 100 mW, DFS on 52-64) + 100-144 (DFS)
    # UNII-3 (149-177) is NOT permitted in the EU.
    _eu = frozenset([36,40,44,48,52,56,60,64,
                     100,104,108,112,116,120,124,128,132,136,140])
                     # ch 144 excluded — not supported by UniFi in many EU deployments
    # US/FCC: all channels including UNII-3/4
    _us = frozenset(_ALL_5GHZ)
    # UK: like EU but 149-165 allowed as SRD (25 mW)
    _uk = frozenset([36,40,44,48,52,56,60,64,
                     100,104,108,112,116,120,124,128,132,136,140,144,
                     149,153,157,161,165])
    # Australia: similar to EU + 149-165
    _au = frozenset([36,40,44,48,52,56,60,64,
                     100,104,108,112,116,120,124,128,132,136,140,144,
                     149,153,157,161,165])
    # Japan: 36-64 + 100-140 (no 144, no UNII-3)
    _jp = frozenset([36,40,44,48,52,56,60,64,
                     100,104,108,112,116,120,124,128,132,136,140])
    db: dict[int, frozenset[int]] = {}
    # EU/ETSI members + Norway (578)
    for code in [578,276,250,528,752,724,380,620,56,348,203,616,
                 40,300,372,356,246,208,191,705,703,642,804,826,
                 756,442,440,233,428,440,703,705]:
        db[code] = _eu
    db[826] = _uk          # UK (post-Brexit override)
    db[840] = _us          # United States
    db[124] = _us          # Canada (same as US for 5 GHz)
    db[36]  = _au          # Australia
    db[554] = _au          # New Zealand
    db[392] = _jp          # Japan
    return db

_REGULATORY_DB: dict[int, frozenset[int]] = _make_regulatory_db()

# Fallback: show all channels when country is unknown
_ALL_5GHZ_SET: frozenset[int] = frozenset(_ALL_5GHZ)


def _channels_for_country(country_code: int) -> frozenset[int]:
    return _REGULATORY_DB.get(country_code, _ALL_5GHZ_SET)


# ---------------------------------------------------------------------------
# Aggregation: fan trial stats out to each covered 20 MHz sub-channel
# ---------------------------------------------------------------------------

def _aggregate_by_subchannel(
    stats: list[dict[str, Any]],
) -> dict[int, dict[str, float]]:
    """For each 20 MHz channel, accumulate hours and radar counts across
    all trials that covered it at any width."""
    agg: dict[int, dict[str, float]] = {}
    for ch in _ALL_5GHZ:
        agg[ch] = {"hours": 0.0, "radar": 0.0}

    for row in stats:
        ch = row["channel"]
        hours = row["active_hours"]
        radar = row["radar"] if "radar" in row else row.get("detections", 0)
        width = row["width_mhz"]

        # Determine which 20 MHz sub-channels this trial covered.
        sub: list[int] = []
        if width == 20:
            sub = [ch]
        elif width == 40:
            for pair in GROUPS_40:
                if pair[0] == ch:
                    sub = list(pair)
                    break
        elif width == 80:
            for grp in GROUPS_80:
                if grp[0] == ch:
                    sub = list(grp)
                    break
        elif width == 160:
            for grp in GROUPS_160:
                if grp[0] == ch:
                    sub = list(grp)
                    break

        if not sub:
            sub = [ch]

        for sc in sub:
            if sc in agg:
                agg[sc]["hours"] += hours
                agg[sc]["radar"] += radar

    return agg


# ---------------------------------------------------------------------------
# Color computation
# ---------------------------------------------------------------------------

def _cell_color(
    ch: int,
    hours: float,
    radar: float,
    scan_set: frozenset[int],
    blacklist_set: frozenset[int],
    available_set: frozenset[int],
    current_ch: Optional[int],
    force_dfs: bool = False,
) -> dict[str, Any]:
    """Return a dict with keys: bg, classes, tooltip_extra.

    ``force_dfs`` should be True when the cell represents a wide channel
    whose sub-channels include at least one DFS channel (e.g. 36 @ 160 MHz
    covers 36-64, where 52-64 are DFS).  In that case the non-DFS fast-path
    (solid blue) is bypassed so the cell is coloured by DFS rules.
    """
    classes = []
    tooltip_extra = ""

    if ch == current_ch:
        classes.append("ch-current")

    if ch in _WEATHER_RADAR_CHANNELS:
        classes.append("ch-weather")

    if ch not in available_set:
        # Not legal in this country
        if ch in {169, 173, 177}:
            bg = "#999999"
            classes.append("ch-no-region")
            tooltip_extra = "Not available in this region"
        else:
            bg = "#c8c8c8"
            classes.append("ch-unavail")
            tooltip_extra = "Not available in this region"
        return {"bg": bg, "classes": classes, "tooltip_extra": tooltip_extra}

    if ch not in _DFS_CHANNELS and not force_dfs:
        # Non-DFS — always light blue; excluded from DFS scanning by design
        bg = "#a8c8e8"
        classes.append("ch-nondfs")
        return {"bg": bg, "classes": classes, "tooltip_extra": tooltip_extra}

    # DFS channel
    if ch in blacklist_set:
        classes.append("ch-blacklisted")
        tooltip_extra = "Blacklisted in config"

    if ch not in scan_set:
        bg = "#f0f0f0"
        if "ch-blacklisted" not in classes:
            classes.append("ch-blacklisted")
        if not tooltip_extra:
            tooltip_extra = "Not enabled in scan pool"
        return {"bg": bg, "classes": classes, "tooltip_extra": tooltip_extra}

    if hours < 0.5:
        # No data yet
        bg = "#f0f0f0"
        return {"bg": bg, "classes": classes, "tooltip_extra": tooltip_extra}

    if radar == 0:
        # Clean — green that deepens with observed hours.
        # At 1h: pale green.  At 24h: medium.  At 96h+: deep forest green.
        confidence = min(hours / 96.0, 1.0)
        sat = int(30 + confidence * 60)   # 30 → 90 %
        light = int(78 - confidence * 33) # 78 → 45 %
        bg = f"hsl(120,{sat}%,{light}%)"
    else:
        # Radar detected — interpolate green → yellow → red by log rate.
        rate = radar / max(hours, 0.5)
        # t=0 at rate≈0.001/h (barely noticed), t=1 at rate=1/h (every hour)
        t = math.log10(rate * 1000.0 + 1.0) / math.log10(1001.0)
        t = max(0.0, min(1.0, t))
        hue = int(120 * (1.0 - t))   # 120=green → 0=red
        sat = 75
        light = 45
        bg = f"hsl({hue},{sat}%,{light}%)"

    return {"bg": bg, "classes": classes, "tooltip_extra": tooltip_extra}


# ---------------------------------------------------------------------------
# Build the complete channel map structure for the template
# ---------------------------------------------------------------------------

def _na_cell(colspan: int = 1) -> dict[str, Any]:
    """Placeholder for a (channel, width) combination that is not valid
    in 802.11 (e.g. 132 MHz @ 160 MHz width).  Rendered as invisible."""
    return {
        "ch": -1, "label": "", "colspan": colspan,
        "bg": "transparent", "classes_str": "ch-cell ch-na",
        "tooltip": "Not a valid combination at this width",
        "is_gap": False, "is_na": True,
    }


def _collapse_na(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive N/A placeholder cells into one wide cell so the
    table looks clean rather than having many tiny invisible cells."""
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(cells):
        if cells[i].get("is_na"):
            span = cells[i]["colspan"]
            j = i + 1
            while j < len(cells) and cells[j].get("is_na"):
                span += cells[j]["colspan"]
                j += 1
            result.append(_na_cell(span))
            i = j
        else:
            result.append(cells[i])
            i += 1
    return result


def _build_channel_map(
    stats: list[dict[str, Any]],
    scan_config: Optional[ScanConfig],
    country_code: int,
    current_trial: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a list of band dicts, each containing rows keyed by width.

    Each row only uses data from trials run at that exact width, so amber
    on the 80 MHz row does NOT bleed into the 40/20 MHz rows.

    Each cell: { ch, label, colspan, bg, classes_str, tooltip, is_gap }
    Band dict: { label, gap_before, ch_count, sub_labels, rows }
    """
    scan_set: frozenset[int] = frozenset(
        scan_config.channels if scan_config else []
    )
    blacklist_set: frozenset[int] = frozenset(
        scan_config.blacklist_channels if scan_config else []
    )
    available_set = _channels_for_country(country_code)

    # Per-width aggregation: {width: {primary_ch: {hours, radar}}}
    # Only trials at exactly that width feed into its row.
    agg_w: dict[int, dict[int, dict[str, float]]] = {20: {}, 40: {}, 80: {}, 160: {}}
    for row in stats:
        w = row["width_mhz"]
        ch = row["channel"]
        if w not in agg_w:
            continue
        if ch not in agg_w[w]:
            agg_w[w][ch] = {"hours": 0.0, "radar": 0.0}
        agg_w[w][ch]["hours"] += row["active_hours"]
        agg_w[w][ch]["radar"] += row.get("radar", row.get("detections", 0))

    def _make_cell(
        ch: int, width: int, colspan: int = 1, label: Optional[str] = None,
        group: Optional[list] = None,
    ) -> dict[str, Any]:
        d = agg_w[width].get(ch, {"hours": 0.0, "radar": 0.0})
        hours = d["hours"]
        radar = d["radar"]
        # Orange border only if this exact (channel, width) is the active trial.
        cur_ch = (
            current_trial["channel"]
            if current_trial and current_trial["width_mhz"] == width
            else None
        )
        # For wide cells (40/80/160 MHz), force DFS rules if any sub-channel
        # is DFS — e.g. 36@160 spans 36–64, where 52–64 are DFS channels.
        cell_channels = group if group is not None else [ch]
        force_dfs = any(c in _DFS_CHANNELS for c in cell_channels)
        color_info = _cell_color(
            ch, hours, radar, scan_set, blacklist_set, available_set, cur_ch,
            force_dfs=force_dfs,
        )
        if hours >= 0.5 and radar == 0:
            mtbd_str = "∞"
        elif hours >= 0.5 and radar > 0:
            mtbd_str = f"{hours/radar:.0f}h"
        else:
            mtbd_str = "–"
        tooltip_parts = [
            f"ch {ch} @ {width} MHz",
            f"{hours:.1f}h observed",
            f"{int(radar)} radar pings",
            f"MTBD {mtbd_str}",
        ]
        if color_info["tooltip_extra"]:
            tooltip_parts.append(color_info["tooltip_extra"])
        return {
            "ch": ch,
            "label": label or str(ch),
            "colspan": colspan,
            "bg": color_info["bg"],
            "classes_str": " ".join(["ch-cell"] + color_info["classes"]),
            "tooltip": " | ".join(tooltip_parts),
            "is_gap": False,
        }

    bands = []
    for band_label, channels, gap_before, sub_labels in _BAND_DEFS:
        ch_set = set(channels)
        rows: dict[int, list[dict[str, Any]]] = {20: [], 40: [], 80: [], 160: []}

        # 20 MHz row — one cell per channel
        for ch in channels:
            rows[20].append(_make_cell(ch, 20))

        # 40 MHz row — merged pairs; N/A where no valid pair exists
        covered_40: set[int] = set()
        for ch in channels:
            if ch in covered_40:
                continue
            pair = next(
                (p for p in GROUPS_40 if p[0] == ch and p[1] in ch_set), None
            )
            if pair:
                rows[40].append(_make_cell(pair[0], 40, colspan=2,
                                           label=f"{pair[0]}–{pair[1]}",
                                           group=list(pair)))
                covered_40.update(pair)
            else:
                rows[40].append(_na_cell())
                covered_40.add(ch)
        rows[40] = _collapse_na(rows[40])

        # 80 MHz row
        covered_80: set[int] = set()
        for ch in channels:
            if ch in covered_80:
                continue
            grp = next(
                (g for g in GROUPS_80
                 if g[0] == ch and all(c in ch_set for c in g)), None
            )
            if grp:
                rows[80].append(_make_cell(grp[0], 80, colspan=len(grp),
                                           label=f"{grp[0]}–{grp[-1]}",
                                           group=list(grp)))
                covered_80.update(grp)
            else:
                rows[80].append(_na_cell())
                covered_80.add(ch)
        rows[80] = _collapse_na(rows[80])

        # 160 MHz row
        covered_160: set[int] = set()
        for ch in channels:
            if ch in covered_160:
                continue
            grp = next(
                (g for g in GROUPS_160
                 if g[0] == ch and all(c in ch_set for c in g)), None
            )
            if grp:
                rows[160].append(_make_cell(grp[0], 160, colspan=len(grp),
                                            label=f"{grp[0]}–{grp[-1]}",
                                            group=list(grp)))
                covered_160.update(grp)
            else:
                rows[160].append(_na_cell())
                covered_160.add(ch)
        rows[160] = _collapse_na(rows[160])

        bands.append({
            "label": band_label,
            "gap_before": gap_before,
            "ch_count": len(channels),
            "sub_labels": sub_labels,
            "rows": rows,
        })

    return bands




def _ranked(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort: clean (no detections) first, then highest MTBD, then largest
    width."""
    def key(r: dict[str, Any]) -> tuple:
        det = r["detections"]
        mtbd = r["mtbd_hours"] if r["mtbd_hours"] is not None else 1e12
        return (det == 0, mtbd, r["width_mhz"], r["channel"])
    return sorted(stats, key=key, reverse=True)


def _fmt_age(ts: Optional[float], now: float) -> Optional[str]:
    if ts is None:
        return None
    s = max(0, int(now - ts))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _decorate_listener(stats: dict[str, Any]) -> dict[str, Any]:
    import time as _time
    now = _time.time()
    out = dict(stats)
    out["last_packet_human"] = _fmt_age(stats.get("last_packet_ts"), now)
    out["last_event_human"] = _fmt_age(stats.get("last_event_ts"), now)
    out["uptime_human"] = _fmt_age(stats.get("started_at"), now)
    return out


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Fjord-Radar</title>
  <meta http-equiv="refresh" content="10">
  <style>
    body { font-family: system-ui, sans-serif; margin: 2rem; max-width: 1200px;
           color: #222; }
    h1 { margin: 0 0 .25rem 0; }
    .sub { color: #666; margin-bottom: 1.5rem; }
    section { margin: 2rem 0; }
    table { border-collapse: collapse; width: 100%; }
    th, td { padding: .35rem .6rem; border-bottom: 1px solid #eee;
             text-align: left; }
    th { background: #f4f4f4; font-weight: 600; }
    tbody tr:hover { background: #fafafa; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px;
            font-size: .8em; }
    .ok { background: #d8f5d8; color: #14571a; }
    .bad { background: #ffe2e2; color: #8a1010; }
    .pending { background: #fff5cc; color: #7a5a00; }
    .muted { color: #888; }
    code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; }
    .counter { display: inline-block; margin: 0 1.5rem 0 0;
               vertical-align: top; }
    .counter .v { display: block; font-size: 2.4rem; font-weight: 700;
                  font-variant-numeric: tabular-nums; line-height: 1.1; }
    .counter .l { display: block; font-size: .8rem; color: #666;
                  text-transform: uppercase; letter-spacing: .04em; }
    .counter.hot .v { color: #b34d4d; }
    .counter.cool .v { color: #14571a; }
    .bar { display: inline-block; height: 14px; background: #b34d4d;
           vertical-align: middle; border-radius: 2px; }
    .bar-cell { padding: 2px 6px; }
    .bar-cell .lbl { display: inline-block; width: 3.5em;
                     font-variant-numeric: tabular-nums; color: #555; }
    .bar-cell .cnt { display: inline-block; width: 3em; text-align: right;
                     font-variant-numeric: tabular-nums; color: #333;
                     margin-left: .4rem; }
    /* --- channel spectrum map --- */
    .chmap-wrap { overflow-x: auto; }
    .chmap { border-collapse: separate; border-spacing: 2px;
             table-layout: fixed; }
    .chmap th.band-hdr { text-align: center; font-size: .72em;
                         letter-spacing: .03em; text-transform: uppercase;
                         color: #555; background: #f4f4f4;
                         border-radius: 4px 4px 0 0; padding: 3px 0; }
    .chmap td.row-lbl { font-size: .72em; color: #777; white-space: nowrap;
                        padding-right: 6px; text-align: right;
                        vertical-align: middle; width: 3.2em; }
    .chmap td.gap-col { width: 8px; }
    .ch-cell { width: 36px; height: 48px; text-align: center;
               vertical-align: middle; font-size: .68em; font-weight: 600;
               border-radius: 4px; cursor: default;
               line-height: 1.2; padding: 2px; }
    .ch-cell.ch-weather {
      background-image: repeating-linear-gradient(
        -45deg,
        transparent, transparent 4px,
        rgba(0,0,0,.10) 4px, rgba(0,0,0,.10) 5px
      );
      background-blend-mode: multiply; }
    .ch-cell.ch-blacklisted { outline: 1px dashed #888; outline-offset: -2px; }
    .ch-cell.ch-current { outline: 3px solid #f90; outline-offset: -2px;
                           box-shadow: 0 0 6px rgba(255,153,0,.5); }
    .ch-cell.ch-no-region { color: #fff; }
    .ch-cell.ch-nondfs { color: #fff; }
    .ch-cell.ch-na { background: transparent !important; border: none;
                     pointer-events: none; }
    .chmap-legend { display: flex; align-items: center; gap: .6rem;
                    margin-top: .6rem; font-size: .78em; flex-wrap: wrap; }
    .chmap-legend .leg-swatch { display: inline-block; width: 18px; height: 14px;
                                border-radius: 3px; vertical-align: middle; }
    .chmap-legend .leg-bar {
      width: 140px; height: 14px; border-radius: 3px;
      background: linear-gradient(to right, hsl(120,30%,78%), hsl(120,90%,45%));
      display: inline-block; vertical-align: middle; }
    .chmap-legend .leg-bar.red {
      background: linear-gradient(to right, hsl(90,75%,45%), hsl(0,75%,45%)); }
  </style>
</head>
<body>
  <h1>Fjord-Radar</h1>
  <div class="sub">UniFi DFS / radar channel mapper</div>

  <section style="margin-top: 0;">
    <div class="counter {{ 'hot' if (timing.total or 0) > 0 else 'cool' }}">
      <span class="v">{{ timing.total or 0 }}</span>
      <span class="l">Radar pings</span>
    </div>
    {% if listener %}
    <div class="counter">
      <span class="v">{{ listener.packets_total }}</span>
      <span class="l">Syslog packets</span>
    </div>
    <div class="counter">
      <span class="v">{{ listener.events_total }}</span>
      <span class="l">DFS events</span>
    </div>
    {% endif %}
  </section>

  <section>
    <h2>Listener</h2>
    {% if listener %}
      <table style="max-width: 720px;">
        <tbody>
          <tr><th>Bind</th><td><code>{{ listener.bind }}</code></td></tr>
          <tr>
            <th>Packets received</th>
            <td class="num">
              {% if listener.packets_total > 0 %}
                <span class="pill ok">{{ listener.packets_total }}</span>
              {% else %}
                <span class="pill bad">0</span>
              {% endif %}
            </td>
          </tr>
          <tr><th>Lines parsed</th>
              <td class="num">{{ listener.lines_total }}</td></tr>
          <tr><th>DFS events stored</th>
              <td class="num">{{ listener.events_total }}</td></tr>
          <tr><th>Ignored (non-DFS)</th>
              <td class="num muted">{{ listener.ignored_total }}</td></tr>
          <tr><th>Last packet</th>
              <td class="muted">
                {% if listener.last_packet_human %}
                  {{ listener.last_packet_human }} ago
                  from <code>{{ listener.last_packet_from }}</code>
                {% else %}never{% endif %}
              </td></tr>
          <tr><th>Last DFS event</th>
              <td class="muted">
                {% if listener.last_event_human %}
                  {{ listener.last_event_human }} ago
                {% else %}never{% endif %}
              </td></tr>
          <tr><th>Uptime</th>
              <td class="muted">{{ listener.uptime_human or '—' }}</td></tr>
        </tbody>
      </table>
    {% else %}
      <p class="muted">Listener stats unavailable.</p>
    {% endif %}
  </section>

  <section>
    <h2>Current trial</h2>
    {% if current %}
      <p>
        <span class="pill pending">running</span>
        <code>ch{{ current.channel }} @ {{ current.width_mhz }} MHz</code>
        — started {{ current.started_at }}
      </p>
    {% else %}
      <p class="muted">No active trial.</p>
    {% endif %}
  </section>

  <section>
    <h2>MTBD ranking</h2>
    {% if stats %}
      <table>
        <thead>
          <tr>
            <th>AP</th><th>Width</th><th>Channel</th>
            <th class="num">Active hours</th>
            <th class="num">Detections</th>
            <th class="num">Trials</th>
            <th class="num">Clean</th>
            <th class="num">MTBD (h)</th>
            <th>Last started</th>
          </tr>
        </thead>
        <tbody>
        {% for r in stats %}
          <tr>
            <td>{{ r.ap_name }}</td>
            <td>{{ r.width_mhz }} MHz</td>
            <td>{{ r.channel }}</td>
            <td class="num">{{ "%.2f"|format(r.active_hours) }}</td>
            <td class="num">
              {% if r.detections == 0 %}
                <span class="pill ok">0</span>
              {% else %}
                <span class="pill bad">{{ r.detections }}</span>
              {% endif %}
            </td>
            <td class="num">{{ r.trials }}</td>
            <td class="num">{{ r.clean_trials }}</td>
            <td class="num">
              {% if r.mtbd_hours is none %}
                <span class="muted">&#8734;</span>
              {% else %}
                {{ "%.2f"|format(r.mtbd_hours) }}
              {% endif %}
            </td>
            <td class="muted">{{ r.last_started or '' }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No completed trials yet.</p>
    {% endif %}
  </section>

  <section id="channel-map">
    <h2>5 GHz channel spectrum map</h2>
    {% if channel_map %}
    <div class="chmap-wrap">
      <table class="chmap">
        <thead>
          <tr>
            <th class="row-lbl" style="border:none; background:#f4f4f4; border-radius:4px 4px 0 0; font-size:.72em; color:#555; padding:3px 6px 3px 0; text-align:right;">MHz</th>
            {% for band in channel_map %}
              {% if band.gap_before %}<th class="gap-col"></th>{% endif %}
              {% if band.sub_labels %}
                {% for sub_label, sub_count in band.sub_labels %}
                  <th class="band-hdr" colspan="{{ sub_count }}">{{ sub_label }}</th>
                {% endfor %}
              {% else %}
                <th class="band-hdr" colspan="{{ band.ch_count }}">{{ band.label }}</th>
              {% endif %}
            {% endfor %}
          </tr>
        </thead>
        <tbody>
        {% for width in [20, 40, 80, 160] %}
          <tr>
            <td class="row-lbl">{{ width }}</td>
            {% for band in channel_map %}
              {% if band.gap_before %}<td class="gap-col"></td>{% endif %}
              {% for cell in band.rows[width] %}
                <td class="{{ cell.classes_str }}"
                    colspan="{{ cell.colspan }}"
                    style="background-color: {{ cell.bg }};"
                    title="{{ cell.tooltip }}">{{ cell.label }}</td>
              {% endfor %}
            {% endfor %}
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
    <div class="chmap-legend">
      <span><span class="leg-swatch" style="background:#a8c8e8;"></span> Non-DFS</span>
      <span><span class="leg-swatch" style="background:#f0f0f0; border:1px solid #ccc;"></span> DFS &#8212; no data / not scanned</span>
      <span><span class="leg-bar"></span> Clean &#8594; more hours observed</span>
      <span><span class="leg-bar red"></span> Radar detected (rare &#8594; frequent)</span>
      <span><span class="leg-swatch" style="background:#c8c8c8;"></span> Unavailable (region)</span>
      <span><span class="leg-swatch" style="background:#999999;"></span> UNII-4 &#8212; not in region</span>
      <span>&#9621; Weather radar overlap &nbsp; &#9484;&#9480;&#9488; Blacklisted / not enabled &nbsp; <span style="color:#f90;">&#9632;</span> Active trial</span>
    </div>
    <p class="muted" style="margin-top:.4rem;">
      Green deepens with hours of clean observation (confidence).
      Red intensity = radar hit rate (log scale).
      Hover any cell for details.
    </p>
    {% else %}
      <p class="muted">No trial data yet &#8212; start scanning to populate.</p>
    {% endif %}
  </section>

  <section>
    <h2>Radar timing
      {% if timing %}<span class="muted" style="font-size: .7em;">
        ({{ timing.total }} radar events, TZ={{ timing.tz }})
      </span>{% endif %}
    </h2>
    {% if timing and timing.total > 0 %}
      <div style="display: flex; gap: 3rem; flex-wrap: wrap;">
        <div>
          <h3 style="font-size: 1em; margin: 0 0 .4rem 0;">Hour of day</h3>
          <table>
            <tbody>
            {% for h in range(24) %}
              {% set cnt = timing.by_hour[h] %}
              {% set pct = (cnt * 100 / timing.hour_max) if timing.hour_max else 0 %}
              <tr><td class="bar-cell">
                <span class="lbl">{{ '%02d'|format(h) }}:00</span>
                <span class="bar" style="width: {{ pct * 1.6 }}px;"></span>
                <span class="cnt">{{ cnt }}</span>
              </td></tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
        <div>
          <h3 style="font-size: 1em; margin: 0 0 .4rem 0;">Day of week</h3>
          <table>
            <tbody>
            {% set days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'] %}
            {% for d in range(7) %}
              {% set cnt = timing.by_dow[d] %}
              {% set pct = (cnt * 100 / timing.dow_max) if timing.dow_max else 0 %}
              <tr><td class="bar-cell">
                <span class="lbl">{{ days[d] }}</span>
                <span class="bar" style="width: {{ pct * 2.4 }}px;"></span>
                <span class="cnt">{{ cnt }}</span>
              </td></tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    {% else %}
      <p class="muted">No radar events recorded yet.</p>
    {% endif %}
  </section>

  <section>
    <h2>Recent trials</h2>
    <table>
      <thead>
        <tr><th>#</th><th>Started</th><th>Ended</th><th>AP</th>
            <th>Combo</th><th>Outcome</th><th class="num">Radar</th></tr>
      </thead>
      <tbody>
      {% for t in trials %}
        <tr>
          <td>{{ t.id }}</td>
          <td class="muted">{{ t.started_at }}</td>
          <td class="muted">{{ t.ended_at or '&#8212;' }}</td>
          <td>{{ t.ap_name }}</td>
          <td><code>ch{{ t.channel }}@{{ t.width_mhz }}</code></td>
          <td>
            {% if t.ended_by == 'dwell_complete' %}
              <span class="pill ok">survived</span>
            {% elif t.ended_by == 'radar' %}
              <span class="pill bad">radar</span>
            {% elif t.ended_by %}
              <span class="pill pending">{{ t.ended_by }}</span>
            {% else %}
              <span class="pill pending">running</span>
            {% endif %}
          </td>
          <td class="num">{{ t.radar_count }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </section>

  <section>
    <h2>Recent events</h2>
    <table>
      <thead>
        <tr><th>Timestamp</th><th>Host</th><th>Kind</th>
            <th class="num">Channel</th><th class="num">Width</th></tr>
      </thead>
      <tbody>
      {% for e in events %}
        <tr>
          <td class="muted">{{ e.ts }}</td>
          <td>{{ e.host }}</td>
          <td>{{ e.kind }}</td>
          <td class="num">{{ e.channel or '' }}</td>
          <td class="num">{{ e.width_mhz or '' }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </section>

  <p class="muted">
    Auto-refresh every 10 s. JSON: <code>/api/stats</code>,
    <code>/api/trials</code>, <code>/api/events</code>,
    <code>/api/status</code>, <code>/api/listener</code>,
    <code>/api/radar_timing</code>, <code>/api/channel_map</code>,
    <code>/healthz</code>.
  </p>
</body>
</html>
"""


def build_app(
    storage: Storage,
    scheduler: Optional[Scheduler],
    listener_stats: Optional[Callable[[], dict[str, Any]]] = None,
    scan_config: Optional[ScanConfig] = None,
    country_code: int = 578,
) -> Flask:
    app = Flask(__name__)
    tz_name = os.environ.get("FJORD_TZ") or os.environ.get("TZ") or "UTC"

    def _timing() -> dict[str, Any]:
        t = storage.radar_timing(tz_name)
        t["hour_max"] = max(t["by_hour"]) if t["by_hour"] else 0
        t["dow_max"] = max(t["by_dow"]) if t["by_dow"] else 0
        return t

    def _cmap(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current = scheduler.status()["current_trial"] if scheduler else None
        return _build_channel_map(stats, scan_config, country_code, current)

    @app.get("/")
    def index() -> Any:
        stats = _ranked(storage.trial_stats())
        trials = storage.recent_trials(limit=30)
        events = storage.recent_events(limit=50)
        current = scheduler.status()["current_trial"] if scheduler else None
        listener = (
            _decorate_listener(listener_stats()) if listener_stats else None
        )
        return render_template_string(
            _TEMPLATE,
            stats=stats,
            trials=trials,
            events=events,
            current=current,
            listener=listener,
            timing=_timing(),
            channel_map=_cmap(stats),
        )

    @app.get("/api/stats")
    def api_stats() -> Any:
        return jsonify(_ranked(storage.trial_stats()))

    @app.get("/api/trials")
    def api_trials() -> Any:
        return jsonify(storage.recent_trials(limit=200))

    @app.get("/api/events")
    def api_events() -> Any:
        return jsonify(storage.recent_events(limit=200))

    @app.get("/api/status")
    def api_status() -> Any:
        return jsonify(scheduler.status() if scheduler else {})

    @app.get("/api/listener")
    def api_listener() -> Any:
        if listener_stats is None:
            return jsonify({})
        return jsonify(_decorate_listener(listener_stats()))

    @app.get("/api/radar_timing")
    def api_radar_timing() -> Any:
        return jsonify(storage.radar_timing(tz_name))

    @app.get("/api/channel_map")
    def api_channel_map() -> Any:
        stats = _ranked(storage.trial_stats())
        current = scheduler.status()["current_trial"] if scheduler else None
        current_ch = current["channel"] if current else None
        # Return a flat list of per-channel summaries (easier to consume
        # than the nested band structure used by the HTML template).
        agg = _aggregate_by_subchannel(stats)
        scan_set: frozenset[int] = frozenset(
            scan_config.channels if scan_config else []
        )
        blacklist_set: frozenset[int] = frozenset(
            scan_config.blacklist_channels if scan_config else []
        )
        available_set = _channels_for_country(country_code)
        out = []
        for ch in _ALL_5GHZ:
            d = agg.get(ch, {"hours": 0.0, "radar": 0.0})
            color_info = _cell_color(
                ch, d["hours"], d["radar"],
                scan_set, blacklist_set, available_set, current_ch,
            )
            mtbd = None
            if d["hours"] >= 0.5 and d["radar"] > 0:
                mtbd = round(d["hours"] / d["radar"], 2)
            out.append({
                "channel": ch,
                "hours": round(d["hours"], 2),
                "radar": int(d["radar"]),
                "mtbd_hours": mtbd,
                "color": color_info["bg"],
                "available": ch in available_set,
                "dfs": ch in _DFS_CHANNELS,
                "weather_radar": ch in _WEATHER_RADAR_CHANNELS,
                "in_scan_pool": ch in scan_set,
                "blacklisted": ch in blacklist_set,
                "is_current": ch == current_ch,
            })
        return jsonify(out)

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify({"ok": True})

    return app


class WebServer:
    """Run Flask via Werkzeug's threaded WSGI server in a background
    thread. Werkzeug is already a dependency of Flask, so no extra
    runtime needed."""

    def __init__(
        self,
        storage: Storage,
        scheduler: Optional[Scheduler],
        host: str,
        port: int,
        listener_stats: Optional[Callable[[], dict[str, Any]]] = None,
        scan_config: Optional[ScanConfig] = None,
        country_code: int = 578,
    ) -> None:
        app = build_app(
            storage, scheduler, listener_stats,
            scan_config=scan_config,
            country_code=country_code,
        )
        self._server = make_server(host, port, app, threaded=True)
        self._thread: Optional[threading.Thread] = None
        log.info("web on http://%s:%d", host, port)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="web",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:  # pragma: no cover
            log.exception("web shutdown failed")
        if self._thread is not None:
            self._thread.join(timeout=5)
