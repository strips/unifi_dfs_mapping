#!/usr/bin/env bash
# Shared helper functions for the fjord-radar repo (single privileged LXC
# running Docker, ZFS-backed persistence at /data -- same contract as
# ha-stack's per-service /srv/ha/<svc>, adapted for this single-service repo).
# shellcheck disable=SC2155
set -euo pipefail
FJORD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${FJORD_DIR}/config.sh"

# --- logging -----------------------------------------------------------------
log()  { echo -e "\033[1;32m[+]\033[0m $*"; }
info() { echo -e "\033[1;34m[i]\033[0m $*"; }
warn() { echo -e "\033[1;33m[!]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[x]\033[0m $*" >&2; exit 1; }

# Read a field from the CT_<svc> associative array. Usage: ctf <svc> <key>
ctf() { local -n _m="CT_$1"; echo "${_m[$2]}"; }

require_root() { [[ $EUID -eq 0 ]] || die "Run as root."; }

ensure_template() {
  local ref="${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}"
  if ! pveam list "${TEMPLATE_STORAGE}" 2>/dev/null | grep -q "${TEMPLATE}"; then
    log "Downloading template ${TEMPLATE} ..."
    pveam download "${TEMPLATE_STORAGE}" "${TEMPLATE}"
  fi
  echo "${ref}"
}

ensure_dataset() {
  zfs list "${ZFS_DATASET}" >/dev/null 2>&1 || zfs create "${ZFS_DATASET}"
  [[ "$(zfs get -H -o value mountpoint "${ZFS_DATASET}")" == "${DATA_ROOT}" ]] || \
    zfs set "mountpoint=${DATA_ROOT}" "${ZFS_DATASET}"
}

ct_exists() { pct status "$1" >/dev/null 2>&1; }

# create_ct <svc>: privileged LXC, nesting+keyctl enabled so Docker's
# cgroup/overlay machinery works inside (same deliberate deviation from
# ha-stack's Docker-free convention that the kasm repo uses). Idempotent:
# leaves an existing CT in place, only ensures it's running.
create_ct() {
  local svc="$1"
  local ctid host ip cores mem swap disk
  ctid="$(ctf "$svc" ctid)"; host="$(ctf "$svc" host)"; ip="$(ctf "$svc" ip)"
  cores="$(ctf "$svc" cores)"; mem="$(ctf "$svc" mem)"; swap="$(ctf "$svc" swap)"
  disk="$(ctf "$svc" disk)"
  ensure_dataset

  if ct_exists "$ctid"; then
    info "CT $ctid ($host) already exists - ensuring running."
    [[ "$(pct status "$ctid")" == *running* ]] || pct start "$ctid"
    return 0
  fi

  local tmpl; tmpl="$(ensure_template)"
  local net="name=eth0,bridge=${BRIDGE},ip=${ip}/${CIDR},gw=${GATEWAY}"

  log "Creating CT $ctid ($host) ${ip} ..."
  pct create "$ctid" "$tmpl" \
    --hostname "$host" \
    --cores "$cores" --memory "$mem" --swap "$swap" \
    --rootfs "${ROOTFS_STORAGE}:${disk}" \
    --net0 "$net" \
    --nameserver "$DNS" --searchdomain "$SEARCHDOMAIN" \
    --onboot 1 --start 0 --unprivileged 0 \
    --features "nesting=1,keyctl=1" \
    --ostype debian

  # Bind-mount the persistence dataset at /data (config.yaml, secrets.env,
  # SQLite/CSV event history all live under here -- see README).
  pct set "$ctid" -mp0 "${DATA_ROOT},mp=/data"
  pct start "$ctid"
  wait_for_ct "$ctid"
  ct_base_setup "$ctid"
}

destroy_ct() {
  local svc="$1" ctid; ctid="$(ctf "$svc" ctid)"
  if ct_exists "$ctid"; then
    warn "Destroying CT $ctid ($svc) - persistence dataset ${DATA_ROOT} is preserved."
    pct stop "$ctid" >/dev/null 2>&1 || true
    pct destroy "$ctid"
  fi
}

# Wait until the container has booted and DNS/network is usable.
wait_for_ct() {
  local ctid="$1" i
  for i in $(seq 1 30); do
    if pct exec "$ctid" -- test -e /etc/resolv.conf 2>/dev/null && \
       pct exec "$ctid" -- getent hosts deb.debian.org >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  warn "CT $ctid network not confirmed after timeout; continuing."
}

ctx() { local ctid="$1"; shift; pct exec "$ctid" -- bash -lc "$*"; }
push() { pct push "$1" "$2" "$3" --perms "${4:-0644}"; }

# Push a local directory tree into a CT via a tar pipe (pct push only
# handles single files). Usage: push_dir <ctid> <local-dir> <remote-dir>
push_dir() {
  local ctid="$1" local_dir="$2" remote_dir="$3"
  ctx "$ctid" "mkdir -p '${remote_dir}'"
  tar -C "$local_dir" -cf - . | pct exec "$ctid" -- tar -C "$remote_dir" -xf -
}

# Common base packages + locale + timezone for the container.
ct_base_setup() {
  local ctid="$1"
  ctx "$ctid" "export DEBIAN_FRONTEND=noninteractive; \
    apt-get update -qq && \
    apt-get install -y -qq curl ca-certificates gnupg sudo jq tzdata locales >/dev/null && \
    ln -sf /usr/share/zoneinfo/${TIMEZONE} /etc/localtime && echo ${TIMEZONE} > /etc/timezone && \
    sed -i -E 's/^# *(${LOCALE_LANG}|${LOCALE_EXTRA}) UTF-8/\1 UTF-8/' /etc/locale.gen && \
    locale-gen >/dev/null && update-locale LANG=${LOCALE_LANG}"
}

# Run an installer asset inside a CT with optional KEY=VAL env.
# Usage: run_install <ctid> <asset-file> [KEY=VAL ...]
run_install() {
  local ctid="$1" asset="$2"; shift 2
  push "$ctid" "$asset" "/root/install.sh" 0755
  local env_prefix=""
  local kv; for kv in "$@"; do env_prefix+="export $(printf '%q' "${kv%%=*}")=$(printf '%q' "${kv#*=}"); "; done
  ctx "$ctid" "${env_prefix} bash /root/install.sh"
}
