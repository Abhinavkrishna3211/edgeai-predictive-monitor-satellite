#!/usr/bin/env python3
"""
test_inference.py — Unit tests for gateway/pipeline/inference.py
(InferenceEngine, ONNX Runtime wrapper).

onnxruntime and onnx are both installed in this environment (unlike
tensorflow), so these tests build a tiny synthetic Identity .onnx model on
the fly with the onnx package rather than depending on the gitignored local
mic_tools/model/autoencoder.onnx fixture. An Identity op makes every
assertion exact (x_hat == x, reconstruction error == 0.0) without needing
any real trained weights.

Run with:
    python -m pytest tests/pipeline/test_inference.py -v
    python tests/pipeline/test_inference.py
"""

import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.pipeline.inference import InferenceEngine

DIM = 8


def _build_identity_onnx(path: str, dim: int = DIM) -> None:
    import onnx
    from onnx import TensorProto, helper

    inp  = helper.make_tensor_value_info('input',  TensorProto.FLOAT, [None, dim])
    out  = helper.make_tensor_value_info('output', TensorProto.FLOAT, [None, dim])
    node = helper.make_node('Identity', ['input'], ['output'])
    graph = helper.make_graph([node], 'identity_graph', [inp], [out])
    model = helper.make_model(graph, producer_name='epm-test')
    model.opset_import[0].version = 13
    # Installed onnx package defaults to a newer IR version than this
    # environment's onnxruntime build supports (max 11) -- pin it down
    # explicitly rather than depending on package-version alignment.
    model.ir_version = 10
    onnx.checker.check_model(model)
    onnx.save(model, path)


class TestInferenceEngine(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._model_path = os.path.join(self._tmpdir.name, 'identity.onnx')
        _build_identity_onnx(self._model_path)
        self.engine = InferenceEngine(self._model_path)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_run_1d_input_autobatches(self):
        x = np.arange(DIM, dtype=np.float32)
        out = self.engine.run(x)
        self.assertEqual(out.shape, (1, DIM))
        np.testing.assert_allclose(out[0], x, atol=1e-5)

    def test_run_2d_input(self):
        x = np.stack([np.arange(DIM, dtype=np.float32)] * 3)
        out = self.engine.run(x)
        self.assertEqual(out.shape, (3, DIM))
        np.testing.assert_allclose(out, x, atol=1e-5)

    def test_reconstruction_error_zero_for_identity(self):
        x = np.random.default_rng(1).normal(size=DIM).astype(np.float32)
        err = self.engine.reconstruction_error(x)
        self.assertIsInstance(err, float)
        self.assertAlmostEqual(err, 0.0, places=5)

    def test_benchmark_returns_expected_keys(self):
        result = self.engine.benchmark(n=5, model_label='identity_test')
        self.assertIn('p50_ms', result)
        self.assertIn('p95_ms', result)
        self.assertIn('p99_ms', result)
        self.assertIn('provider', result)
        self.assertGreaterEqual(result['p95_ms'], 0.0)

    def test_is_ready(self):
        self.assertTrue(self.engine.is_ready())


class TestInferenceEngineMissingModel(unittest.TestCase):
    def test_missing_path_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            InferenceEngine(os.path.join('no', 'such', 'model.onnx'))


if __name__ == '__main__':
    unittest.main()
