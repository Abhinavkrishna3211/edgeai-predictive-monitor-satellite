#!/usr/bin/env python3
"""
test_ml_scoring.py — Unit tests for gateway/pipeline/ml_scoring.py.

Covers: tflite vs IsolationForest routing in _ml_score_with(), the
direction asymmetry between the two paths (tflite: mse >= threshold is
worse; IsolationForest: score <= threshold is worse), HST detector state
persistence round-tripping through _try_load_hst_state()/_save_hst_state(),
and the IsolationForest fallback branch of _train_sat_model_bg() (the
TensorFlow branch is skipped in this environment — tensorflow isn't
installed, same as test_autoencoder.py's TestBuildTrainExportAutoencoder).

The routing/direction tests use small duck-typed stub objects for
'scaler'/'model' rather than real fitted sklearn estimators — _ml_score_with()
never imports sklearn itself, it just calls whatever .transform()/
.decision_function() the model dict hands it, so a stub is a faithful,
dependency-free test double here (matches this repo's no-mocking-library
convention).

Run with:
    python -m pytest tests/pipeline/test_ml_scoring.py -v
    python tests/pipeline/test_ml_scoring.py
"""

import os
import sys
import shutil
import tempfile
import unittest

import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'mic_tools'))

import matplotlib
matplotlib.use('Agg')
import recv_verify as rv  # noqa: E402

from gateway.pipeline.ml_scoring import (  # noqa: E402
    _HST_AVAILABLE,
    _ml_score_tflite,
    _ml_score_with,
    _save_hst_state,
    _train_sat_model_bg,
    _try_load_hst_state,
)

try:
    import tensorflow  # noqa: F401
    _TF_AVAILABLE = True
except ImportError:
    _TF_AVAILABLE = False


class _StubInferencer:
    """Duck-typed stand-in for autoencoder.NpuInferencer — returns a fixed
    reconstruction MSE regardless of input, so tflite-routing tests control
    the exact score without a real trained model."""

    def __init__(self, mse: float):
        self._mse = mse

    def infer(self, feat_raw):
        return self._mse


class _IdentityScaler:
    def transform(self, X):
        return np.asarray(X, dtype=np.float64)


class _StubIsoModel:
    """Duck-typed stand-in for a fitted IsolationForest — returns a fixed
    decision_function score regardless of input."""

    def __init__(self, score: float):
        self._score = score

    def decision_function(self, X):
        return np.array([self._score])


_BASE_FRAME = {
    'mic_rms': 0.1, 'mic_crest': 3.0, 'mic_kurtosis': 3.0,
    'imu_rms': 0.05, 'imu_crest': 2.5, 'high_band_ratio': 0.1, 'z_score': 0.0,
}


class TestMlScoreWithRouting(unittest.TestCase):
    def test_tflite_type_routes_to_tflite_scorer(self):
        model = {
            'type': 'tflite', 'inferencer': _StubInferencer(0.2),
            't_warn': 0.05, 't_fault': 0.10,
        }
        result = _ml_score_with(_BASE_FRAME, model)
        self.assertEqual(result, rv.EPM_ALERT_FAULT)

    def test_isolation_forest_type_routes_to_fallback_scorer(self):
        model = {
            'scaler': _IdentityScaler(), 'model': _StubIsoModel(-0.5),
            'feat_cols': ['mic_rms', 'mic_crest', 'mic_kurtosis', 'imu_rms',
                          'imu_crest', 'high_band_ratio', 'z_score'],
            't_warn': -0.1, 't_fault': -0.3,
        }
        result = _ml_score_with(_BASE_FRAME, model)
        self.assertEqual(result, rv.EPM_ALERT_FAULT)


class TestMlScoreTfliteDirection(unittest.TestCase):
    """tflite uses mse >= threshold (higher = worse); IsolationForest uses
    score <= threshold (lower = worse) — the two scorers are inverted."""

    def test_tflite_high_mse_is_fault(self):
        model = {'inferencer': _StubInferencer(0.5), 't_warn': 0.05, 't_fault': 0.10}
        self.assertEqual(_ml_score_tflite(_BASE_FRAME, model), rv.EPM_ALERT_FAULT)

    def test_tflite_low_mse_is_ok(self):
        model = {'inferencer': _StubInferencer(0.01), 't_warn': 0.05, 't_fault': 0.10}
        self.assertEqual(_ml_score_tflite(_BASE_FRAME, model), rv.EPM_ALERT_OK)

    def test_isolation_forest_low_score_is_fault(self):
        model = {
            'scaler': _IdentityScaler(), 'model': _StubIsoModel(-1.0),
            'feat_cols': list(_BASE_FRAME.keys()), 't_warn': -0.1, 't_fault': -0.3,
        }
        self.assertEqual(_ml_score_with(_BASE_FRAME, model), rv.EPM_ALERT_FAULT)

    def test_isolation_forest_high_score_is_ok(self):
        model = {
            'scaler': _IdentityScaler(), 'model': _StubIsoModel(1.0),
            'feat_cols': list(_BASE_FRAME.keys()), 't_warn': -0.1, 't_fault': -0.3,
        }
        self.assertEqual(_ml_score_with(_BASE_FRAME, model), rv.EPM_ALERT_OK)


