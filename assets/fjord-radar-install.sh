#!/usr/bin/env bash
# In-container provisioner: Docker + docker compose up for fjord-radar.
# Idempotent: Docker install is skipped if already present; compose up
# rebuilds/recreates only what changed.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# --- Docker (Docker-in-LXC: this container was created privileged with
# nesting=1,keyctl=1 specifically so this works) --------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "[+] Installing Docker"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
systemctl enable --now docker

# Sanity-check overlay2 works on this ZFS-backed rootfs; fall back to vfs
# (slower, more disk, but always works) if the kernel/zfs combo lacks d_type.
if ! docker info 2>/dev/null | grep -q 'Storage Driver: overlay2'; then
  if docker info 2>/dev/null | grep -qi 'd_type.*false\|not supported'; then
    echo "[!] overlay2 unsupported on this rootfs -- falling back to vfs storage driver"
    mkdir -p /etc/docker
    echo '{"storage-driver": "vfs"}' > /etc/docker/daemon.json
    systemctl restart docker
  fi
fi

# --- fjord-radar ------------------------------------------------------------------
cd /opt/fjord-radar/app
echo "[+] Building and starting fjord-radar"
docker compose up -d --build fjord-radar

echo "[+] fjord-radar web UI: http://<this-container-ip>:8070/"
