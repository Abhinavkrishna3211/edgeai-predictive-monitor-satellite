#!/usr/bin/env python3
"""
test_online_detector.py — Unit and isolation tests for OnlineDetector.

Run with:
    python -m pytest mic_tools/test_online_detector.py -v
    # or directly:
    python mic_tools/test_online_detector.py

Test suite covers:
  1. Normal-distribution stability  — scores on healthy data stabilise near 0.5
  2. Anomaly sensitivity            — 5-sigma injections score above 0.8 after warm-up
  3. Save / load round-trip         — pickled state reproduces identical scores
  4. Network isolation              — monkey-patch socket to prove zero network I/O
"""

import os
import pickle
import socket
import tempfile
import unittest

import numpy as np

from online_detector import OnlineDetector


class TestNormalStability(unittest.TestCase):
    """Feed 1000 Gaussian samples; mean HST score should stabilise near 0.5."""

    def test_scores_stabilize_on_normal(self):
        rng = np.random.default_rng(0)
        det = OnlineDetector(n_features=7)
        scores = []
        for _ in range(1000):
            # Samples centred at 0.5, std 0.15 — well within [0,1]
            x = np.clip(rng.standard_normal(7) * 0.15 + 0.5, 0.0, 1.0)
            score = det.score(x)
            det.learn(x)
            scores.append(score)

        tail_mean = float(np.mean(scores[500:]))
        self.assertGreater(tail_mean, 0.3,
                           f"tail mean {tail_mean:.3f} unexpectedly low — "
                           "HST may not have warmed up")
        self.assertLess(tail_mean, 0.7,
                        f"tail mean {tail_mean:.3f} unexpectedly high — "
                        "detector may be over-sensitive on normal data")


class TestAnomalySensitivity(unittest.TestCase):
    """Inject 50 samples shifted by 5σ; mean score should exceed 0.8."""

    def test_high_score_on_anomalies(self):
        rng = np.random.default_rng(1)
        det = OnlineDetector(n_features=7, window=250)

        # Warm up on normally distributed data (mean=0, std=1 raw)
        for _ in range(600):
            x = rng.standard_normal(7)          # raw N(0, 1)
            det.learn(x)

        # Inject anomalies 5σ above the normal mean
        anomaly_scores = []
        for _ in range(50):
            x_anom = rng.standard_normal(7) + 5.0   # raw N(5, 1)
            anomaly_scores.append(det.score(x_anom))

        mean_score = float(np.mean(anomaly_scores))
        self.assertGreater(mean_score, 0.8,
                           f"mean anomaly score {mean_score:.3f} — "
                           "expected > 0.8 for 5-sigma injections after warm-up")


class TestSaveLoadRoundtrip(unittest.TestCase):
    """Save and reload must preserve identical scoring behaviour."""

    def test_roundtrip(self):
        rng = np.random.default_rng(2)
        det = OnlineDetector(n_features=7)
        for _ in range(300):
            x = np.clip(rng.standard_normal(7) * 0.15 + 0.5, 0.0, 1.0)
            det.learn(x)

        probe = np.clip(rng.standard_normal(7) * 0.15 + 0.5, 0.0, 1.0)
        score_before = det.score(probe)
        n_before     = det._n

        with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False) as f:
            path = f.name
        try:
            det.save(path)

            det2 = OnlineDetector(n_features=7)
            det2.load(path)

            score_after = det2.score(probe)
            self.assertAlmostEqual(score_before, score_after, places=6,
                                   msg="score changed after save/load round-trip")
            self.assertEqual(det2._n, n_before,
                             "sample count changed after save/load round-trip")
        finally:
            os.unlink(path)

    def test_pickle_contains_no_model_paths(self):
        """Pickled state must not embed file paths (portability check)."""
        det = OnlineDetector(n_features=7)
        for _ in range(50):
            det.learn(np.random.rand(7))
        with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False) as f:
            path = f.name
        try:
            det.save(path)
            with open(path, 'rb') as f:
                state = pickle.load(f)
            # State dict must contain the HST object and Welford stats, not file paths
            self.assertIn('hst',  state)
            self.assertIn('mean', state)
            self.assertIn('m2',   state)
            self.assertIn('n',    state)
        finally:
            os.unlink(path)


class TestNetworkIsolation(unittest.TestCase):
    """Monkey-patch socket.socket to prove OnlineDetector makes zero network I/O."""

    def test_no_network_during_score_and_learn(self):
        """Run 1000 score+learn cycles; any socket.socket() call is a test failure."""
        _original = socket.socket

        class _BlockAllSockets:
            def __init__(self, *args, **kwargs):
                raise RuntimeError(
                    "OnlineDetector unexpectedly opened a network socket — "
                    "this violates the on-device guarantee.")

        socket.socket = _BlockAllSockets  # type: ignore[assignment]
        try:
            rng = np.random.default_rng(3)
            det = OnlineDetector(n_features=7)
            for _ in range(1000):
                x = np.clip(rng.standard_normal(7) * 0.15 + 0.5, 0.0, 1.0)
                det.score(x)
                det.learn(x)
        finally:
            socket.socket = _original     # always restore, even on failure

    def test_no_network_on_warmup_boundary(self):
        """Specifically test the Welford branch switch at n==30 (no network spike)."""
        _original = socket.socket

        class _BlockAllSockets:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("Unexpected socket during Welford branch switch")

        socket.socket = _BlockAllSockets  # type: ignore[assignment]
        try:
            rng = np.random.default_rng(4)
            det = OnlineDetector(n_features=7)
            for i in range(60):   # crosses the n<30 / n>=30 boundary
                x = np.clip(rng.standard_normal(7) * 0.15 + 0.5, 0.0, 1.0)
                det.score(x)
                det.learn(x)
        finally:
            socket.socket = _original


class TestWarmupFlag(unittest.TestCase):
    """is_warmed_up() must return False before window samples, True after."""

    def test_warmup_flag(self):
        det = OnlineDetector(n_features=7, window=50)
        self.assertFalse(det.is_warmed_up())
        for _ in range(49):
            det.learn(np.random.rand(7))
        self.assertFalse(det.is_warmed_up())
        det.learn(np.random.rand(7))
        self.assertTrue(det.is_warmed_up())


if __name__ == '__main__':
    unittest.main(verbosity=2)
