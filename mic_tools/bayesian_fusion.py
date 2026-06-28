#!/usr/bin/env python3
"""
bayesian_fusion.py — Bayesian posterior fusion of independent anomaly evidence channels.

Each sensor channel provides a likelihood ratio L_i = P(observation | fault) /
P(observation | healthy), computed from its z-score via the logistic mapping:

    L_i = sigmoid((z_i - z_mid) / temperature)

Naïve-Bayes posterior (channels assumed conditionally independent):

    P(fault | E) = prior * prod(L_i)
                   ─────────────────────────────────────────────────────────
                   prior * prod(L_i) + (1 - prior) * prod(1 - L_i)

INDEPENDENCE ASSUMPTION
-----------------------
The independence assumption is approximate. For correlated faults (e.g. shaft
misalignment exciting both mic and IMU together) this overestimates joint
evidence. Acceptable for fault DETECTION but not for fault MAGNITUDE.

In practice, the EPM system runs 2–3 channels: mic_kurtosis z-score,
mic_rms z-score, and (when warmed up) the HST anomaly score mapped to
z-scale. Kurtosis and RMS respond differently — kurtosis is impulsive,
RMS is energetic — so the independence approximation holds reasonably.

Typical operating points (prior=0.01, z_mid=3.0):
  • 4 channels all at z=0   → p_fault ≈ 10⁻⁷ (near-zero, healthy fleet)
  • 4 channels all at z=4.5 → p_fault ≈ 0.80  (WARN territory)
  • 4 channels all at z=7   → p_fault > 0.99  (FAULT with high confidence)
  • 1 channel at z=10, 3 at z=0 → p_fault < 0.002 (single transient isolated)

The last point is the isolation property: a single extreme transient does NOT
dominate because the three healthy channels provide strong contrary evidence.
This complements the temporal persistence counters (WARN_PERSIST, CLEAR_PERSIST)
in recv_verify.py, which provide isolation in the time dimension.
"""
import math
import numpy as np


class BayesianFusion:
    """
    Fuses N independent anomaly evidence streams into a posterior P(fault | evidence).

    Parameters
    ----------
    prior       : float
        Prior probability of fault per frame. Start at 0.01 (1%). A higher
        value (e.g. 0.05) makes the system more sensitive at the cost of
        more false positives. Tune via --fault-prior.
    z_mid       : float
        Z-score at which a channel contributes 50% fault likelihood. A channel
        at z_mid contributes equal fault and healthy evidence (L=0.5). Default
        3.0 means "3-sigma deviation is the 50/50 evidence point."
    temperature : float
        Softness of the logistic transition. Smaller = sharper transition
        at z_mid. Default 1.0 corresponds to a standard logistic.
    """

    def __init__(self, prior: float = 0.01, z_mid: float = 3.0,
                 temperature: float = 1.0):
        if not (0.0 < prior < 1.0):
            raise ValueError(f"prior must be in (0, 1), got {prior}")
        if temperature <= 0.0:
            raise ValueError(f"temperature must be positive, got {temperature}")
        self.prior       = prior
        self.z_mid       = z_mid
        self.temperature = temperature

    def channel_likelihood(self, z_score: float) -> float:
        """Logistic likelihood for one channel: 0.5 at z=z_mid, saturating to 1."""
        return 1.0 / (1.0 + math.exp(-(z_score - self.z_mid) / self.temperature))

    def fuse(self, z_scores: list) -> float:
        """
        Return posterior P(fault | all channels) in [0, 1].

        NaN z-scores are silently dropped — omit a channel by passing NaN or
        by not including it in the list. An empty list returns the prior.
        """
        likelihoods = [
            self.channel_likelihood(z) for z in z_scores
            if not (isinstance(z, float) and math.isnan(z))
        ]
        if not likelihoods:
            return float(self.prior)

        p_evidence_given_fault   = 1.0
        p_evidence_given_healthy = 1.0
        for L in likelihoods:
            p_evidence_given_fault   *= L
            p_evidence_given_healthy *= (1.0 - L)

        num = self.prior * p_evidence_given_fault
        den = num + (1.0 - self.prior) * p_evidence_given_healthy
        if den <= 0.0:
            return float(self.prior)
        return float(num / den)
