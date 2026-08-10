#!/usr/bin/env python3
"""
test_autoencoder.py — Unit tests for gateway/pipeline/autoencoder.py.

autoencoder.py imports tensorflow lazily, inside build_autoencoder() /
train_autoencoder() / export_tflite() / train_and_export() — never at module
level. This environment has no tensorflow/tflite_runtime installed (confirmed
via pip show), so TestBuildTrainExportAutoencoder is the first
skipUnless-gated test class in this repo; everything else here exercises code
paths that are TF-independent (make_feature_vector) or that are the actually-
live degraded path in this environment (NpuInferencer with no runtime,
load_npu_model on a missing prefix).

Run with:
    python -m pytest tests/pipeline/test_autoencoder.py -v
    python tests/pipeline/test_autoencoder.py
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.pipeline.autoencoder import (
    INPUT_DIM,
    SPEC_BANDS,
    STAT_DIM,
    NpuInferencer,
    load_npu_model,
    make_feature_vector,
)

try:
    import tensorflow  # noqa: F401
    _TF_AVAILABLE = True
except ImportError:
    _TF_AVAILABLE = False


class TestMakeFeatureVector(unittest.TestCase):
    def test_shape_and_dim(self):
        vec = make_feature_vector({'mic_kurtosis': 4.0, 'z_score': 1.5})
        self.assertEqual(vec.shape, (INPUT_DIM,))
        self.assertEqual(INPUT_DIM, STAT_DIM + SPEC_BANDS)

    def test_stat_block_ordering(self):
        frame = {
            'mic_rms': 0.1, 'mic_crest': 3.5, 'mic_kurtosis': 4.2,
            'imu_rms': 0.05, 'imu_crest': 2.8, 'high_band_ratio': 0.33,
            'z_score': 1.7,
        }
        vec = make_feature_vector(frame)
        stats = vec[:STAT_DIM]
        self.assertAlmostEqual(stats[0], 0.1,  places=5)
        self.assertAlmostEqual(stats[1], 3.5,  places=5)
        self.assertAlmostEqual(stats[2], 4.2,  places=5)
        self.assertAlmostEqual(stats[3], 0.05, places=5)
        self.assertAlmostEqual(stats[4], 2.8,  places=5)
        self.assertAlmostEqual(stats[5], 0.33, places=5)
        self.assertAlmostEqual(stats[6], 1.7,  places=5)
        # log1p(kurtosis) / log1p(z_score) derived tail features
        self.assertAlmostEqual(stats[7], np.log1p(4.2), places=5)
        self.assertAlmostEqual(stats[8], np.log1p(1.7), places=5)

    def test_missing_mic_fft_zero_fills_spectral_bands(self):
        vec = make_feature_vector({'mic_kurtosis': 3.0, 'z_score': 0.0})
        bands = vec[STAT_DIM:]
        self.assertEqual(bands.shape, (SPEC_BANDS,))
        self.assertTrue(np.all(bands == 0.0))

    def test_present_mic_fft_bands_in_range(self):
        rng = np.random.default_rng(42)
        fft = rng.uniform(-100.0, -10.0, size=2048).astype(np.float32)
        vec = make_feature_vector({'mic_kurtosis': 3.0, 'z_score': 0.0, 'mic_fft': fft})
        bands = vec[STAT_DIM:]
        self.assertEqual(bands.shape, (SPEC_BANDS,))
        self.assertTrue(np.all(bands >= -1.0))
        self.assertTrue(np.all(bands <= 1.0))

    def test_too_short_mic_fft_zero_fills(self):
        vec = make_feature_vector({'mic_kurtosis': 3.0, 'z_score': 0.0, 'mic_fft': [1.0, 2.0]})
        bands = vec[STAT_DIM:]
        self.assertTrue(np.all(bands == 0.0))


@unittest.skipUnless(_TF_AVAILABLE, 'tensorflow not installed in this environment')
class TestBuildTrainExportAutoencoder(unittest.TestCase):
    def test_train_and_export_round_trip(self):
        import tempfile

        from gateway.pipeline.autoencoder import export_tflite, train_autoencoder

        rng = np.random.default_rng(7)
        X = rng.normal(loc=0.0, scale=1.0, size=(64, 8)).astype(np.float32)

        model, scaler, t_warn, t_fault, mse_train = train_autoencoder(
            X, contamination=0.05, epochs=5, batch_size=16)

        self.assertGreater(t_fault, t_warn)
        self.assertGreater(t_warn, 0.0)
        self.assertEqual(len(mse_train), len(X))

        with tempfile.TemporaryDirectory() as tmpdir:
            prefix = os.path.join(tmpdir, 'test_model')
            tflite_path = export_tflite(model, scaler, prefix, X)
            self.assertTrue(os.path.exists(tflite_path))
            self.assertTrue(os.path.exists(prefix + '_scaler.json'))


class TestNpuInferencerDegradedPath(unittest.TestCase):
    """Exercises the actually-live path in this environment: NpuInferencer
    with no tflite_runtime/tensorflow.lite available at all falls back to
    self._interp is None rather than raising."""

    def setUp(self):
        import json
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self._scaler_path = os.path.join(self._tmpdir.name, 'scaler.json')
        with open(self._scaler_path, 'w') as f:
            json.dump({'mean': [0.0] * INPUT_DIM, 'scale': [1.0] * INPUT_DIM,
                       'n_features': INPUT_DIM}, f)
        # tflite file need not be a real model: __init__ opens the scaler
        # first, then _load() only reaches Interpreter(...) if a TFLite
        # runtime import succeeds -- which it never does in this environment.
        self._tflite_path = os.path.join(self._tmpdir.name, 'model.tflite')
        with open(self._tflite_path, 'wb') as f:
            f.write(b'\x00')

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_no_runtime_available_is_not_available(self):
        inferencer = NpuInferencer(self._tflite_path, self._scaler_path)
        if inferencer.available:
            self.skipTest('a TFLite runtime is actually installed in this environment')
        self.assertFalse(inferencer.available)
        self.assertFalse(inferencer.npu_active)

    def test_infer_returns_zero_when_unavailable(self):
        inferencer = NpuInferencer(self._tflite_path, self._scaler_path)
        if inferencer.available:
            self.skipTest('a TFLite runtime is actually installed in this environment')
        feat = np.zeros(INPUT_DIM, dtype=np.float32)
        self.assertEqual(inferencer.infer(feat), 0.0)


class TestLoadNpuModelMissingFiles(unittest.TestCase):
    def test_missing_prefix_returns_none(self):
        result = load_npu_model(os.path.join('no', 'such', 'prefix'))
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
