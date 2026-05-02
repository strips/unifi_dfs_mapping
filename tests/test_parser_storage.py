"""Self-contained smoke tests using stdlib unittest. Run with:

    python -m unittest tests.test_parser_storage
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone

from fjord_radar.parser import parse
from fjord_radar.storage import Storage


SAMPLE_LINES = [
    # hostapd radar detected on freq=5260 (chan 52 — but our living-room AP
    # is on 100 = 5500 MHz; here is one of those):
    "<30>Apr 30 12:34:56 AC-HD hostapd: wlan1: DFS-RADAR-DETECTED freq=5500 ht_enabled=1 chan_offset=0 chan_width=0 cf1=5500 cf2=0",
    # subsequent channel switch
    "<30>Apr 30 12:35:01 AC-HD hostapd: wlan1: DFS-NEW-CHANNEL freq=5660 chan_offset=0 chan_width=0",
    # CAC complete on the new channel
    "<30>Apr 30 12:36:01 AC-HD hostapd: wlan1: DFS-CAC-COMPLETED success=1 freq=5660 ht_enabled=1 chan_offset=0 chan_width=0 cf1=5660 cf2=0",
    # later, kernel-style radar message naming the channel directly
    "<30>Apr 30 14:00:00 AC-HD kernel: wlan1: radar detected on channel 132",
    # noise that should be ignored
    "<30>Apr 30 14:00:01 AC-HD kernel: link is up",
]


class ParserTests(unittest.TestCase):
    def test_radar_with_freq(self):
        ev = parse(SAMPLE_LINES[0])
        self.assertIsNotNone(ev)
        assert ev is not None
        self.assertEqual(ev.kind, "radar")
        self.assertEqual(ev.host, "AC-HD")
        self.assertEqual(ev.freq_mhz, 5500)
        self.assertEqual(ev.channel, 100)

    def test_new_channel(self):
        ev = parse(SAMPLE_LINES[1])
        assert ev is not None
        self.assertEqual(ev.kind, "new_channel")
        self.assertEqual(ev.channel, 132)  # 5660 MHz

    def test_cac_done(self):
        ev = parse(SAMPLE_LINES[2])
        assert ev is not None
        self.assertEqual(ev.kind, "cac_done")
        self.assertEqual(ev.channel, 132)

    def test_kernel_radar_named_channel(self):
        ev = parse(SAMPLE_LINES[3])
        assert ev is not None
        self.assertEqual(ev.kind, "radar")
        self.assertEqual(ev.channel, 132)

    def test_noise_ignored(self):
        self.assertIsNone(parse(SAMPLE_LINES[4]))


class StorageTests(unittest.TestCase):
    def test_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Storage(tmp)
            try:
                # Pretend AP starts on ch 100 via a CAC-done.
                ev1 = parse(
                    "<30>Apr 30 12:00:00 AC-HD hostapd: wlan1: DFS-CAC-COMPLETED success=1 freq=5500 chan_width=0"
                )
                assert ev1 is not None
                s.record(ev1)
                self.assertIsNotNone(s.open_session("AC-HD"))

                # Radar hit on 100 closes the session.
                ev2 = parse(
                    "<30>Apr 30 13:00:00 AC-HD hostapd: wlan1: DFS-RADAR-DETECTED freq=5500 chan_width=0"
                )
                assert ev2 is not None
                s.record(ev2)
                self.assertIsNone(s.open_session("AC-HD"))

                # Switch to 132 opens a new session.
                ev3 = parse(
                    "<30>Apr 30 13:01:00 AC-HD hostapd: wlan1: DFS-NEW-CHANNEL freq=5660 chan_width=0"
                )
                assert ev3 is not None
                s.record(ev3)
                row = s.open_session("AC-HD")
                self.assertIsNotNone(row)
                assert row is not None
                self.assertEqual(row[1], 132)
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
