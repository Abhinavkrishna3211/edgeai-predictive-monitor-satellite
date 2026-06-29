#!/usr/bin/env python3
"""
inference.py -- ONNX Runtime inference engine for the EPM neural autoencoder.

Provider selection order:
  1. CUDAExecutionProvider   -- NVIDIA dev laptops
  2. CoreMLExecutionProvider -- macOS dev laptops
  3. CPUExecutionProvider    -- everything else; on Uno Q (aarch64) this
                               is ARMv8 NEON SIMD-accelerated automatically
                               by ONNX Runtime's aarch64 build.

A 28-feature autoencoder hits ~1-3 ms on the Uno Q's A53 cores with
CPUExecutionProvider -- already faster than the frame rate (~450 ms/frame).
GPU acceleration via TVM/OpenCL (inference_gpu.py) is reserved for the
larger Conv1D autoencoder on raw 3584-dim FFT input.

Usage:
    python inference.py --model model/autoencoder.onnx
    python inference.py --model model/autoencoder.onnx --label autoencoder_v1 --n 500
"""

import argparse
import os
import platform
import sys
import time

import numpy as np

try:
    import onnxruntime as ort
    _ORT_AVAILABLE = True
except ImportError:
    _ORT_AVAILABLE = False

PROVIDER_PREFERENCE = [
    'CUDAExecutionProvider',
    'CoreMLExecutionProvider',
    'CPUExecutionProvider',
]

_IS_AARCH64 = platform.machine() in ('aarch64', 'arm64')

_PROVIDER_LABELS = {
    'CUDAExecutionProvider':
        'ONNX Runtime / CUDAExecutionProvider (NVIDIA GPU)',
    'CoreMLExecutionProvider':
        'ONNX Runtime / CoreMLExecutionProvider (Apple Neural Engine)',
    'CPUExecutionProvider':
        ('ONNX Runtime / CPUExecutionProvider (NEON aarch64)'
         if _IS_AARCH64 else
         'ONNX Runtime / CPUExecutionProvider (x86 AVX2)'),
}


class InferenceEngine:
    """ONNX Runtime inference engine with automatic hardware provider selection."""

    def __init__(self, model_path: str):
        if not _ORT_AVAILABLE:
            raise ImportError(
                "onnxruntime is not installed.\n"
                "  pip install onnxruntime>=1.17.0"
            )
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"ONNX model not found: {model_path}")

        available = {p.name for p in ort.get_all_providers()}
        providers = [p for p in PROVIDER_PREFERENCE if p in available]
        if not providers:
            providers = ['CPUExecutionProvider']

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            model_path, sess_options=opts, providers=providers)

        self._active_provider = self._session.get_providers()[0]
        self._input_name  = self._session.get_inputs()[0].name
        self._output_name = self._session.get_outputs()[0].name
        self._input_shape  = self._session.get_inputs()[0].shape
        self._output_shape = self._session.get_outputs()[0].shape
        self.model_path = model_path

    @property
    def backend_label(self) -> str:
        return _PROVIDER_LABELS.get(
            self._active_provider,
            f'ONNX Runtime / {self._active_provider}',
        )

    def run(self, x: np.ndarray) -> np.ndarray:
        """Run one inference pass. Input is auto-batched if 1-D."""
        if x.ndim == 1:
            x = x[np.newaxis, :]
        out = self._session.run(
            [self._output_name],
            {self._input_name: x.astype(np.float32)},
        )
        return out[0]

    def benchmark(self, n: int = 200, model_label: str = 'autoencoder_v1') -> None:
        """Run n warm inferences and print latency + throughput stats."""
        in_shape = [
            s if isinstance(s, int) and s > 0 else 1
            for s in self._input_shape
        ]
        dummy = np.random.randn(*in_shape).astype(np.float32)

        for _ in range(10):
            self._session.run([self._output_name], {self._input_name: dummy})

        times_ms = []
        for _ in range(n):
            t0 = time.perf_counter()
            self._session.run([self._output_name], {self._input_name: dummy})
            times_ms.append((time.perf_counter() - t0) * 1000.0)

        times_ms.sort()
        p50 = times_ms[int(n * 0.50)]
        p95 = times_ms[int(n * 0.95)]
        p99 = times_ms[int(n * 0.99)]
        throughput = int(1000.0 / p50)

        in_dim  = in_shape[-1]
        out_dim = next(
            (s for s in reversed(self._output_shape) if isinstance(s, int) and s > 0),
            '?',
        )
        fps_per_sat = 2
        max_sats = throughput // fps_per_sat

        print(f'[EPM] Inference backend: {self.backend_label}')
        print(f'[EPM] Model: {model_label} ({in_dim}-dim input, {out_dim}-dim bottleneck)')
        print(f'[EPM] Latency: p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms (n={n})')
        print(f'[EPM] Throughput: {throughput} inferences/sec, '
              f'headroom for {max_sats} satellites @ {fps_per_sat} fps each')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Benchmark ONNX Runtime inference on this hardware.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--model', required=True,
                        help='Path to .onnx model file')
    parser.add_argument('--label', default='autoencoder_v1',
                        help='Model label for output (default: autoencoder_v1)')
    parser.add_argument('--n', type=int, default=200,
                        help='Number of benchmark iterations (default: 200)')
    args = parser.parse_args()

    engine = InferenceEngine(args.model)
    engine.benchmark(n=args.n, model_label=args.label)


if __name__ == '__main__':
    main()