@unittest.skipUnless(_HST_AVAILABLE, 'river (OnlineDetector dependency) not installed')
class TestHstStateRoundTrip(unittest.TestCase):
    """Follows test_alerting.py's own precedent of building a real
    SatelliteState via rv._sat_register() rather than constructing one
    directly, since _sat_register() is what actually wires
    _try_load_hst_state()/model/baseline loading together in production."""

    def setUp(self):
        self._orig_base_dir = rv._BASE_DIR
        self._tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self._tmpdir, 'logs'), exist_ok=True)
        os.makedirs(os.path.join(self._tmpdir, 'model'), exist_ok=True)
        rv._BASE_DIR = self._tmpdir
        self._mac = 'AA:BB:CC:DD:EE:02'
        rv._satellites.pop(self._mac, None)

    def tearDown(self):
        rv._satellites.pop(self._mac, None)
        rv._BASE_DIR = self._orig_base_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_learned_state_survives_save_and_reload(self):
        sat1 = rv._sat_register(self._mac, 'TESTSAT-HST', 1, 0, ('127.0.0.1', 5100))
        self.assertIsNotNone(sat1.hst_detector)

        rng = np.random.default_rng(3)
        for _ in range(30):
            sat1.hst_detector.learn(rng.normal(size=rv.FEATURE_DIM))

        probe = rng.normal(size=rv.FEATURE_DIM)
        score_before = sat1.hst_detector.score(probe)

        _save_hst_state(sat1)

        sat2 = rv.SatelliteState(self._mac, 'TESTSAT-HST', 1, 0, ('127.0.0.1', 5100))
        _try_load_hst_state(sat2)
        self.assertIsNotNone(sat2.hst_detector)
        score_after = sat2.hst_detector.score(probe)

        self.assertAlmostEqual(score_before, score_after, places=9)
        self.assertEqual(sat2.hst_detector._n, sat1.hst_detector._n)

    def test_missing_pickle_starts_fresh_detector(self):
        sat = rv._sat_register(self._mac, 'TESTSAT-HST-FRESH', 1, 0, ('127.0.0.1', 5100))
        self.assertIsNotNone(sat.hst_detector)
        self.assertEqual(sat.hst_detector._n, 0)


class TestTriggerSatTrainingFallback(unittest.TestCase):
    """Exercises _train_sat_model_bg()'s IsolationForest fallback branch
    directly (bypassing _trigger_sat_training()'s background thread so the
    test stays synchronous). The neural-autoencoder branch raises ImportError
    in this environment (tensorflow not installed) and falls through to
    IsolationForest automatically — no skip needed."""

    def setUp(self):
        self._orig_base_dir = rv._BASE_DIR
        self._tmpdir = tempfile.mkdtemp()
        rv._BASE_DIR = self._tmpdir
        self._mac = 'AA:BB:CC:DD:EE:03'
        rv._satellites.pop(self._mac, None)
        self.sat = rv._sat_register(self._mac, 'TESTSAT-TRAIN', 1, 0, ('127.0.0.1', 5100))

    def tearDown(self):
        rv._satellites.pop(self._mac, None)
        rv._BASE_DIR = self._orig_base_dir
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_isolation_forest_fallback_trains_and_sets_state(self):
        rng = np.random.default_rng(11)
        buf = [
            {
                'mic_rms': float(rng.uniform(0.05, 0.15)),
                'mic_crest': float(rng.uniform(2.5, 3.5)),
                'mic_kurtosis': float(rng.uniform(2.5, 3.5)),
                'imu_rms': float(rng.uniform(0.02, 0.08)),
                'imu_crest': float(rng.uniform(2.0, 3.0)),
                'high_band_ratio': float(rng.uniform(0.05, 0.2)),
                'z_score': float(rng.uniform(0.0, 1.0)),
            }
            for _ in range(60)
        ]

        _train_sat_model_bg(self.sat, buf)

        self.assertTrue(self.sat.ml_trained)
        self.assertEqual(self.sat.ml_backend, 'IsolationForest (CPU)')
        self.assertFalse(self.sat.ml_training)
        with rv._sat_models_lock:
            model = rv._sat_models.get(self.sat.mac_hex)
        self.assertIsNotNone(model)
        self.assertEqual(model['type'], 'isolation_forest')
        self.assertGreater(model['t_warn'], model['t_fault'])


if __name__ == '__main__':
    unittest.main()
