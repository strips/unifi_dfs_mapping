"""Configuration loading.

* Non-secret runtime config lives in `config/config.yaml` (a YAML file,
  templated by `config/config.example.yaml`).
* Secrets live in environment variables, populated either by docker
  compose's `env_file: secrets/secrets.env` or by the host environment.

Nothing in this module ever logs or persists a secret value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ControllerConfig:
    url: str
    site: str = "default"
    verify_tls: bool = False
    username: str = ""  # filled from env, never YAML
    password: str = ""  # filled from env, never YAML

    def redacted(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "site": self.site,
            "verify_tls": self.verify_tls,
            "username_set": bool(self.username),
            "password_set": bool(self.password),
        }


@dataclass(frozen=True)
class TargetConfig:
    ap_name: str
    radio: str = "na"  # "na" 5GHz | "ng" 2.4GHz | "6e" 6GHz


@dataclass(frozen=True)
class ScanConfig:
    enabled: bool = False
    widths: tuple[int, ...] = (20,)
    channels: tuple[int, ...] = ()
    blacklist_channels: tuple[int, ...] = ()
    blacklist_combos: tuple[tuple[int, int], ...] = ()
    dwell_hours: float = 24.0
    cooldown_after_radar_minutes: float = 30.0
    strategy: str = "round_robin"  # round_robin | shuffle
    poll_seconds: int = 60          # how often the scheduler checks for radar


@dataclass(frozen=True)
class WebConfig:
    enabled: bool = True
    bind_host: str = "0.0.0.0"
    bind_port: int = 8080


@dataclass(frozen=True)
class SyslogConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 5514


@dataclass(frozen=True)
class RegionConfig:
    # ISO 3166-1 numeric country code.  578 = Norway (EU/ETSI rules).
    # Used to grey out channels that are not legal in the local regulatory
    # domain on the channel spectrum map.
    country_code: int = 578
    # When true, fjord-radar will try to read the country code from the
    # UniFi controller at startup and use that instead of country_code.
    auto_detect: bool = True


@dataclass(frozen=True)
class AppConfig:
    controller: ControllerConfig
    target: TargetConfig
    scan: ScanConfig
    web: WebConfig
    syslog: SyslogConfig
    region: RegionConfig
    data_dir: str
    log_level: str = "INFO"


def _t(seq: Any) -> tuple:
    if seq is None:
        return ()
    if isinstance(seq, (list, tuple)):
        return tuple(seq)
    raise ValueError(f"expected list, got {type(seq).__name__}")


def load(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load YAML config and merge env-var secrets. Fails loudly on missing
    required values."""

    cfg_path = Path(path or os.environ.get("FJORD_CONFIG", "config/config.yaml"))
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"config file not found: {cfg_path}. "
            f"Copy config/config.example.yaml to {cfg_path} and edit."
        )
    with cfg_path.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    ctrl_raw = raw.get("controller") or {}
    # Secrets ONLY from env. We never accept them from YAML to avoid
    # accidental commits.
    for forbidden in ("username", "password"):
        if forbidden in ctrl_raw:
            raise ValueError(
                f"controller.{forbidden} must not be set in YAML — "
                f"use the UNIFI_{forbidden.upper()} environment variable"
            )

    controller = ControllerConfig(
        url=ctrl_raw.get("url", "").rstrip("/"),
        site=ctrl_raw.get("site", "default"),
        verify_tls=bool(ctrl_raw.get("verify_tls", False)),
        username=os.environ.get("UNIFI_USERNAME", ""),
        password=os.environ.get("UNIFI_PASSWORD", ""),
    )

    tgt_raw = raw.get("target") or {}
    target = TargetConfig(
        ap_name=tgt_raw.get("ap_name", ""),
        radio=tgt_raw.get("radio", "na"),
    )

    scan_raw = raw.get("scan") or {}
    combos_raw = scan_raw.get("blacklist_combos") or []
    combos = tuple(
        (int(c["channel"]), int(c["width"])) for c in combos_raw
    )
    scan = ScanConfig(
        enabled=bool(scan_raw.get("enabled", False)),
        widths=tuple(int(w) for w in _t(scan_raw.get("widths", [20]))),
        channels=tuple(int(c) for c in _t(scan_raw.get("channels", []))),
        blacklist_channels=tuple(
            int(c) for c in _t(scan_raw.get("blacklist_channels", []))
        ),
        blacklist_combos=combos,
        dwell_hours=float(scan_raw.get("dwell_hours", 24)),
        cooldown_after_radar_minutes=float(
            scan_raw.get("cooldown_after_radar_minutes", 30)
        ),
        strategy=str(scan_raw.get("strategy", "round_robin")),
        poll_seconds=int(scan_raw.get("poll_seconds", 60)),
    )

    web_raw = raw.get("web") or {}
    web = WebConfig(
        enabled=bool(web_raw.get("enabled", True)),
        bind_host=web_raw.get("bind_host", "0.0.0.0"),
        bind_port=int(web_raw.get("bind_port", 8080)),
    )

    sys_raw = raw.get("syslog") or {}
    syslog = SyslogConfig(
        bind_host=sys_raw.get("bind_host", "0.0.0.0"),
        bind_port=int(sys_raw.get("bind_port", 5514)),
    )

    reg_raw = raw.get("region") or {}
    region = RegionConfig(
        country_code=int(reg_raw.get("country_code", 578)),
        auto_detect=bool(reg_raw.get("auto_detect", True)),
    )

    data_dir = os.environ.get("FJORD_DATA_DIR", raw.get("data_dir", "./data"))
    log_level = os.environ.get(
        "FJORD_LOG_LEVEL", raw.get("log_level", "INFO")
    ).upper()

    if scan.enabled:
        if not controller.url:
            raise ValueError("scan.enabled=true but controller.url is empty")
        if not controller.username or not controller.password:
            raise ValueError(
                "scan.enabled=true but UNIFI_USERNAME/UNIFI_PASSWORD env "
                "vars are not set (see secrets/secrets.example.env)"
            )
        if not target.ap_name:
            raise ValueError("scan.enabled=true but target.ap_name is empty")

    return AppConfig(
        controller=controller,
        target=target,
        scan=scan,
        web=web,
        syslog=syslog,
        region=region,
        data_dir=data_dir,
        log_level=log_level,
    )
