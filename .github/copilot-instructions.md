# unifi_dfs_mapping (fjord-radar) — Copilot instructions

Python app + LXC provisioning for **fjord-radar**: a long-running mapper of
UniFi DFS/radar events that finds the most stable 5 GHz channel via
Mean-Time-Between-Detections (MTBD). Read `README.md` and `docs/PROJECT.md`
for the methodology before touching planner/scheduler logic — MTBD vs
"pings per day" is a deliberate, documented design decision, not arbitrary.

## Architecture

- App lives in `fjord_radar/` — one process (`app.py`) running a UDP/514
  syslog listener, a trial scheduler driving the AP via the UniFi Network
  REST API, SQLite(WAL)+CSV storage, and a Flask stats page on TCP/8080.
- **Trials are the source of truth**: each `(channel, width)` interval is a
  `trials` row ended by `dwell_complete` or `radar`. The scheduler owns these
  rows; on restart it retries interrupted trials rather than advancing.
- Syslog arrives relayed from home-ops-ai's CT 128 `unifi` container
  (the UDR sends there; identical datagrams are forwarded here unchanged).

## Deployment (CT 126, single-service homelab repo)

- Like `../kasm`: **Docker-in-LXC** (privileged, `nesting=1`), provisioned by
  `containers/fjord-radar.sh` + `assets/fjord-radar-install.sh` with the
  shared `config.sh`/`lib.sh` idioms (`set -euo pipefail`, `require_root`,
  idempotent re-runs).
- **Deploy = re-run `bash containers/fjord-radar.sh`** — it tar-copies this
  repo's working tree into the CT (NOT a git pull; no push needed to deploy)
  and restarts compose. The CT rootfs is disposable.
- `/data` (ZFS `vmpool/fjord-radar`, host path `/srv/fjord-radar`) holds the
  real `config.yaml`, `secrets.env`, and SQLite/CSV state — survives
  destroy/recreate. The provision script copies (not symlinks) config/secrets
  from `/data` into the app tree each run; edit under `/data`, then re-run.
- Logging: Promtail ships this CT's Docker logs to the fleet Loki at
  `192.168.1.28:3100` — see `../homelab/README.md` "Centralized logging".

## Conventions

- Secrets ONLY from env (`secrets/secrets.env`, gitignored, chmod 600) —
  never in YAML, never in git. `config/config.yaml` is gitignored too;
  `*.example.*` templates are what's committed.
- Tests in `tests/` run with no network — keep it that way (mock the UniFi
  client boundary). Run them before finishing any change to `fjord_radar/`.
- Prefer small, surgical changes; the stats/statistics logic (MTBD, spectrum
  map cleanliness scoring) is documented in `docs/PROJECT.md` — update docs
  in the same session if you change behavior.

## Safety

- `containers/fjord-radar.sh` runs against production Proxmox. Anything
  destructive (`pct destroy`, `zfs destroy`, touching `/srv/fjord-radar`)
  needs explicit user confirmation first — per `../homelab/AGENTS.md` ground
  rules, which all apply here (no `git add`/`commit`/`push` without asking,
  secrets pre-commit hook active via `core.hooksPath .githooks`, re-run that
  config after fresh clone).
