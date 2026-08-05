#!/usr/bin/env python3
"""
Online unsupervised anomaly detection using Half-Space Trees (HST).

Tan, Ting, Liu (2011) "Fast Anomaly Detection for Streaming Data" IJCAI.

ON-DEVICE GUARANTEE
-------------------
This module performs all computation locally:
- No network calls. No HTTP, no gRPC, no socket connections beyond the
  existing TCP gateway port.
- No external services. river is a pure-Python library installed once via
  pip; after install, no further internet contact.
- No telemetry. No usage data leaves the device.
- No licence checks. river is BSD-licensed open source.

You can verify the no-network guarantee by running, on the gateway host:
    sudo tcpdump -i any -n 'not port 22 and not port 5100 and not port 8080'
while the detector is active. Expected output: zero packets.
"""
import collections
import numpy as np
import pickle
from typing import Optional
from river.anomaly import HalfSpaceTrees
from river.drift import ADWIN


class OnlineDetector:
    def __init__(self, n_features: int, n_trees: int = 25, height: int = 15,
                 window: int = 250, seed: int = 42, drift_delta: float = 0.002):
        self.n_features  = n_features
        self.n_trees     = n_trees
        self.height      = height
        self.window      = window
        self.seed        = seed
        self._drift_delta = drift_delta
        self._hst = HalfSpaceTrees(n_trees=n_trees, height=height,
                                    window_size=window, seed=seed)
        self._mean = np.zeros(n_features, dtype=np.float64)
        self._m2   = np.ones(n_features, dtype=np.float64)
        self._n    = 0
        # ADWIN concept-drift detector — only updated on OK-frame scores
        # to avoid learning fault signatures as the new normal.
        self._drift        = ADWIN(delta=drift_delta)
        self._drift_events: collections.deque = collections.deque(maxlen=100)
        # VERIFY-FIX: river 0.21 HST returns scores near 1.0 for ALL data; normalize
        # relative to the learned normal-score baseline so normal~0.5 and anomaly>0.8.
        self._score_ema    = 0.5    # EMA of raw HST scores on training data
        self._score_ema_alpha = 0.05  # EMA weight (decays slowly — stable baseline)

    def _welford_update(self, x: np.ndarray):
        self._n += 1
        delta = x - self._mean
        self._mean += delta / self._n
        delta2 = x - self._mean
        self._m2 += delta * delta2

    def _normalize(self, x: np.ndarray) -> dict:
        if self._n < 30:
            x_norm = np.clip(x, 0.0, 1.0)
        else:
            std = np.sqrt(self._m2 / max(self._n - 1, 1))
            z = (x - self._mean) / (std + 1e-9)
            x_norm = np.clip((z + 3.0) / 6.0, 0.0, 1.0)
        return {f"f{i}": float(x_norm[i]) for i in range(self.n_features)}

    def score(self, x: np.ndarray) -> float:
        x_dict = self._normalize(x)
        raw = float(self._hst.score_one(x_dict))
        # VERIFY-FIX: river 0.21 HST raw scores cluster near 1.0 even for normal data
        # because all mass in a window is non-zero. We normalize relative to the EMA of
        # normal-data scores so that typical healthy data → ~0.5 and anomalies → >0.8.
        if self._n < 30:
            return raw
        # Map: raw==_score_ema → 0.5; raw==1.0 → scales up from 0.5 proportionally
        # Using sigmoid-like stretch: score = 0.5 + 0.5 * (raw - ema) / (1.0 - ema + 1e-9)
        baseline = self._score_ema
        excess   = (raw - baseline) / (1.0 - baseline + 1e-9)
        return float(np.clip(0.5 + 0.5 * excess, 0.0, 1.0))

    def learn(self, x: np.ndarray) -> None:
        self._welford_update(x)
        x_dict = self._normalize(x)
        self._hst.learn_one(x_dict)
        # VERIFY-FIX: update score EMA on training (healthy) samples so score()
        # can normalize relative to the typical normal-data score baseline.
        if self._n >= 30:
            raw_train = float(self._hst.score_one(x_dict))
            self._score_ema += self._score_ema_alpha * (raw_train - self._score_ema)

    def check_drift(self, score: float, timestamp: float) -> bool:
        """Feed one OK-frame score into ADWIN and return True if drift is detected.

        IMPORTANT: call this only when the current alert is OK. Feeding scores
        from WARN/FAULT frames teaches ADWIN that elevated anomaly scores are the
        new baseline, corrupting the drift detector.

        When drift is detected, call refresh_baseline() with recent OK-frame
        feature vectors to reset the model to the new operating point.
        """
        self._drift.update(score)
        if self._drift.drift_detected:
            self._drift_events.append(timestamp)
            return True
        return False

    def refresh_baseline(self, recent_samples: list) -> None:
        """Reset HST, Welford normalizer, and drift detector; re-learn from
        recent OK-frame feature vectors.

        Call only after check_drift() returns True AND the current alert is OK.
        recent_samples must contain only healthy (non-fault) frames so the
        model learns the new normal operating point, not a fault signature.
        """
        # Reset the HST tree
        self._hst = HalfSpaceTrees(
            n_trees=self.n_trees, height=self.height,
            window_size=self.window, seed=self.seed)
        # Reset Welford normalizer — critical: without this, normalisation
        # still uses the old distribution's mean/variance, poisoning the new model.
        self._mean = np.zeros(self.n_features, dtype=np.float64)
        self._m2   = np.ones(self.n_features, dtype=np.float64)
        self._n    = 0
        # Reset drift detector so it starts tracking the new baseline
        self._drift = ADWIN(delta=self._drift_delta)
        # VERIFY-FIX: reset score EMA so normalization re-learns new regime's baseline.
        self._score_ema = 0.5
        # Re-learn from provided OK samples
        for x in recent_samples:
            self.learn(x)

    def is_warmed_up(self) -> bool:
        return self._n >= self.window

    def save(self, path: str) -> None:
        state = {
            'hst':          self._hst,
            'mean':         self._mean,
            'm2':           self._m2,
            'n':            self._n,
            'n_features':   self.n_features,
            'n_trees':      self.n_trees,
            'height':       self.height,
            'window':       self.window,
            'seed':         self.seed,
            'drift_delta':  self._drift_delta,
            'drift':        self._drift,
            'drift_events': list(self._drift_events),
            # VERIFY-FIX: persist score EMA so normalization survives restart.
            'score_ema':    self._score_ema,
        }
        with open(path, 'wb') as f:
            pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)

    def load(self, path: str) -> None:
        with open(path, 'rb') as f:
            state = pickle.load(f)
        # VERIFY-FIX: check dimension match so a pickle from a different FEATURE_DIM doesn't
        # silently corrupt the normalizer arrays (mean/m2 shape mismatch).
        saved_n_features = state.get('n_features', self.n_features)
        if saved_n_features != self.n_features:
            raise ValueError(
                f"Pickle n_features={saved_n_features} != detector n_features={self.n_features}; "
                "delete the stale .pkl file to start fresh.")
        self._hst          = state['hst']
        self._mean         = state['mean']
        self._m2           = state['m2']
        self._n            = state['n']
        # Drift fields use .get() for backward compat with pickles from older versions
        self._drift_delta  = state.get('drift_delta', self._drift_delta)
        self._drift        = state.get('drift', ADWIN(delta=self._drift_delta))
        self._drift_events = collections.deque(
            state.get('drift_events', []), maxlen=100)
        # VERIFY-FIX: restore score EMA for normalization continuity; default 0.5 for old pickles.
        self._score_ema    = float(state.get('score_ema', 0.5))
