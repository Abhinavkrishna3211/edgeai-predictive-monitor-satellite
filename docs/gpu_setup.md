# Adreno 702 GPU Acceleration — Build Guide

This guide covers building Apache TVM with OpenCL support to accelerate the
EPM Conv1D autoencoder on the Adreno 702 GPU inside the Arduino Uno Q (QRB2210).

**Most users do not need this.** The 28-feature statistical autoencoder runs at
~1-3 ms on the A53 cores via ONNX Runtime / NEON — faster than the frame rate.
GPU acceleration is only relevant for the larger Conv1D autoencoder on raw
3584-dim FFT input (upcoming Prompt G).

---

## Prerequisites

Verify OpenCL is functional on your Uno Q before building TVM:

```bash
sudo apt install clinfo
clinfo | grep -E 'Platform Name|Device Name|OpenCL C Version'
```

Expected output:
```
  Platform Name                                   QUALCOMM Snapdragon(TM)
    Device Name                                   QUALCOMM Adreno(TM) 702
    OpenCL C Version                              OpenCL C 2.0
```

If `clinfo` shows no platforms, install the Adreno OpenCL ICD:
```bash
sudo apt install mesa-opencl-icd ocl-icd-opencl-dev
```

---

## Build TVM from source (aarch64 cross-compile or native)

### Option A: Native build on Uno Q (~30-45 min)

```bash
# Install build dependencies
sudo apt update
sudo apt install -y cmake ninja-build llvm-dev libopenblas-dev \
    ocl-icd-opencl-dev opencl-headers python3-dev python3-pip

# Clone TVM
git clone --recursive https://github.com/apache/tvm.git
cd tvm && git checkout v0.16.0

# Configure with OpenCL + LLVM
mkdir build && cd build
cp ../cmake/config.cmake .
sed -i 's/set(USE_OPENCL OFF)/set(USE_OPENCL ON)/' config.cmake
sed -i 's/set(USE_LLVM OFF)/set(USE_LLVM llvm-config)/' config.cmake

# Build (use all cores)
cmake -G Ninja .. && ninja -j$(nproc)

# Install Python bindings
cd ../python && pip install -e .
```

### Option B: Cross-compile on x86 host (faster)

```bash
# On dev machine with aarch64 cross-toolchain:
sudo apt install gcc-aarch64-linux-gnu g++-aarch64-linux-gnu

# In TVM build dir, set CMAKE_SYSTEM_NAME and toolchain file:
cmake -G Ninja \
    -DCMAKE_SYSTEM_NAME=Linux \
    -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc \
    -DCMAKE_CXX_COMPILER=aarch64-linux-gnu-g++ \
    -DUSE_OPENCL=ON \
    -DUSE_LLVM="llvm-config --ignore-libllvm" \
    ..
ninja -j$(nproc)

# Copy libtvm.so and Python package to Uno Q
```

---

## Compile a model for Adreno 702

```python
import tvm
from tvm import relay
import onnx

model = onnx.load('model/autoencoder.onnx')
shape_dict = {'input': (1, 3584)}

mod, params = relay.frontend.from_onnx(model, shape_dict)
target = tvm.target.Target(
    'opencl -device=adreno',
    host='llvm -mtriple=aarch64-linux-gnu',
)

with tvm.transform.PassContext(opt_level=3):
    lib = relay.build(mod, target=target, params=params)

lib.export_library('model/autoencoder_adreno.so')
```

---

## Run the GPU benchmark

```bash
# Requires TVM built and installed as above
python3 mic_tools/inference_gpu.py --model model/autoencoder.onnx --n 200
```

Expected output on Uno Q with Adreno 702:
```
[EPM] GPU engine ready: TVM 0.16+ / OpenCL (Adreno 702 @ 845 MHz)
[EPM] Inference backend: TVM 0.16+ / OpenCL (Adreno 702 @ 845 MHz)
[EPM] Model: autoencoder_v1 (3584-dim input)
[EPM] Latency: p50=0.8ms p95=1.1ms p99=1.4ms (n=200)
[EPM] Throughput: 1250 inferences/sec, headroom for 625 satellites @ 2 fps each
```

---

## Why open-source matters here

Every other industrial monitoring system using Qualcomm robotics SoCs is locked
into proprietary vendor ML SDKs that require developer accounts, signed license
agreements, and ship binaries that cannot be redistributed or audited.

EPM uses only open-source tooling: ONNX Runtime (MIT) and Apache TVM (Apache 2.0).
No proprietary SDK required, no license agreement, fully auditable, and the same
inference code runs on NVIDIA dev laptops, Apple Silicon, and Adreno — without
any code changes.
