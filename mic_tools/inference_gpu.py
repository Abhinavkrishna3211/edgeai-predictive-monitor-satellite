#!/usr/bin/env python3
"""
inference_gpu.py -- Optional TVM + OpenCL inference targeting Adreno 702
on the Arduino Uno Q (QRB2210).

Use this only after confirming OpenCL is functional on your board:
    clinfo | grep -E 'Platform|Device|Version'

Build TVM with OpenCL support before using this module.
See docs/gpu_setup.md for step-by-step build instructions.

Falls back to inference.InferenceEngine (ONNX Runtime / CPU) if TVM is
not installed or OpenCL initialisation fails.

Usage:
    # Load GPU engine (auto-falls back to CPU if TVM unavailable):
    from inference_gpu import load
    engine = load('model/autoencoder.onnx')
    engine.benchmark()

    # Or directly:
    python inference_gpu.py --model model/autoencoder.onnx
"""

import argparse
import os
import sys
import time

import numpy as np

_TVM_AVAILABLE = False
try:
    import tvm
    from tvm import relay
    from tvm.contrib import graph_executor
    _TVM_AVAILABLE = True
except ImportError:
    pass

# Adreno 702 OpenCL target for QRB2210 (Arduino Uno Q).
# Host triple targets the A53 cores that drive the GPU command queue.
_ADRENO_TARGET = (
    tvm.target.Target(
        'opencl -device=adreno',
        host='llvm -mtriple=aarch64-linux-gnu',
    )
    if _TVM_AVAILABLE else None
)

_BACKEND_LABEL = 'TVM 0.16+ / OpenCL (Adreno 702 @ 845 MHz)'


class GPUInferenceEngine:
    """
    TVM-compiled inference engine targeting the Adreno 702 GPU via OpenCL.
    Exposes the same run() / benchmark() interface as inference.InferenceEngine.
    """

    def __init__(self, onnx_model_path: str):
        if not _TVM_AVAILABLE:
            raise ImportError(
                'TVM is not installed.\n'
                'See docs/gpu_setup.md for build instructions.\n'
                'Use inference.InferenceEngine for CPU fallback.'
            )
        try:
            import onnx
        except ImportError:
            raise ImportError(
                'onnx package required for TVM ONNX frontend.\n'
                '  pip install onnx>=1.14.0'
            )

        if not os.path.exists(onnx_model_path):
            raise FileNotFoundError(f'ONNX model not found: {onnx_model_path}')

        model = onnx.load(onnx_model_path)
        input_name  = model.graph.input[0].name
        input_shape = self._static_shape(model.graph.input[0])
        shape_dict  = {input_name: input_shape}

        mod, params = relay.frontend.from_onnx(model, shape_dict)
        with tvm.transform.PassContext(opt_level=3):
            self._lib = relay.build(mod, target=_ADRENO_TARGET, params=params)

        dev = tvm.device('opencl', 0)
        self._runner     = graph_executor.GraphModule(self._lib['default'](dev))
        self._dev        = dev
        self._input_name = input_name
        self._input_shape = input_shape
        self.model_path  = onnx_model_path

    @staticmethod
    def _static_shape(tensor_value_info) -> tuple:
        dims = tensor_value_info.type.tensor_type.shape.dim
        return tuple(d.dim_value if d.dim_value > 0 else 1 for d in dims)

    @property
    def backend_label(self) -> str:
        return _BACKEND_LABEL

    def run(self, x: np.ndarray) -> np.ndarray:
        """Run one inference pass through the TVM OpenCL runtime."""
        if x.ndim == 1:
            x = x[np.newaxis, :]
        self._runner.set_input(
            self._input_name,
            tvm.nd.array(x.astype(np.float32), self._dev),
        )
        self._runner.run()
        return self._runner.get_output(0).numpy()

    def benchmark(self, n: int = 200, model_label: str = 'autoencoder_v1') -> None:
        """Run n warm inferences and print latency + throughput stats."""
        dummy = np.random.randn(*self._input_shape).astype(np.float32)
        for _ in range(10):
            self.run(dummy)

        times_ms = []
        for _ in range(n):
            t0 = time.perf_counter()
            self.run(dummy)
            times_ms.append((time.perf_counter() - t0) * 1000.0)

        times_ms.sort()
        p50 = times_ms[int(n * 0.50)]
        p95 = times_ms[int(n * 0.95)]
        p99 = times_ms[int(n * 0.99)]
        throughput = int(1000.0 / p50)
        fps_per_sat = 2
        max_sats = throughput // fps_per_sat
        in_dim = self._input_shape[-1]

        print(f'[EPM] Inference backend: {self.backend_label}')
        print(f'[EPM] Model: {model_label} ({in_dim}-dim input)')
        print(f'[EPM] Latency: p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms (n={n})')
        print(f'[EPM] Throughput: {throughput} inferences/sec, '
              f'headroom for {max_sats} satellites @ {fps_per_sat} fps each')


def load(onnx_model_path: str):
    """Return a GPUInferenceEngine, falling back to InferenceEngine on failure."""
    if _TVM_AVAILABLE:
        try:
            engine = GPUInferenceEngine(onnx_model_path)
            print(f'[EPM] GPU engine ready: {engine.backend_label}')
            return engine
        except Exception as e:
            print(f'[EPM] TVM/OpenCL unavailable ({e}) -- falling back to ONNX Runtime CPU')

    sys.path.insert(0, os.path.dirname(__file__))
    from inference import InferenceEngine
    return InferenceEngine(onnx_model_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Benchmark TVM/OpenCL inference on Adreno 702 (falls back to CPU).',
    )
    parser.add_argument('--model', required=True,
                        help='Path to .onnx model file')
    parser.add_argument('--label', default='autoencoder_v1',
                        help='Model label for output (default: autoencoder_v1)')
    parser.add_argument('--n', type=int, default=200,
                        help='Number of benchmark iterations (default: 200)')
    args = parser.parse_args()

    engine = load(args.model)
    engine.benchmark(n=args.n, model_label=args.label)


if __name__ == '__main__':
    main()
