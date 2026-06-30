---
id: ADR-001
title: Online anomaly detection via Half-Space Trees (HST)
status: accepted
date: 2026-06-30
deciders: Abhinav Krishna N
---

## Context

The EPM satellite must detect acoustic and vibration anomalies on an embedded node (XIAO ESP32-S3, 512 KB SRAM, 8 MB PSRAM) with no persistent network connection for inference. Three candidate algorithms were evaluated: static IsolationForest trained offline, autoencoder, and online Half-Space Trees. The critical constraint is that the model must learn the machine's normal signature continuously after deployment, without requiring a labelled dataset or retraining pipeline.

## Options considered

### Option A: IsolationForest (static, offline-trained)
**Evidence:** scikit-learn IsolationForest; retrain complexity O(N·d·n_estimators) per dataset.
**Pros:** Well-understood; good default anomaly score calibration.
**Cons:** Requires a labelled or curated training set; model is static — cannot adapt to gradual machine wear or seasonal changes in acoustic environment without a full retrain cycle and OTA update. Retraining O(N·d) means >10 s on any Python backend for N>10 000 samples. Cannot run on-device.

### Option B: Autoencoder (neural, on-device)
**Evidence:** Qualcomm NPU neural autoencoder implemented in `qrb2210_autoencoder.py`. Runs on Adreno 702 via ONNX.
**Pros:** Learns rich non-linear representations.
**Cons:** Requires a warm-up dataset of known-normal frames to train; frozen after training. On XIAO ESP32-S3 (no dedicated NPU): inference requires software float; too slow for 2.2 fps pipeline. The NPU path requires separate hardware (QRB2210 gateway node), not the satellite itself.

### Option C: Half-Space Trees (HST, online streaming)
**Evidence:** Tan, Ting & Liu, "Fast Anomaly Detection for Streaming Data," IJCAI-2011.
Update complexity: O(Ψ·h) per sample, where Ψ=25 (number of trees), h=15 (depth) → 375 node visits per sample.
Memory: O(Ψ·2^h) = 25·32768 ≈ 800 KB. Python implementation via `river>=0.21.0` (BSD licence).
**Pros:** Truly online — each sample updates the model; no full retrain needed. Constant time per update. Detects concept drift via companion ADWIN detector (see ADR-004). `river` library is pure Python, network-free.
**Cons:** river 0.21 HST returns raw scores near 1.0 for all data under some conditions. Mitigated by score EMA normalization: `excess = (raw - ema) / (1.0 - ema + 1e-9); score = 0.5 + 0.5 * excess`.

## Decision
**Chosen: Option C — Half-Space Trees via `river`**

**Justification:** HST update complexity O(375) operations per sample is compatible with 2.2 fps on the Python gateway host. The online learning property eliminates the offline retraining requirement entirely. The ADWIN drift detector (δ=0.002) provides automatic detection of concept drift without requiring labelled fault data. No network calls are made during scoring or learning — confirmed by `test_online_detector.py::TestNetworkIsolation`.

## Consequences
**Positive:**
- Model adapts continuously to normal machine signature evolution
- No OTA retrain cycles needed post-deployment
- Memory footprint ~800 KB fits within gateway Python process
- Network-isolation property: all scoring and learning is on-device (gateway), no cloud dependency

**Negative / trade-offs:**
- Warm-up period of 250 frames (~115 s at 2.2 fps) before anomaly scores are reliable; satellite shows RGB_LEARNING during this window
- Score EMA normalization introduces a hyperparameter (EMA alpha = 0.05); requires tuning if the normal acoustic signature is very spiky

**Metrics to watch:**
- `g_hst_warmed_up` flag transition time from boot
- HST anomaly score distribution mean ± std over 1-hour normal run (target: mean < 0.3)
- False alarm rate (WARN/FAULT alerts during known-good machine operation)

## Validation
`mic_tools/test_online_detector.py` — 7 tests: normal stability, anomaly sensitivity (5σ input → score > 0.8), save/load roundtrip, pickle portability, 2× network isolation tests, warmup flag. All 14 tests pass in 247 s (2026-06-28 run).
