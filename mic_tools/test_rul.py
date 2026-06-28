#!/usr/bin/env python3
"""
test_rul.py — Tests for ExponentialRUL Kalman-filter estimator.

Run with:
    python -m pytest mic_tools/test_rul.py -v
    # or:
    python mic_tools/test_rul.py

True degradation model used throughout:
    K(t) = K0_TRUE * exp(LAM_TRUE * t),  t in hours
    K0_TRUE = 3.0  (Gaussian kurtosis, healthy bearing)
    LAM_TRUE = 0.02 h^-1
    K_FAIL   = 40.0  (ISO 13381-1 severe-stage threshold)

    True RUL at time t:
        RUL(t) = (ln(K_FAIL/K0_TRUE) / LAM_TRUE) - t
               = (ln(40/3) / 0.02) - t
               ≈ 132.5 - t  hours
"""

import math
import unittest

import numpy as np

from rul_estimator import ExponentialRUL, RULResult

# ── Simulation constants ──────────────────────────────────────────────────────
K0_TRUE  = 3.0
LAM_TRUE = 0.02     # 1/hour
K_FAIL   = 40.0
TRUE_TOTAL_LIFE = math.log(K_FAIL / K0_TRUE) / LAM_TRUE   # ≈ 132.5 h

DT_SEC  = 60.0          # 1 sample per minute
DT_HOUR = DT_SEC / 3600.0


def _sim_kurtosis(t_hours: float, rng: np.random.Generator,
                   log_noise_std: float = 0.08) -> float:
    """Simulate one kurtosis observation with multiplicative log-normal noise."""
    k_true = K0_TRUE * math.exp(LAM_TRUE * t_hours)
    noise  = rng.standard_normal() * log_noise_std
    return max(1.0, k_true * math.exp(noise))


def _run_estimator(n_steps: int, rng: np.random.Generator,
                   obs_noise: float = 0.05) -> list[RULResult]:
    """Run one simulation for n_steps frames; return list of RULResult."""
    est = ExponentialRUL(k_fail=K_FAIL, obs_noise=obs_noise)
    results = []
    t_sec = 0.0
    for step in range(n_steps):
        t_h  = step * DT_HOUR
        k_ob = _sim_kurtosis(t_h, rng)
        results.append(est.update(k_ob, t_sec))
        t_sec += DT_SEC
    return results


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestBasicProperties(unittest.TestCase):

    def test_initial_state_is_no_degradation(self):
        """Before 30 updates, filter should return inf RUL (not converged)."""
        est = ExponentialRUL(k_fail=K_FAIL)
        rng = np.random.default_rng(0)
        for step in range(29):
            r = est.update(_sim_kurtosis(step * DT_HOUR, rng), step * DT_SEC)
        self.assertTrue(math.isinf(r.hours_remaining),
                        "Expected inf RUL before 30 updates")
        self.assertEqual(r.confidence, 0.0)

    def test_lambda_converges_to_positive_value(self):
        """After 200 steps on a degrading signal, lambda_hat should be positive."""
        rng = np.random.default_rng(1)
        results = _run_estimator(200, rng)
        final = results[-1]
        self.assertGreater(final.lambda_hat, 0.0,
                           "lambda_hat should be positive for a degrading signal")

    def test_rul_result_is_dataclass(self):
        est = ExponentialRUL(k_fail=K_FAIL)
        r = est.update(3.5, 0.0)
        self.assertIsInstance(r, RULResult)
        self.assertTrue(hasattr(r, 'hours_remaining'))
        self.assertTrue(hasattr(r, 'confidence'))


