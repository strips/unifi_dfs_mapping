"""Storage tests for the trials table + thread safety basics."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest

from fjord_radar.parser import parse
from fjord_radar.storage import Storage


class TrialsTests(unittest.TestCase):
    def test_start_and_end_trial(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Storage(tmp)
            try:
                tid = s.start_trial("AC-HD", channel=100, width_mhz=80)
                self.assertGreater(tid, 0)

                row = s.open_trial("AC-HD")
                self.assertIsNotNone(row)
                self.assertEqual(row["channel"], 100)

                s.end_trial(tid, "dwell_complete", radar_count=0)
                self.assertIsNone(s.open_trial("AC-HD"))

                stats = s.trial_stats()
                self.assertEqual(len(stats), 1)
                self.assertEqual(stats[0]["channel"], 100)
                self.assertEqual(stats[0]["clean_trials"], 1)
            finally:
                s.close()

    def test_radar_during_trial_increments_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Storage(tmp)
            try:
                tid = s.start_trial("AC-HD", channel=100, width_mhz=20)
                # Simulate a radar event after the trial started.
                ev = parse(
                    "<30>Apr 30 12:34:56 AC-HD hostapd: wlan1: "
                    "DFS-RADAR-DETECTED freq=5500 chan_width=0"
                )
                assert ev is not None
                s.record(ev)
                # `count_radar_since('1970...')` should see the event.
                self.assertGreaterEqual(
                    s.count_radar_since("1970-01-01T00:00:00+00:00"), 1
                )
                s.end_trial(tid, "radar", radar_count=1)
                stats = s.trial_stats()
                self.assertEqual(stats[0]["detections"], 1)
                self.assertIsNotNone(stats[0]["mtbd_hours"])
            finally:
                s.close()

    def test_concurrent_writes(self):
        """Writers from multiple threads must not corrupt the DB."""
        with tempfile.TemporaryDirectory() as tmp:
            s = Storage(tmp)
            errors: list[Exception] = []

            def worker(n: int) -> None:
                try:
                    for i in range(20):
                        tid = s.start_trial(f"ap{n}", channel=36, width_mhz=20)
                        s.end_trial(tid, "dwell_complete")
                except Exception as e:  # pragma: no cover
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(s.trial_stats()), 4)
            s.close()


if __name__ == "__main__":
    unittest.main()
