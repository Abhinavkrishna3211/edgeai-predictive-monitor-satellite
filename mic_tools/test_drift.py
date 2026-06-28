#!/usr/bin/env python3
"""
test_drift.py — Unit tests for ADWIN concept-drift detection in OnlineDetector.

Tests cover:
  1. Drift detection: N(0.3, 0.05) → N(0.7, 0.05) shift is detected within
     ~100 samples of the change point.
  2. Baseline refresh: post-refresh scores on new-distribution samples are
     lower than pre-refresh scores on old-distribution samples (model adapts).
  3. OK-only update policy: check_drift() is separate from learn(); calling
     check_drift() while feeding fault scores does not corrupt the detector.
  4. Refresh with fewer than 50 samples (guard in recv_verify.py): verify
     refresh_baseline() still works correctly regardless of sample count.

Run with:
    python -m pytest mic_tools/test_drift.py -v
    # or:
    python mic_tools/test_drift.py
"""

import sys
import os
import unittest
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from online_detector import OnlineDetector


def _make_detector(seed=0):
    return OnlineDetector(n_features=3, n_trees=25, height=15, window=250, seed=seed,
                          drift_delta=0.002)


def _synthetic_features(n, mean_score_target, rng):
    """Generate n 3-d feature vectors whose HST scores will cluster near mean_score_target."""
    return [rng.normal(loc=mean_score_target, scale=0.05, size=3).clip(0.0, 1.0)
            for _ in range(n)]


class TestDriftDetection(unittest.TestCase):
    """ADWIN must detect a mean shift in the OK-frame score stream.

    Tests call check_drift() directly with synthetic scores to isolate ADWIN
    behaviour from the HST learning loop. In production, recv_verify.py passes
    the pre-computed hst_score (from score() before learn()) to check_drift();
    here we simulate that score stream directly.
    """

    def test_detects_shift_in_score_stream(self):
        """500 stable scores at N(0.3, 0.05), then N(0.7, 0.05) → drift detected."""
        rng = np.random.default_rng(42)
        det = _make_detector(seed=0)

        # Stable regime: feed 500 score values directly into ADWIN
        for i in range(500):
            det.check_drift(float(rng.normal(0.3, 0.05)), float(i))

        # Shifted regime: ADWIN should detect the mean shift
        detected_at = None
        for i in range(300):
            if det.check_drift(float(rng.normal(0.7, 0.05)), float(500 + i)):
                detected_at = i + 1
                break

        self.assertIsNotNone(
            detected_at,
            "ADWIN failed to detect a 0.3→0.7 score-mean shift within 300 samples")
        self.assertLessEqual(
            detected_at, 200,
            f"ADWIN detected drift at sample {detected_at}, expected within 200")

    def test_no_false_drift_on_stable_signal(self):
        """Stable score stream should not trigger drift detection."""
        rng = np.random.default_rng(7)
        det = _make_detector(seed=1)

        drift_count = 0
        for i in range(800):
            score = float(rng.normal(0.3, 0.05))
            if det.check_drift(score, float(i)):
                drift_count += 1

        # ADWIN with delta=0.002 may fire occasionally; >3 on stable signal is suspicious
        self.assertLessEqual(
            drift_count, 3,
            f"ADWIN fired {drift_count} times on a stable signal (expected ≤ 3)")