class TestConvergence(unittest.TestCase):
    """RUL point estimate must converge to within 10% of truth by hour 50."""

    def test_convergence_at_hour_50(self):
        rng = np.random.default_rng(42)
        # Simulate at 1 sample/minute up to hour 60
        n_steps = int(60 / DT_HOUR)   # 3600 steps

        est = ExponentialRUL(k_fail=K_FAIL, obs_noise=0.05)
        result_at_50 = None
        t_sec = 0.0

        for step in range(n_steps):
            t_h  = step * DT_HOUR
            k_ob = _sim_kurtosis(t_h, rng)
            r    = est.update(k_ob, t_sec)
            if abs(t_h - 50.0) < DT_HOUR / 2:
                result_at_50 = r
            t_sec += DT_SEC

        self.assertIsNotNone(result_at_50, "No result captured near t=50 h")
        self.assertFalse(math.isinf(result_at_50.hours_remaining),
                         "RUL is still inf at hour 50 — filter has not converged")

        true_rul = TRUE_TOTAL_LIFE - 50.0   # ≈ 82.5 h
        est_rul  = result_at_50.hours_remaining
        error    = abs(est_rul - true_rul) / true_rul

        self.assertLess(error, 0.10,
                        f"RUL error at hour 50: {error:.1%} > 10%  "
                        f"(estimated {est_rul:.1f} h, true {true_rul:.1f} h)")

    def test_lambda_hat_converges_to_true_rate(self):
        """lambda_hat should be within 20% of true rate after sufficient data."""
        rng = np.random.default_rng(99)
        n_steps = int(80 / DT_HOUR)   # 4800 steps
        results = _run_estimator(n_steps, rng)
        lam_hat = results[-1].lambda_hat
        error   = abs(lam_hat - LAM_TRUE) / LAM_TRUE
        self.assertLess(error, 0.20,
                        f"lambda_hat error: {error:.1%}  "
                        f"(estimated {lam_hat:.4f}, true {LAM_TRUE:.4f})")


class TestCIConverage(unittest.TestCase):
    """95% CI should contain the true RUL in >= 90% of Monte Carlo trials."""

    def test_ci95_coverage(self):
        # Evaluate at step corresponding to hour 60 (well past warm-up)
        check_step = int(60 / DT_HOUR)   # 3600
        n_trials   = 100
        covered    = 0

        for seed in range(n_trials):
            rng = np.random.default_rng(seed + 1000)
            est = ExponentialRUL(k_fail=K_FAIL, obs_noise=0.05)
            t_sec = 0.0
            last_r = None
            for step in range(check_step + 1):
                t_h   = step * DT_HOUR
                k_ob  = _sim_kurtosis(t_h, rng)
                last_r = est.update(k_ob, t_sec)
                t_sec += DT_SEC

            if last_r is None or math.isinf(last_r.hours_remaining):
                continue   # filter not converged — don't count this trial

            true_rul = TRUE_TOTAL_LIFE - 60.0   # ≈ 72.5 h
            if last_r.hours_low <= true_rul <= last_r.hours_high:
                covered += 1

        coverage = covered / n_trials
        self.assertGreaterEqual(coverage, 0.90,
                                f"CI95 coverage {coverage:.0%} < 90% "
                                f"({covered}/{n_trials} trials contained true RUL)")


class TestEdgeCases(unittest.TestCase):

    def test_constant_healthy_kurtosis_gives_no_degradation(self):
        """Stable kurtosis near 3 should keep lambda ≈ 0 → inf RUL."""
        est = ExponentialRUL(k_fail=K_FAIL)
        rng = np.random.default_rng(7)
        t_sec = 0.0
        for step in range(500):
            k_ob = max(1.0, rng.standard_normal() * 0.2 + 3.0)
            r = est.update(k_ob, t_sec)
            t_sec += DT_SEC

        # With no real degradation, lambda should stay tiny → RUL = inf
        self.assertTrue(r.hours_remaining > 1000 or math.isinf(r.hours_remaining),
                        f"Expected very high RUL for stable machine, got {r.hours_remaining:.1f} h")

    def test_kurtosis_already_above_k_fail(self):
        """If kurtosis already exceeds K_FAIL, RUL should be 0."""
        est = ExponentialRUL(k_fail=K_FAIL)
        rng = np.random.default_rng(8)
        # Feed 100 healthy frames first so filter converges
        t_sec = 0.0
        for step in range(100):
            t_h  = step * DT_HOUR
            k_ob = _sim_kurtosis(t_h, rng)
            est.update(k_ob, t_sec)
            t_sec += DT_SEC
        # Now inject extreme kurtosis
        for _ in range(50):
            r = est.update(50.0, t_sec)
            t_sec += DT_SEC
        # RUL should be 0 or very small
        self.assertLessEqual(r.hours_remaining, 5.0,
                             f"Expected RUL ≈ 0 when K >> K_FAIL, got {r.hours_remaining:.1f} h")

    def test_update_returns_rul_result(self):
        est = ExponentialRUL(k_fail=K_FAIL)
        r   = est.update(4.0, 0.0)
        self.assertIsInstance(r, RULResult)
        self.assertTrue(math.isfinite(r.confidence) or r.confidence == 0.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
