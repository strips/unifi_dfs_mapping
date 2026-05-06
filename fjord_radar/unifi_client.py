"""Minimal UniFi Network controller client.

Targets UniFi OS consoles (UDR / UDM / UDM-Pro / UCG) where the Network
application is reached via the `/proxy/network/` reverse-proxy.

The controller's REST API is not officially documented for end users,
but it has been stable for years and is the basis for `pyunifi`,
`aiounifi`, the Home Assistant integration, etc.

Authentication:
    POST {url}/api/auth/login   {"username": ..., "password": ...}
    -> sets `TOKEN` cookie and returns an `X-CSRF-Token` header that
       must be echoed on subsequent unsafe requests.

Read devices:
    GET  {url}/proxy/network/api/s/{site}/stat/device

Update one device's radio_table (channel/width):
    PUT  {url}/proxy/network/api/s/{site}/rest/device/{_id}
         body = {"radio_table": [...modified entries...]}

The PUT only needs the entries you want to change; we send the full
radio_table back to be safe and to avoid the controller computing
deltas from a partial.

Designed to be small, dependency-light, and easy to mock.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests
from requests import Response, Session
from urllib3.exceptions import InsecureRequestWarning

log = logging.getLogger(__name__)


class UnifiError(RuntimeError):
    """Any failure talking to the controller."""


class UnifiAuthError(UnifiError):
    """Login failed or session expired and could not be renewed."""


class UnifiNotFoundError(UnifiError):
    """Requested device / site / resource not present."""


_VALID_HT = {20, 40, 80, 160}


@dataclass
class Device:
    id: str          # controller `_id`
    name: str
    mac: str
    model: str
    radio_table: list[dict[str, Any]]


class UnifiClient:
    """Thread-safe-enough client (single shared `Session` + a lock).

    All public methods raise `UnifiError` (or subclass) on failure.
    """

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        site: str = "default",
        verify_tls: bool = False,
        timeout: float = 10.0,
    ) -> None:
        if not url:
            raise ValueError("controller url is required")
        if not username or not password:
            raise ValueError("username and password are required")
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._site = site
        self._verify = verify_tls
        self._timeout = timeout

        self._lock = threading.RLock()
        self._session: Session = requests.Session()
        self._session.verify = self._verify
        self._csrf: Optional[str] = None
        self._logged_in = False

        if not verify_tls:
            requests.packages.urllib3.disable_warnings(  # type: ignore[attr-defined]
                InsecureRequestWarning
            )

    # -- auth --------------------------------------------------------------

    def login(self) -> None:
        with self._lock:
            r = self._session.post(
                f"{self._url}/api/auth/login",
                json={"username": self._username, "password": self._password},
                timeout=self._timeout,
            )
            if r.status_code in (401, 403):
                raise UnifiAuthError(
                    f"login rejected ({r.status_code}); check UNIFI_USERNAME"
                    f"/UNIFI_PASSWORD and that the user has local access"
                )
            self._raise_for_status(r, "login")
            csrf = r.headers.get("X-CSRF-Token") or r.headers.get("x-csrf-token")
            if csrf:
                self._csrf = csrf
            self._logged_in = True
            log.info("authenticated to %s", self._url)

    def logout(self) -> None:
        with self._lock:
            if not self._logged_in:
                return
            try:
                self._session.post(
                    f"{self._url}/api/auth/logout", timeout=self._timeout
                )
            except requests.RequestException:
                pass
            self._logged_in = False
            self._csrf = None

    def ensure_logged_in(self) -> None:
        """Log in only if not already authenticated."""
        with self._lock:
            if not self._logged_in:
                self.login()

    # -- low-level ---------------------------------------------------------

    def _request(
        self, method: str, path: str, *, json: Any = None, _retried: bool = False
    ) -> Response:
        with self._lock:
            if not self._logged_in:
                self.login()
            headers: dict[str, str] = {}
            if self._csrf and method.upper() != "GET":
                headers["X-CSRF-Token"] = self._csrf
            r = self._session.request(
                method,
                f"{self._url}{path}",
                json=json,
                headers=headers,
                timeout=self._timeout,
            )
            # Refresh CSRF on every response that carries one.
            new_csrf = r.headers.get("X-CSRF-Token") or r.headers.get("x-csrf-token")
            if new_csrf:
                self._csrf = new_csrf

            if r.status_code in (401, 403) and not _retried:
                log.info("session expired; re-authenticating")
                self._logged_in = False
                self.login()
                return self._request(method, path, json=json, _retried=True)
            return r

    @staticmethod
    def _raise_for_status(r: Response, what: str) -> None:
        if r.ok:
            return
        body = r.text[:500] if r.text else ""
        if r.status_code == 404:
            raise UnifiNotFoundError(f"{what} not found ({r.status_code}): {body}")
        raise UnifiError(f"{what} failed ({r.status_code}): {body}")

    # -- devices -----------------------------------------------------------

    def list_devices(self) -> list[Device]:
        r = self._request("GET", f"/proxy/network/api/s/{self._site}/stat/device")
        self._raise_for_status(r, "list devices")
        payload = r.json()
        out: list[Device] = []
        for d in payload.get("data", []):
            out.append(
                Device(
                    id=d["_id"],
                    name=d.get("name") or d.get("hostname") or d.get("mac", ""),
                    mac=d.get("mac", ""),
                    model=d.get("model", ""),
                    radio_table=list(d.get("radio_table", [])),
                )
            )
        return out

    def find_ap(self, name_or_mac: str) -> Device:
        needle = name_or_mac.strip().lower()
        for d in self.list_devices():
            if d.name.lower() == needle or d.mac.lower() == needle:
                return d
        raise UnifiNotFoundError(f"no AP named or MAC matching {name_or_mac!r}")

    def set_radio(
        self, device: Device, radio: str, channel: int, width_mhz: int
    ) -> Device:
        """Apply (channel, width) to one radio of one AP. Returns the
        re-fetched device."""
        if width_mhz not in _VALID_HT:
            raise ValueError(f"invalid width {width_mhz}; must be one of {_VALID_HT}")
        if channel < 1 or channel > 233:
            raise ValueError(f"invalid channel {channel}")

        new_table: list[dict[str, Any]] = []
        found = False
        for entry in device.radio_table:
            entry = dict(entry)
            if entry.get("radio") == radio:
                entry["channel"] = str(channel)
                entry["ht"] = str(width_mhz)
                # Some firmwares look at channel_width instead of `ht`.
                entry["channel_width"] = width_mhz
                # Force manual selection (overrides "auto").
                if "tx_power_mode" not in entry:
                    entry["tx_power_mode"] = "auto"
                found = True
            new_table.append(entry)
        if not found:
            raise UnifiError(
                f"AP {device.name} has no radio {radio!r} "
                f"(found: {[e.get('radio') for e in device.radio_table]})"
            )

        log.info(
            "applying ch=%s width=%s to %s/%s",
            channel, width_mhz, device.name, radio,
        )
        r = self._request(
            "PUT",
            f"/proxy/network/api/s/{self._site}/rest/device/{device.id}",
            json={"radio_table": new_table},
        )
        self._raise_for_status(r, "set_radio")

        # The controller returns the updated record; re-fetch to be safe
        # (some firmwares return only meta).
        time.sleep(0.5)
        return self.find_ap(device.name)

    def get_country_code(self) -> int:
        """Return the ISO 3166-1 numeric country code stored in the UniFi
        site settings.  Raises ``UnifiError`` if the setting is absent or
        the response is malformed."""
        r = self._request(
            "GET",
            f"/proxy/network/api/s/{self._site}/rest/setting",
        )
        self._raise_for_status(r, "get site settings")
        for item in r.json().get("data", []):
            if item.get("key") == "country":
                code = item.get("code")
                if code is not None:
                    return int(code)
        raise UnifiError("country code not found in site settings")
