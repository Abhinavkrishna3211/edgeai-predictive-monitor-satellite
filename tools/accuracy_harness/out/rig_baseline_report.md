# Real-rig Normal baseline — method: real-rig

Source CSV: `C:\Users\abhin\Documents\edgeai-predictive-monitor-satellite\mic_tools\logs\csv\2026\08\epm_sat-5ab004_20260810.csv`

**135,032 frames** captured over 9.46 hours of live satellite → MQTT → `_process_satellite_frame()` pipeline, ambient/no-injected-fault conditions (see real_rig_baseline_runbook.md for exact capture conditions and the deviation from the originally planned tone stimulus).

## Headline metrics

- **FPR = 0.8568** (85.7% of frames alerted WARN or FAULT)
- **recall_normal = 0.1432**
- precision: n/a — no fault frames exist in this Normal-only baseline capture
- F1: n/a — no fault frames exist in this Normal-only baseline capture
- Alert breakdown: {'OK': 19343, 'WARN': 88974, 'FAULT': 26715}

## Root-cause signal: IMU crest factor

72.1% of frames have `imu_crest >= 5.0` (the default CREST_WARN threshold) from ambient vibration alone — median imu_crest across the capture is **5.233**, already above the WARN gate. This is the primary driver of the FPR above, not the mic channel (mic_crest p50=3.090, well under threshold).

## Distributions (p5 / p50 / p95 / min / max)

| channel | p5 | p50 | p95 | min | max |
|---|---|---|---|---|---|
| imu_crest | 4.560 | 5.233 | 6.025 | 3.752 | 8.162 |
| mic_crest | 2.394 | 3.090 | 3.884 | 1.549 | 31.589 |
| mic_kurtosis | -0.910 | -0.178 | 0.843 | -1.754 | 969.464 |
| p_fault | 0.000 | 0.000 | 0.997 | 0.000 | 1.000 |

## Caveats

- recv_verify.py's alert field carries persistence (WARN_PERSIST/CLEAR_PERSIST/FAULT_CLEAR_PERSIST hysteresis) — a fraction this high is not simply "72% of instantaneous samples exceeded threshold" in isolation, but the underlying per-frame imu_crest distribution (median already >= CREST_WARN) is the root cause regardless of the persistence logic layered on top.
- mic_kurtosis has a heavy tail (max within this capture reaches into the hundreds) from real transient acoustic events (room noise, keyboard/movement near the mic) — expected for an unattended multi-hour ambient capture, not a sensor fault.
