#!/usr/bin/env python3
"""
test_fusion.py — Unit tests for BayesianFusion multi-channel evidence combiner.

Mathematical reference (prior=0.01, z_mid=3.0, temperature=1.0):

  4 channels at z=0:
    L_each = sigmoid(-3) ≈ 0.047,  (1-L) ≈ 0.953
    posterior ≈ 0.01×0.047⁴ / (0.01×0.047⁴ + 0.99×0.953⁴) ≈ 6×10⁻⁸
    → near zero (much lower than prior — 4 healthy channels suppress fault prob.)

  4 channels at z=7:
    L_each = sigmoid(4) ≈ 0.982,  (1-L) ≈ 0.018
    posterior ≈ 0.01×0.982⁴ / (0.01×0.982⁴ + 0.99×0.018⁴) ≈ 0.9998
    → effectively certain fault

  4 channels at z=4.5:
    L_each = sigmoid(1.5) ≈ 0.818
    posterior ≈ 0.01×0.818⁴ / (0.01×0.818⁴ + 0.99×0.182⁴) ≈ 0.80
    → above P_FUSION_WARN (0.70) → WARN condition

  1 channel at z=10, 3 channels at z=0 (isolation property):
    L1 ≈ 1.0,  (1-L1) ≈ 9×10⁻⁴
    L_rest = 0.047,  (1-L_rest) = 0.953
    posterior ≈ 0.01×1.0×0.047³ / (0.01×1.0×0.047³ + 0.99×9×10⁻⁴×0.953³)
             ≈ 0.0012  (0.12%)
    → The three healthy channels provide strong contrary evidence, preventing
       a single transient from triggering FAULT. Temporal isolation is handled
       separately by WARN_PERSIST / CLEAR_PERSIST in recv_verify.py.

Run with:
    python -m pytest mic_tools/test_fusion.py -v
    # or:
    python mic_tools/test_fusion.py
"""

import math
import unittest

from bayesian_fusion import BayesianFusion


class TestBasicProperties(unittest.TestCase):

    def setUp(self):
        self.bf = BayesianFusion(prior=0.01, z_mid=3.0, temperature=1.0)

    def test_empty_channels_returns_prior(self):
        """No evidence → posterior = prior."""
        p = self.bf.fuse([])
        self.assertAlmostEqual(p, 0.01, places=6)

    def test_nan_channels_dropped(self):
        """NaN channels are silently omitted; one NaN = empty → prior."""
        p = self.bf.fuse([float('nan')])
        self.assertAlmostEqual(p, 0.01, places=6)

    def test_output_in_unit_interval(self):
        """Posterior must always be in [0, 1]."""
        for z in [-10, -3, 0, 3, 7, 15]:
            p = self.bf.fuse([float(z)])
            self.assertGreaterEqual(p, 0.0)
            self.assertLessEqual(p, 1.0)

    def test_channel_likelihood_at_z_mid(self):
        """At z_mid the logistic gives exactly 0.5."""
        L = self.bf.channel_likelihood(3.0)
        self.assertAlmostEqual(L, 0.5, places=6)

    def test_likelihood_monotone(self):
        """Likelihood increases with z-score."""
        prev = 0.0
        for z in [-5, -2, 0, 2, 5, 10]:
            L = self.bf.channel_likelihood(float(z))
            self.assertGreater(L, prev)
            prev = L

    def test_invalid_prior_raises(self):
        with self.assertRaises(ValueError):
            BayesianFusion(prior=0.0)
        with self.assertRaises(ValueError):
            BayesianFusion(prior=1.0)
        with self.assertRaises(ValueError):
            BayesianFusion(prior=-0.1)

    def test_invalid_temperature_raises(self):
        with self.assertRaises(ValueError):
            BayesianFusion(temperature=0.0)
        with self.assertRaises(ValueError):
            BayesianFusion(temperature=-1.0)


class TestHealthyFleet(unittest.TestCase):
    """4 channels all at z=0 → posterior near zero (much below prior)."""

    def test_all_channels_healthy(self):
        bf = BayesianFusion(prior=0.01, z_mid=3.0)
        p = bf.fuse([0.0, 0.0, 0.0, 0.0])
        # 4 healthy channels suppress fault prob far below prior
        self.assertLess(p, 0.001,
                        f"Expected p_fault << 0.001 for 4 healthy channels, got {p:.6f}")


