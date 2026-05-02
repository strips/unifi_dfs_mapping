"""Planner unit tests."""

from __future__ import annotations

import unittest

from fjord_radar.planner import Trial, build_trials, order


class PlannerTests(unittest.TestCase):
    def test_20mhz_respects_blacklist(self):
        trials = build_trials(
            channels=[36, 40, 100, 104, 124, 128],
            widths=[20],
            blacklist_channels=[124, 128],
        )
        self.assertEqual(
            sorted(t.channel for t in trials),
            [36, 40, 100, 104],
        )
        self.assertTrue(all(t.width_mhz == 20 for t in trials))

    def test_40mhz_requires_both_subchannels(self):
        # 100+104 is fine; 116+120 is broken because 120 is not in pool.
        trials = build_trials(
            channels=[100, 104, 108, 112, 116, 124],
            widths=[40],
        )
        self.assertEqual(
            sorted((t.channel, t.width_mhz) for t in trials),
            [(100, 40), (108, 40)],
        )

    def test_80mhz_requires_all_four_subchannels(self):
        # Group 100..112 complete; 116..128 missing 124 due to blacklist.
        trials = build_trials(
            channels=[100, 104, 108, 112, 116, 120, 124, 128],
            widths=[80],
            blacklist_channels=[124],
        )
        self.assertEqual(
            sorted((t.channel, t.width_mhz) for t in trials),
            [(100, 80)],
        )

    def test_blacklist_combo(self):
        trials = build_trials(
            channels=[36, 40, 44, 48],
            widths=[20, 80],
            blacklist_combos=[(36, 80)],
        )
        combos = sorted((t.channel, t.width_mhz) for t in trials)
        self.assertIn((36, 20), combos)
        self.assertIn((48, 20), combos)
        self.assertNotIn((36, 80), combos)

    def test_dedup_across_widths(self):
        # Asking for both 20 and 40 should give DISTINCT (channel, width)
        # rows, not duplicated 20s.
        trials = build_trials(
            channels=[36, 40],
            widths=[20, 40],
        )
        labels = sorted(t.label() for t in trials)
        self.assertEqual(labels, ["ch36@20MHz", "ch36@40MHz", "ch40@20MHz"])

    def test_empty_pool_yields_no_trials(self):
        self.assertEqual(build_trials(channels=[], widths=[20, 40, 80]), [])

    def test_invalid_width_raises(self):
        with self.assertRaises(ValueError):
            build_trials(channels=[36], widths=[27])

    def test_round_robin_preserves_order(self):
        trials = [Trial(36, 20), Trial(40, 20), Trial(100, 80)]
        self.assertEqual(order(trials, "round_robin"), trials)

    def test_shuffle_returns_permutation(self):
        trials = [Trial(c, 20) for c in (36, 40, 44, 48)]
        out = order(trials, "shuffle")
        self.assertEqual(sorted(out), sorted(trials))


if __name__ == "__main__":
    unittest.main()
