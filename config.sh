#!/usr/bin/env bash
# Shared configuration for the fjord-radar LXC. Single-service repo --
# edit values here, not inline in the scripts.

# --- Network -----------------------------------------------------------------
BRIDGE="vmbr0"
GATEWAY="192.168.1.1"
CIDR="24"
DNS="192.168.1.1"
SEARCHDOMAIN="lan"

# --- Proxmox storage ---------------------------------------------------------
ROOTFS_STORAGE="vmpool"        # same redundant pool ha-stack/kasm use for
                                # rootfs -- see ../homelab/README.md for the
                                # full pool inventory.
TEMPLATE_STORAGE="local"
TEMPLATE="debian-13-standard_13.1-2_amd64.tar.zst"

# --- Persistence ---------------------------------------------------------------
# ZFS dataset backing /data inside the CT: real config.yaml, secrets.env, and
# the SQLite/CSV event history. Same vmpool (5-disk raidz2) ha-stack's
# vmpool/ha/* datasets live on.
ZFS_DATASET="vmpool/fjord-radar"
DATA_ROOT="/srv/fjord-radar"

# --- Container definition -----------------------------------------------------
# CTID continues the shared host-wide numbering used by ha-stack (120-125)
# and kasm (127) -- 126 is the next free slot.
declare -A CT_fjord_radar=( [ctid]=126 [host]=fjord-radar [ip]=192.168.1.26 [cores]=2 [mem]=1024 [swap]=512 [disk]=8 )
SERVICES=(fjord_radar)

# --- Misc --------------------------------------------------------------------
TIMEZONE="$(cat /etc/timezone 2>/dev/null || echo Europe/Oslo)"
# Locales generated in the CT: LANG stays English, LC_* categories forwarded
# by SSH clients (Norwegian) must also exist or perl/apt spew locale warnings.
LOCALE_LANG="en_US.UTF-8"
LOCALE_EXTRA="nb_NO.UTF-8"
