"""UniFi client tests using a mocked Session."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from fjord_radar.unifi_client import (
    Device,
    UnifiAuthError,
    UnifiClient,
    UnifiError,
    UnifiNotFoundError,
)


def _resp(status: int = 200, json_data: Any = None, headers: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 400
    r.headers = headers or {}
    r.json = MagicMock(return_value=json_data or {})
    r.text = ""
    return r


def _make_client() -> tuple[UnifiClient, MagicMock]:
    c = UnifiClient(
        url="https://1.2.3.4",
        username="u",
        password="p",
        site="default",
        verify_tls=False,
    )
    sess = MagicMock()
    c._session = sess  # noqa: SLF001
    return c, sess


class AuthTests(unittest.TestCase):
    def test_login_sets_csrf(self):
        c, sess = _make_client()
        sess.post.return_value = _resp(
            200, headers={"X-CSRF-Token": "abc"}
        )
        c.login()
        self.assertTrue(c._logged_in)  # noqa: SLF001
        self.assertEqual(c._csrf, "abc")  # noqa: SLF001

    def test_login_401_raises_auth_error(self):
        c, sess = _make_client()
        sess.post.return_value = _resp(401)
        with self.assertRaises(UnifiAuthError):
            c.login()


class DeviceTests(unittest.TestCase):
    def test_list_devices_parses_payload(self):
        c, sess = _make_client()
        sess.post.return_value = _resp(200)
        sess.request.return_value = _resp(
            200,
            json_data={
                "data": [
                    {
                        "_id": "abc",
                        "name": "AC-HD",
                        "mac": "aa:bb:cc:dd:ee:ff",
                        "model": "U7HD",
                        "radio_table": [
                            {"radio": "ng", "channel": "6", "ht": "20"},
                            {"radio": "na", "channel": "100", "ht": "20"},
                        ],
                    }
                ]
            },
        )
        devs = c.list_devices()
        self.assertEqual(len(devs), 1)
        self.assertEqual(devs[0].name, "AC-HD")
        self.assertEqual(len(devs[0].radio_table), 2)

    def test_find_ap_404(self):
        c, sess = _make_client()
        sess.post.return_value = _resp(200)
        sess.request.return_value = _resp(200, json_data={"data": []})
        with self.assertRaises(UnifiNotFoundError):
            c.find_ap("nope")

    def test_set_radio_updates_target_radio_only(self):
        c, sess = _make_client()
        sess.post.return_value = _resp(200)

        device = Device(
            id="abc", name="AC-HD", mac="aa:bb", model="x",
            radio_table=[
                {"radio": "ng", "channel": "6", "ht": "20"},
                {"radio": "na", "channel": "36", "ht": "20"},
            ],
        )

        # Two GETs come from the post-PUT re-fetch; one PUT in between.
        refreshed_payload = {
            "data": [
                {
                    "_id": "abc",
                    "name": "AC-HD",
                    "mac": "aa:bb",
                    "model": "x",
                    "radio_table": [
                        {"radio": "ng", "channel": "6", "ht": "20"},
                        {"radio": "na", "channel": "100", "ht": "80"},
                    ],
                }
            ]
        }
        sess.request.side_effect = [
            _resp(200),                                 # PUT
            _resp(200, json_data=refreshed_payload),    # GET refresh
        ]

        out = c.set_radio(device, "na", channel=100, width_mhz=80)
        # The PUT body should have channel/ht updated for `na` only.
        put_call = sess.request.call_args_list[0]
        body = put_call.kwargs["json"]
        ng = next(e for e in body["radio_table"] if e["radio"] == "ng")
        na = next(e for e in body["radio_table"] if e["radio"] == "na")
        self.assertEqual(ng["channel"], "6")          # untouched
        self.assertEqual(na["channel"], "100")
        self.assertEqual(na["ht"], "80")
        self.assertEqual(out.name, "AC-HD")

    def test_set_radio_rejects_bad_width(self):
        c, _ = _make_client()
        device = Device(
            id="x", name="x", mac="x", model="x",
            radio_table=[{"radio": "na"}],
        )
        with self.assertRaises(ValueError):
            c.set_radio(device, "na", channel=100, width_mhz=33)

    def test_set_radio_missing_radio_raises(self):
        c, sess = _make_client()
        sess.post.return_value = _resp(200)
        device = Device(
            id="x", name="x", mac="x", model="x",
            radio_table=[{"radio": "ng"}],
        )
        with self.assertRaises(UnifiError):
            c.set_radio(device, "na", channel=100, width_mhz=20)


if __name__ == "__main__":
    unittest.main()
