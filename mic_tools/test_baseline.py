#!/usr/bin/env python3
"""
test_baseline.py — Unit tests for the AdaptiveBaseline class.

Tests:
  1. No z-score before warm-up
  2. Mean / std track N(2, 0.5) distribution after 5000 healthy frames
  3. Unhealthy frames leave the baseline unchanged
  4. Mean drifts toward a new regime after additional healthy frames
  5. reset() returns to pre-warm-up state
  6. state_dict / load_state_dict round-trip preserves all state exactly
"""

import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from adaptive_baseline import AdaptiveBaseline, WARMUP_N, EMA_ALPHA


class TestWarmup(unittest.TestCase):
    def test_no_zscore_before_warmup(self):
        ab = AdaptiveBaseline()
        for _ in range(WARMUP_N - 1):
            ab.update(3.0, True)
        self.assertEqual(ab.z_score(100.0), 0.0,
                         "z_score must be 0.0 until warm-up completes")

    def test_zscore_active_after_warmup(self):
        ab = AdaptiveBaseline()
        for _ in range(WARMUP_N):
            ab.update(3.0, True)
        self.assertNotEqual(ab.z_score(100.0), 0.0,
                            "z_score must be non-zero once warm-up is complete")

    def test_warmup_count(self):
        ab = AdaptiveBaseline()
        for i in range(WARMUP_N + 5):
            ab.update(3.0, True)
        self.assertEqual(ab.n_updates, WARMUP_N + 5)


class TestHealthyTracking(unittest.TestCase):
    """5 000 N(2, 0.5) healthy frames → mean ≈ 2.0, std ≈ 0.5."""

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(42)
        cls.ab = AdaptiveBaseline()
        for x in rng.normal(2.0, 0.5, 5000):
            cls.ab.update(float(x), True)

    def test_mean_close_to_2(self):
        self.assertAlmostEqual(self.ab.mean, 2.0, delta=0.05,
                               msg=f"mean={self.ab.mean:.4f} not within 0.05 of 2.0")

    def test_std_close_to_0_5(self):
        self.assertAlmostEqual(self.ab.std, 0.5, delta=0.05,
                               msg=f"std={self.ab.std:.4f} not within 0.05 of 0.5")

    def test_positive_zscore_for_high_value(self):
        self.assertGreater(self.ab.z_score(4.0), 3.0,
                           "value 2σ above mean should yield z > 3")

    def test_negative_zscore_for_low_value(self):
        self.assertLess(self.ab.z_score(0.0), -3.0,
                        "value well below mean should yield negative z")


class TestUnhealthyIgnored(unittest.TestCase):
    """100 N(10, 0.5) UNHEALTHY frames must not shift the mean."""

    def test_unhealthy_frames_do_not_change_mean(self):
        rng = np.random.default_rng(7)
        ab = AdaptiveBaseline()
        for x in rng.normal(2.0, 0.5, 5000):
            ab.update(float(x), True)
        mean_before = ab.mean
        for x in rng.normal(10.0, 0.5, 100):
            ab.update(float(x), False)   # is_healthy=False
        self.assertAlmostEqual(
            ab.mean, mean_before, places=9,
            msg="Unhealthy frames must not change the baseline mean")

    def test_unhealthy_frames_do_not_increment_n_updates(self):
        ab = AdaptiveBaseline()
        for _ in range(WARMUP_N):
            ab.update(3.0, True)
        n_before = ab.n_updates
        ab.update(100.0, False)
        self.assertEqual(ab.n_updates, n_before,
                         "n_updates must not increment on unhealthy frames")


class TestDriftTracking(unittest.TestCase):
    """Mean drifts toward a new healthy regime given enough EMA frames.

    After 5 000 N(2, 0.5) frames and then 1 000 N(3, 0.5) frames:
      Expected mean ≈ 2.0 · (1-α)^1000 + 3.0 · (1-(1-α)^1000)
                    ≈ 2.0 · 0.607       + 3.0 · 0.393  ≈  2.39
    """

    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(17)
        cls.ab = AdaptiveBaseline()
        for x in rng.normal(2.0, 0.5, 5000):
            cls.ab.update(float(x), True)
        cls.mean_after_phase1 = cls.ab.mean
        for x in rng.normal(3.0, 0.5, 1000):
            cls.ab.update(float(x), True)

    def test_mean_has_drifted_up(self):
        self.assertGreater(
            self.ab.mean, self.mean_after_phase1 + 0.15,
            f"Mean {self.ab.mean:.3f} should have drifted above "
            f"{self.mean_after_phase1:.3f} + 0.15")

    def test_mean_not_jumped_all_the_way(self):
        # With α=0.0005 and 1000 frames, convergence is slow by design
        self.assertLess(self.ab.mean, 2.8,
                        f"Mean {self.ab.mean:.3f} should not have reached 3.0 yet "
                        f"(half-life ≈ 1386 frames)")


class TestReset(unittest.TestCase):
    def test_reset_clears_all_state(self):
        ab = AdaptiveBaseline()
        rng = np.random.default_rng(99)
        for x in rng.normal(5.0, 0.3, 100):
            ab.update(float(x), True)
        ab.reset()
        self.assertEqual(ab.n_updates, 0)
        self.assertEqual(ab.z_score(100.0), 0.0,
                         "After reset z_score must return 0.0 (warm-up required again)")

    def test_reset_then_relearn(self):
        ab = AdaptiveBaseline()
        for _ in range(WARMUP_N):
            ab.update(3.0, True)
        ab.reset()
        for _ in range(WARMUP_N):
            ab.update(7.0, True)
        self.assertAlmostEqual(ab.mean, 7.0, delta=0.1,
                               msg="After reset, baseline should learn from new regime")


class TestPersistence(unittest.TestCase):
    def test_state_dict_round_trip(self):
        rng = np.random.default_rng(123)
        ab = AdaptiveBaseline()
        for x in rng.normal(3.5, 0.3, 500):
            ab.update(float(x), True)

        sd = ab.state_dict()
        ab2 = AdaptiveBaseline()
        ab2.load_state_dict(sd)

        self.assertAlmostEqual(ab.mean,      ab2.mean,      places=9)
        self.assertAlmostEqual(ab.std,       ab2.std,       places=9)
        self.assertEqual(ab.n_updates,   ab2.n_updates)
        self.assertAlmostEqual(ab.z_score(7.0), ab2.z_score(7.0), places=9)

    def test_state_dict_keys(self):
        ab = AdaptiveBaseline()
        sd = ab.state_dict()
        for key in ('alpha', 'warmup_n', 'w_n', 'w_mean', 'w_M2', 'mean', 'var', 'n_updates'):
            self.assertIn(key, sd, f"Missing key '{key}' in state_dict()")


class TestThresholds(unittest.TestCase):
    def test_warn_threshold_at_4_sigma(self):
        rng = np.random.default_rng(0)
        ab = AdaptiveBaseline()
        for x in rng.normal(3.0, 0.5, 5000):
            ab.update(float(x), True)
        self.assertAlmostEqual(ab.warn_threshold(4.0),
                               ab.mean + 4.0 * ab.std, places=9)

    def test_fault_threshold_above_warn(self):
        rng = np.random.default_rng(1)
        ab = AdaptiveBaseline()
        for x in rng.normal(3.0, 0.5, 5000):
            ab.update(float(x), True)
        self.assertGreater(ab.fault_threshold(6.0), ab.warn_threshold(4.0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
