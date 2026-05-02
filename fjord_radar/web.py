"""Built-in stats web page (Flask).

Single page that shows:
  * the current trial (if any)
  * the MTBD ranking, sorted best → worst per width
  * the last 30 trials
  * the last 50 raw events

A `/api/*` JSON surface is exposed for ad-hoc tooling. No write
endpoints, no auth — bind to the LAN only or front it with your
existing reverse proxy if you need access from outside.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional

from flask import Flask, jsonify, render_template_string
from werkzeug.serving import make_server

from .scheduler import Scheduler
from .storage import Storage

log = logging.getLogger(__name__)


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
                <span class="muted">∞</span>
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
          <td class="muted">{{ t.ended_at or '—' }}</td>
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
    <code>/api/radar_timing</code>, <code>/healthz</code>.
  </p>
</body>
</html>
"""


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


def build_app(
    storage: Storage,
    scheduler: Optional[Scheduler],
    listener_stats: Optional[Callable[[], dict[str, Any]]] = None,
) -> Flask:
    app = Flask(__name__)
    tz_name = os.environ.get("FJORD_TZ") or os.environ.get("TZ") or "UTC"

    def _timing() -> dict[str, Any]:
        t = storage.radar_timing(tz_name)
        t["hour_max"] = max(t["by_hour"]) if t["by_hour"] else 0
        t["dow_max"] = max(t["by_dow"]) if t["by_dow"] else 0
        return t

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
    ) -> None:
        app = build_app(storage, scheduler, listener_stats)
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
