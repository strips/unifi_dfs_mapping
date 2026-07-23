#!/usr/bin/env bash
# Provision the fjord-radar container: Docker + the app itself, copied in
# from this repo's own source (not cloned from GitHub) and pointed at the
# persistent /data dataset for config/secrets/state. Idempotent: safe to
# re-run after pulling repo changes or after a CT recreate.
# See README for the Docker-in-LXC rationale (mirrors the ../kasm repo).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${DIR}/lib.sh"; require_root

create_ct fjord_radar
CTID="$(ctf fjord_radar ctid)"

# Persistent subdirs (all under the ZFS-backed DATA_ROOT, i.e. /data inside
# the CT): real config.yaml, secrets.env, and the SQLite/CSV event state.
mkdir -p "${DATA_ROOT}"/{config,secrets,state}

# App source lives on the CT's disposable rootfs -- re-copied from this repo
# every run, so a `pct destroy` + recreate just needs this script re-run.
log "Copying app source into CT ${CTID} ..."
ctx "$CTID" "mkdir -p /opt/fjord-radar/app"
tar -C "$DIR" \
  --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
  --exclude='.pytest_cache' --exclude='.vscode' --exclude='data' \
  --exclude='secrets/secrets.env' --exclude='config/config.yaml' \
  -cf - . | pct exec "$CTID" -- tar -C /opt/fjord-radar/app -xf -

# Copy the real config/secrets in from the persistent dataset. Not a
# symlink: compose bind-mounts ./config and secrets/secrets.env straight
# into the Docker container's own mount namespace, which can't follow a
# symlink pointing outside that tree (it would resolve to a missing path
# inside the container). /data is the source of truth; this copy re-runs
# every time this script does, so edits under /data still take effect on
# the next `containers/fjord-radar.sh` run (or manually docker compose
# restart after editing the /data copy directly).
if [[ -f "${DATA_ROOT}/config/config.yaml" ]]; then
  ctx "$CTID" "cp -f /data/config/config.yaml /opt/fjord-radar/app/config/config.yaml"
fi
if [[ -f "${DATA_ROOT}/secrets/secrets.env" ]]; then
  ctx "$CTID" "cp -f /data/secrets/secrets.env /opt/fjord-radar/app/secrets/secrets.env && chmod 600 /opt/fjord-radar/app/secrets/secrets.env"
fi

# Non-secret compose env (mirrors nexus's own .env -- see docs/SETUP.md).
ctx "$CTID" "cat > /opt/fjord-radar/app/.env <<'ENVEOF'
PUID=1000
PGID=1000
FJORD_HOST_PORT=514
FJORD_WEB_PORT=8070
FJORD_DATA_DIR=/data/state
FJORD_LOG_LEVEL=INFO
ENVEOF"

run_install "$CTID" "${DIR}/assets/fjord-radar-install.sh"

log "fjord-radar done -> http://$(ctf fjord_radar ip):8070/"