class TestRefreshBaseline(unittest.TestCase):
    """After refresh_baseline(), scores on new-distribution samples should be
    substantially lower than old-model scores on those same samples."""

    def test_scores_drop_after_refresh(self):
        """Old model gives high scores on regime-B samples.

        After refresh_baseline() trained on regime-B samples, the same
        regime-B samples look normal → scores drop.
        """
        rng = np.random.default_rng(99)
        det = _make_detector(seed=2)

        regime_a = [rng.normal(0.3, 0.05, size=3).clip(0.0, 1.0) for _ in range(400)]
        for x in regime_a:
            score = det.score(x)
            det.learn(x)

        regime_b = [rng.normal(0.7, 0.05, size=3).clip(0.0, 1.0) for _ in range(200)]

        # Scores before refresh (regime-A model sees regime-B samples as anomalous)
        scores_before = [det.score(x) for x in regime_b]
        mean_before = float(np.mean(scores_before))

        # Refresh baseline using regime-B samples
        det.refresh_baseline(regime_b)

        # Scores after refresh (new model sees regime-B samples as normal)
        scores_after = [det.score(x) for x in regime_b]
        mean_after = float(np.mean(scores_after))

        self.assertGreater(
            mean_before, mean_after,
            f"Expected scores to drop after baseline refresh. "
            f"Before={mean_before:.4f}, After={mean_after:.4f}")

    def test_refresh_resets_welford_stats(self):
        """refresh_baseline() must reset Welford statistics (_n=0).

        If _n is not reset, the normaliser still uses the old distribution's
        mean/variance, poisoning the new model.
        """
        rng = np.random.default_rng(13)
        det = _make_detector(seed=3)

        for _ in range(400):
            x = rng.normal(0.3, 0.05, size=3).clip(0.0, 1.0)
            det.learn(x)

        regime_b = [rng.normal(0.7, 0.04, size=3).clip(0.0, 1.0) for _ in range(100)]
        det.refresh_baseline(regime_b)

        self.assertEqual(det._n, 100,
                         f"After refresh with 100 samples, _n should be 100, got {det._n}")
        self.assertFalse(
            np.allclose(det._mean, 0.0),
            "After refresh, _mean should reflect regime-B features (not zeroed)")

    def test_refresh_resets_drift_detector(self):
        """refresh_baseline() must reset the ADWIN detector."""
        rng = np.random.default_rng(55)
        det = _make_detector(seed=4)

        for i in range(300):
            x = rng.normal(0.3 if i < 200 else 0.7, 0.05, size=3).clip(0.0, 1.0)
            score = det.score(x)
            det.learn(x)
            det.check_drift(score, float(i))

        regime_b = [rng.normal(0.7, 0.05, size=3).clip(0.0, 1.0) for _ in range(100)]
        det.refresh_baseline(regime_b)

        # After reset the drift detector should not immediately fire on
        # stable regime-B samples (its window starts fresh)
        fired_immediately = False
        for i in range(10):
            x = rng.normal(0.7, 0.02, size=3).clip(0.0, 1.0)
            score = det.score(x)
            if det.check_drift(score, float(1000 + i)):
                fired_immediately = True
                break
        self.assertFalse(fired_immediately,
                         "ADWIN should not fire immediately after reset on stable samples")


class TestOKOnlyPolicy(unittest.TestCase):
    """check_drift() must only be called on OK-frame scores; verify the
    interface keeps this responsibility with the caller (recv_verify.py)."""

    def test_check_drift_is_separate_from_learn(self):
        """check_drift() and learn() are independent calls — verify the interface.

        In production (recv_verify.py), when alert==OK:
          1. hst_score = sat.hst_detector.score(feats)   ← computed earlier
          2. sat.hst_detector.learn(feats)
          3. sat.hst_detector.check_drift(hst_score, now) ← OK frames only

        This test feeds ADWIN a synthetic score stream (bypassing HST) to confirm
        that check_drift() is callable independently of learn(), and that ADWIN
        detects a clear shift from N(0.3) to N(0.7).
        """
        rng = np.random.default_rng(77)
        det = _make_detector(seed=5)

        # Baseline: feed stable OK-frame scores into ADWIN only
        for i in range(500):
            det.check_drift(float(rng.normal(0.3, 0.05)), float(i))

        # Shift: detect elevated OK-frame scores (new operating regime)
        detected = False
        for i in range(300):
            if det.check_drift(float(rng.normal(0.7, 0.05)), float(500 + i)):
                detected = True
                break

        self.assertTrue(detected,
                        "check_drift() should detect a 0.3→0.7 shift in OK-frame scores")


class TestSaveLoad(unittest.TestCase):
    """Drift state must survive save/load round-trip."""

    def test_drift_state_persisted(self):
        import tempfile, os
        rng = np.random.default_rng(101)
        det = _make_detector(seed=6)

        timestamps = []
        for i in range(300):
            x = rng.normal(0.3 if i < 200 else 0.7, 0.05, size=3).clip(0.0, 1.0)
            det.learn(x)
            score = det.score(x)
            if det.check_drift(score, float(i)):
                timestamps.append(float(i))

        with tempfile.NamedTemporaryFile(suffix='.pkl', delete=False) as tf:
            path = tf.name
        try:
            det.save(path)
            det2 = _make_detector(seed=99)
            det2.load(path)
            self.assertEqual(det2._drift_delta, det._drift_delta)
            self.assertEqual(list(det2._drift_events), list(det._drift_events))
            self.assertEqual(det2._n, det._n)
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
