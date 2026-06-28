#!/usr/bin/env python3
"""
rul_estimator.py — Exponential degradation RUL estimator with 2-state Kalman filter.

Model:  K(t) = K0 * exp(lambda * t),  t in hours
         → ln K(t) = ln(K0)  +  lambda * t        (linear in state space)

State:  x = [ln(K0), lambda]^T
Observation at time t:  z = ln(K(t)) = x[0] + x[1]*t,  H_t = [1, t]

The Kalman filter tracks both the baseline log-kurtosis (x[0]) and the
degradation rate (x[1]=lambda) from streaming frames, adapting continuously
as the machine condition evolves.

RUL formula (solving K(t_fail) = K_FAIL for t_fail):
    t_fail = (ln(K_FAIL) - ln(K0)) / lambda
    RUL    = t_fail - t_now
           = (ln(K_FAIL) - x[0] - x[1]*t_now) / x[1]

95% credible interval via delta method on the lambda uncertainty:
    sigma_RUL ≈ |d(RUL)/d(lambda)| * sigma_lambda
    d(RUL)/d(lambda) = -(ln(K_FAIL) - ln(K(t_now))) / lambda^2

Reference: ISO 13381-1 defines K_FAIL ≈ 40 for rolling-element bearings
           in the severe-stage degradation zone.
"""
import math
import numpy as np
from dataclasses import dataclass


@dataclass
class RULResult:
    hours_remaining: float   # point estimate (inf = no degradation detected)
    hours_low:       float   # 2.5th percentile of 95% CI
    hours_high:      float   # 97.5th percentile of 95% CI
    confidence:      float   # 0..1 — filter convergence / SNR on lambda
    lambda_hat:      float   # current degradation rate estimate (1/h)
    k0_hat:          float   # current baseline kurtosis estimate


class ExponentialRUL:
    """
    Per-satellite RUL estimator.  Call update() on every received frame.
    The filter is self-initialising: it starts with ln(K0)=ln(3) (healthy,
    Gaussian kurtosis ≈ 3) and lambda=0 (no degradation), then adapts.
    """

    def __init__(self, k_fail: float = 40.0,
                 process_noise: float = 1e-6,
                 obs_noise: float = 0.05):
        # State: x = [ln(K0), lambda]
        self.x = np.array([math.log(3.0), 0.0])
        # Initial covariance — loose on K0, very tight on lambda (assume healthy)
        self.P = np.array([[1.0, 0.0], [0.0, 1e-3]])
        self.Q = np.eye(2) * process_noise   # process noise (slow drift allowed)
        self.R = obs_noise                    # observation noise in log-K space
        self.k_fail = k_fail
        self.t_start: float | None = None
        self.n_updates: int = 0

    def update(self, kurtosis: float, timestamp_sec: float) -> RULResult:
        """Ingest one frame and return updated RUL estimate."""
        if self.t_start is None:
            self.t_start = timestamp_sec
        t_hours = (timestamp_sec - self.t_start) / 3600.0

        # ── Kalman predict ───────────────────────────────────────────────────
        # State is assumed constant between frames; only covariance grows.
        self.P = self.P + self.Q

        # ── Kalman update ─────────────────────────────────────────────────────
        z = math.log(max(kurtosis, 1e-3))          # observation: ln(K)
        H = np.array([1.0, t_hours])               # measurement model: [1, t]
        y = z - float(H @ self.x)                  # innovation
        S = float(H @ self.P @ H) + self.R         # innovation variance
        K = (self.P @ H) / S                       # Kalman gain (2-element vector)
        self.x = self.x + K * y
        self.P = (np.eye(2) - np.outer(K, H)) @ self.P
        # Symmetrise to prevent numerical drift
        self.P = 0.5 * (self.P + self.P.T)
        self.n_updates += 1

        return self._compute_rul(t_hours)

    def _compute_rul(self, t_now_hours: float) -> RULResult:
        ln_k0, lam = float(self.x[0]), float(self.x[1])

        # Require at least 30 updates and a positive (degrading) lambda
        if lam <= 1e-6 or self.n_updates < 30:
            return RULResult(
                hours_remaining=math.inf,
                hours_low=math.inf,
                hours_high=math.inf,
                confidence=0.0,
                lambda_hat=max(lam, 0.0),
                k0_hat=math.exp(ln_k0),
            )

        # RUL point estimate
        rul = (math.log(self.k_fail) - ln_k0 - lam * t_now_hours) / lam
        rul = max(0.0, rul)

        # Uncertainty propagation (delta method on lambda)
        sigma_lam = math.sqrt(max(float(self.P[1, 1]), 0.0))
        ln_k_now  = ln_k0 + lam * t_now_hours
        drul_dlam = -(math.log(self.k_fail) - ln_k_now) / (lam * lam)
        sigma_rul = abs(drul_dlam) * sigma_lam

        rul_low  = max(0.0, rul - 1.96 * sigma_rul)
        rul_high = rul + 1.96 * sigma_rul

        # Confidence: posterior SNR on lambda (capped at 1.0)
        confidence = min(1.0, lam / (sigma_lam + 1e-9) / 10.0)

        return RULResult(
            hours_remaining=rul,
            hours_low=rul_low,
            hours_high=rul_high,
            confidence=confidence,
            lambda_hat=lam,
            k0_hat=math.exp(ln_k0),
        )
