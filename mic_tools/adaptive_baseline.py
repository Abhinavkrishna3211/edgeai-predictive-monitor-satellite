#!/usr/bin/env python3
"""
adaptive_baseline.py — Per-machine slowly-updating statistical baseline.

Tracks a slowly-evolving distribution of a single feature (kurtosis, crest
factor, RMS, or high-band energy) during healthy operation.  Issues a z-score
when a new observation is far above the learned mean, providing per-machine
adaptive fault thresholds that require no hand-tuning.

Algorithm
---------
Phase 1 — Warm-up (first WARMUP_N healthy frames):
    Welford online mean/variance estimation.  No z-scores are issued.

Phase 2 — EMA tracking (after warm-up):
    Mean and variance updated via exponential moving average:
        mean  ← (1-α) mean  + α x
        var   ← (1-α) (var  + α (x - mean_prev)²)
    With α=0.0005, the effective half-life is ln(2)/α ≈ 1386 healthy frames.
    At 2 fps that is ≈11.5 minutes — slow enough to track machine wear-in
    without inadvertently learning deteriorating fault signatures.

Only healthy (is_healthy=True) frames advance the model.  WARN/FAULT frames
are silently ignored so a gradually worsening machine cannot corrupt its own
baseline and mask an escalating fault.
"""

import math

WARMUP_N  = 30       # Welford warm-up length before EMA activates (matches CAL_FRAMES)
EMA_ALPHA = 5e-05    # EMA weight per OK frame (Phase 4 sweep: 5e-05 -> detect@241 vs 0.0005 -> detect@1444)


class AdaptiveBaseline:
    """One-dimensional online baseline tracker for a single machine feature."""

    def __init__(self, alpha: float = EMA_ALPHA, warmup_n: int = WARMUP_N):
        self.alpha    = alpha
        self.warmup_n = warmup_n
        # Welford state (warm-up phase)
        self._w_n    = 0
        self._w_mean = 0.0
        self._w_M2   = 0.0   # sum of squared deviations (Welford algorithm)
        # EMA state (post warm-up phase)
        self._mean   = 0.0
        self._var    = 1e-6   # variance floor prevents zero-division
        # Total healthy updates across both phases
        self.n_updates = 0

    # ── Public read-only properties ───────────────────────────────────────────

    @property
    def mean(self) -> float:
        return self._mean if self.n_updates >= self.warmup_n else self._w_mean

    @property
    def std(self) -> float:
        if self.n_updates < self.warmup_n:
            v = (self._w_M2 / max(self._w_n - 1, 1)) if self._w_n > 1 else 1e-6
        else:
            v = self._var
        return max(math.sqrt(v), 1e-6)

    # ── Update ────────────────────────────────────────────────────────────────

    def update(self, x: float, is_healthy: bool) -> None:
        """Advance the baseline with a new observation.

        Parameters
        ----------
        x           : observed feature value
        is_healthy  : True when the frame was classified OK; False on WARN/FAULT
        """
        if not is_healthy:
            return
        x = float(x)
        self.n_updates += 1

        if self.n_updates <= self.warmup_n:
            # Welford online mean/variance — numerically stable for any n
            self._w_n  += 1
            delta       = x - self._w_mean
            self._w_mean += delta / self._w_n
            self._w_M2  += delta * (x - self._w_mean)
            # On the last warm-up frame, seed the EMA from Welford stats
            if self.n_updates == self.warmup_n:
                self._mean = self._w_mean
                self._var  = max(self._w_M2 / max(self._w_n - 1, 1), 1e-6)
        else:
            # EMA update: variance estimator from West (1979)
            delta       = x - self._mean
            self._mean += self.alpha * delta
            self._var   = (1.0 - self.alpha) * (self._var + self.alpha * delta * delta)
            self._var   = max(self._var, 1e-6)

    # ── Inference ─────────────────────────────────────────────────────────────

    def z_score(self, x: float) -> float:
        """Signed deviation: (x − mean) / std.  Returns 0.0 before warm-up."""
        if self.n_updates < self.warmup_n:
            return 0.0
        return (float(x) - self.mean) / self.std

    def warn_threshold(self, k_sigma: float = 4.0) -> float:
        """Absolute value that would produce z_score == k_sigma (WARN by default)."""
        return self.mean + k_sigma * self.std

    def fault_threshold(self, k_sigma: float = 6.0) -> float:
        """Absolute value that would produce z_score == k_sigma (FAULT by default)."""
        return self.mean + k_sigma * self.std

    # ── State management ──────────────────────────────────────────────────────

    def reset(self) -> None:
        """Return to pre-warm-up state (called after concept drift is detected)."""
        self._w_n     = 0
        self._w_mean  = 0.0
        self._w_M2    = 0.0
        self._mean    = 0.0
        self._var     = 1e-6
        self.n_updates = 0

    def state_dict(self) -> dict:
        """Serialisable snapshot for JSON persistence."""
        return {
            'alpha':     self.alpha,
            'warmup_n':  self.warmup_n,
            'w_n':       self._w_n,
            'w_mean':    self._w_mean,
            'w_M2':      self._w_M2,
            'mean':      self._mean,
            'var':       self._var,
            'n_updates': self.n_updates,
        }

    def load_state_dict(self, d: dict) -> None:
        """Restore from a snapshot previously returned by state_dict()."""
        self.alpha     = float(d.get('alpha',    self.alpha))
        self.warmup_n  = int(d.get('warmup_n',   self.warmup_n))
        self._w_n      = int(d.get('w_n',        0))
        self._w_mean   = float(d.get('w_mean',   0.0))
        self._w_M2     = float(d.get('w_M2',     0.0))
        self._mean     = float(d.get('mean',     0.0))
        self._var      = float(d.get('var',      1e-6))
        self.n_updates = int(d.get('n_updates',  0))
