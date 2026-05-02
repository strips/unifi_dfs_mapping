# Project Fjord-Radar

Long-running mapper for **UniFi DFS / radar events** that finds the most
stable, widest 5 GHz channel by:

1. Receiving syslog from your UniFi UDR on UDP/514.
2. Optionally driving the target AP through a **planned sequence of
   (channel, width) trials** via the UniFi Network REST API.
3. Recording every event + every trial into SQLite (+ append-only CSV).
4. Serving a built-in stats web page and JSON API on TCP/8080.

> The name is purely thematic — the project sits between Drammensfjorden
> and Oslofjorden, hence "Fjord-Radar". Rename freely; nothing in the
> code depends on the string.

## Why this design

- **MTBD over PPD.** Pings-per-day is mathematically invalid because an
  AP that detects radar vacates a channel for the rest of the day.
  Mean-Time-Between-Detections accounts for actual on-channel time.
  See [docs/PROJECT.md](docs/PROJECT.md).
- **Width matters but is constrained.** A 40/80 MHz channel only stays
  up if **all** of its 20 MHz sub-channels stay clean. The planner
  enforces this for you (`fjord_radar/planner.py`).
- **Trials are the source of truth.** Once `scan.enabled=true`, the
  controller is the *only* thing setting the channel. Each "AP X was on
  ch Y at width W from t0 to t1" interval is a `trials` row, ended
  either by `dwell_complete` (survived) or `radar` (forced off).

## Repo layout

```
fjord_radar/
├── app.py            Orchestrator (listener + scheduler + web in one process)
├── config.py         YAML loader; secrets ONLY from env, never YAML
├── listener.py       UDP syslog server (thread)
├── parser.py         Regex for DFS-RADAR / DFS-NEW-CHANNEL / CAC events
├── planner.py        Builds the (channel, width) trial set
├── scheduler.py      Drives the AP through trials, owns `trials` rows
├── unifi_client.py   Minimal UniFi Network REST client
├── storage.py        SQLite (WAL) + CSV; thread-safe writes
├── web.py            Flask stats page + JSON API
└── report.py         CLI report (tabulate)

config/
├── config.example.yaml   Committed template
└── config.yaml           Your real config (gitignored)

secrets/
├── secrets.example.env   Committed template
└── secrets.env           Your real secrets (gitignored, chmod 600)

grafana/provisioning/     Drop-in dashboard JSON for any Grafana

tests/                    25 unit tests, no network needed
```

## Quick start

```bash
# 1. Bootstrap your real configs (gitignored)
cp config/config.example.yaml config/config.yaml
cp secrets/secrets.example.env secrets/secrets.env
chmod 600 secrets/secrets.env
cp .env.example .env
echo "PUID=$(id -u)" >> .env
echo "PGID=$(id -g)" >> .env

# 2. Edit config/config.yaml (controller URL, target AP, channel pool,
#    blacklist). Leave scan.enabled=false for now.

# 3. Edit secrets/secrets.env with your local UniFi admin user
#    (Network → Settings → Admins → "Restrict to local access only").

# 4. Boot it
docker compose up -d --build fjord-radar
docker compose logs -f fjord-radar
```

Open http://localhost:8080/ — the live dashboard.

## Enabling channel cycling

When you're ready for the program to actually move the radio:

1. Verify in the UniFi UI that you can log in as your *local-only* user
   and that your test AP is reachable by name.
2. Set `scan.enabled: true` in `config/config.yaml`.
3. Tune `widths`, `channels`, `blacklist_channels`, `dwell_hours`.
4. `docker compose restart fjord-radar`.
5. Watch http://localhost:8080/ — the "Current trial" badge should
   appear within a minute.

## Strategy: finding the widest stable channel

The planner generates **all valid combos** of (primary channel, width)
within your pool. The scheduler dwells on each one until either:

- `dwell_hours` elapse → mark `dwell_complete` and rotate (good signal).
- A `radar` event arrives → close trial, cooldown, rotate (bad signal).

Sorted by MTBD, the dashboard tells you:

- For 20 MHz: which sub-channels are statistically clean.
- For 40 MHz / 80 MHz: which **bonded combos** survive — these directly
  answer "where can I run wider?". A clean 80 MHz combo is also implicit
  evidence that all four of its 20 MHz primaries are clean, but the
  reverse is not guaranteed (interactions / leaky neighbours).

Recommended tuning for a long survey:

```yaml
scan:
  enabled: true
  widths: [20, 40, 80]
  dwell_hours: 24       # one day per trial; 168 (= 7 days) for tighter MTBD
  cooldown_after_radar_minutes: 30
  strategy: round_robin
```

Once `mtbd_hours` for a wider combo is reliably *higher than your
patience*, that's your answer.

## CLI report

```bash
docker compose exec fjord-radar python -m fjord_radar.report
docker compose exec fjord-radar python -m fjord_radar.report --source sessions
docker compose exec fjord-radar python -m fjord_radar.report --format csv
```

## Grafana

The built-in web page covers ~all common stats use-cases. If you have an
existing Grafana, see [docs/GRAFANA.md](docs/GRAFANA.md) for two ways to
hook it up (keep our SQLite, or export to your existing Postgres/InfluxDB).

The bundled `--profile viz` Grafana is convenient but optional and not
recommended if you already run Grafana — two of them on the same host
just costs RAM.

## Security

- **No secret ever lives in YAML or in `.env`.** They live only in
  `secrets/secrets.env` (gitignored, `chmod 600`) and reach the
  process as environment variables via docker compose's `env_file:`.
- The web page has **no write endpoints** and **no auth**. Bind it to
  `127.0.0.1:8080` (set `FJORD_WEB_PORT` and edit `bind_host` in
  `config.yaml` to `127.0.0.1`) if you don't want LAN access.
- The UniFi REST API uses self-signed TLS by default; we disable cert
  verification with `verify_tls: false`. Set it to `true` once you've
  installed your own cert on the UDR.
- The local UniFi user should be created with **"Restrict to local
  access only"** so the credentials cannot be used against the cloud
  identity.

## License

MIT.