class TestClearFault(unittest.TestCase):
    """4 channels all at z=7 → posterior > 0.99."""

    def test_all_channels_fault(self):
        bf = BayesianFusion(prior=0.01, z_mid=3.0)
        p = bf.fuse([7.0, 7.0, 7.0, 7.0])
        self.assertGreater(p, 0.99,
                           f"Expected p_fault > 0.99 for 4 channels at z=7, got {p:.6f}")


class TestWarnThreshold(unittest.TestCase):
    """4 channels at z=4.5 → posterior > 0.70 (P_FUSION_WARN)."""

    def test_moderate_multi_channel_triggers_warn(self):
        bf = BayesianFusion(prior=0.01, z_mid=3.0)
        p = bf.fuse([4.5, 4.5, 4.5, 4.5])
        self.assertGreater(p, 0.70,
                           f"Expected p_fault > 0.70 for 4 channels at z=4.5, got {p:.6f}")
        # But should still be below FAULT threshold
        self.assertLess(p, 0.95,
                        f"Expected p_fault < 0.95 (FAULT threshold) for z=4.5, got {p:.6f}")


class TestIsolationProperty(unittest.TestCase):
    """Single extreme channel should NOT dominate when others are healthy.

    With prior=0.01 and z_mid=3.0: 1 channel at z=10 with 3 channels at z=0
    gives posterior ≈ 0.12%, because the 3 healthy channels provide strong
    contrary evidence. This is the key advantage over max() fusion.
    """

    def test_single_spike_does_not_dominate(self):
        bf = BayesianFusion(prior=0.01, z_mid=3.0)
        p = bf.fuse([10.0, 0.0, 0.0, 0.0])
        self.assertLess(p, 0.05,
                        f"Single z=10 spike should not dominate (3 healthy channels). "
                        f"Got p_fault={p:.4f}, expected < 0.05")

    def test_two_spikes_still_moderate(self):
        """Two anomalous + two healthy channels → moderate posterior."""
        bf = BayesianFusion(prior=0.01, z_mid=3.0)
        p_one   = bf.fuse([6.0, 0.0, 0.0, 0.0])
        p_two   = bf.fuse([6.0, 6.0, 0.0, 0.0])
        p_three = bf.fuse([6.0, 6.0, 6.0, 0.0])
        # More corroborating channels → monotonically higher posterior
        self.assertLess(p_one, p_two)
        self.assertLess(p_two, p_three)


class TestPriorEffect(unittest.TestCase):
    """Higher prior → higher posterior for the same evidence."""

    def test_higher_prior_raises_posterior(self):
        z_list = [4.0, 4.0, 4.0]
        p_lo = BayesianFusion(prior=0.01).fuse(z_list)
        p_hi = BayesianFusion(prior=0.10).fuse(z_list)
        self.assertLess(p_lo, p_hi,
                        "Higher prior should give higher posterior for same evidence")

    def test_lower_z_mid_raises_sensitivity(self):
        """Lower z_mid means a lower z-score is considered strong evidence."""
        z_list = [2.0, 2.0, 2.0]
        p_mid3 = BayesianFusion(prior=0.01, z_mid=3.0).fuse(z_list)
        p_mid1 = BayesianFusion(prior=0.01, z_mid=1.0).fuse(z_list)
        self.assertLess(p_mid3, p_mid1,
                        "z_mid=1 should give higher posterior than z_mid=3 for z=2 channels")


class TestSymmetryAndMonotonicity(unittest.TestCase):

    def test_posterior_increases_with_more_anomalous_channels(self):
        bf = BayesianFusion(prior=0.01, z_mid=3.0)
        p1 = bf.fuse([5.0])
        p2 = bf.fuse([5.0, 5.0])
        p3 = bf.fuse([5.0, 5.0, 5.0])
        self.assertLess(p1, p2)
        self.assertLess(p2, p3)

    def test_channel_order_does_not_matter(self):
        """Fusion is commutative — channel order has no effect."""
        bf = BayesianFusion(prior=0.01, z_mid=3.0)
        p1 = bf.fuse([5.0, 2.0, 1.0])
        p2 = bf.fuse([1.0, 5.0, 2.0])
        p3 = bf.fuse([2.0, 1.0, 5.0])
        self.assertAlmostEqual(p1, p2, places=10)
        self.assertAlmostEqual(p2, p3, places=10)

    def test_mixed_nan_and_valid(self):
        """NaN channels are excluded; remaining channels computed normally."""
        bf = BayesianFusion(prior=0.01, z_mid=3.0)
        p_all  = bf.fuse([5.0, 5.0])
        p_nan  = bf.fuse([5.0, float('nan'), 5.0, float('nan')])
        self.assertAlmostEqual(p_all, p_nan, places=10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
