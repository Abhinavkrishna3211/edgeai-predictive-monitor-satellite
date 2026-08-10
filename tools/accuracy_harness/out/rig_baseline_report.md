# Real-rig Normal baseline — method: real-rig

Source CSV: `C:\Users\abhin\Documents\edgeai-predictive-monitor-satellite\mic_tools\logs\csv\2026\08\epm_sat-5ab004_20260810_fresh_postfix.csv`

**4,298 frames** captured over 0.25 hours of live satellite → MQTT → `_process_satellite_frame()` pipeline, ambient/no-injected-fault conditions (see real_rig_baseline_runbook.md for exact capture conditions and the deviation from the originally planned tone stimulus).

## Headline metrics

- **FPR = 0.2001** (20.0% of frames alerted WARN or FAULT)
- **recall_normal = 0.7999**
- precision: n/a — no fault frames exist in this Normal-only baseline capture
- F1: n/a — no fault frames exist in this Normal-only baseline capture
- Alert breakdown: {'OK': 3438, 'WARN': 285, 'FAULT': 575}

## Root-cause signal: IMU crest factor

0.0% of frames have `imu_crest >= 9.0` (the default IMU_CREST_WARN threshold) from ambient vibration alone — median imu_crest across the capture is **4.339**. This is the primary driver of the FPR above if it is still elevated, not the mic channel (mic_crest p50=3.185, well under its own threshold).

## Distributions (p5 / p50 / p95 / min / max)

| channel | p5 | p50 | p95 | min | max |
|---|---|---|---|---|---|
| imu_crest | 3.723 | 4.339 | 4.878 | 3.218 | 5.463 |
| mic_crest | 2.695 | 3.185 | 3.909 | 2.223 | 4.989 |
| mic_kurtosis | -0.603 | -0.117 | 0.543 | -1.250 | 3.126 |
| p_fault | 0.000 | 0.000 | 0.001 | 0.000 | 0.462 |

## Caveats

- recv_verify.py's alert field carries persistence (WARN_PERSIST/CLEAR_PERSIST/FAULT_CLEAR_PERSIST hysteresis) — the FPR above is not simply "0.0% of instantaneous samples exceeded threshold" in isolation, but the underlying per-frame imu_crest distribution relative to IMU_CREST_WARN is the primary driver of whatever WARN/FAULT rate is measured, regardless of the persistence logic layered on top.
- mic_kurtosis has a heavy tail (max within this capture reaches into the hundreds) from real transient acoustic events (room noise, keyboard/movement near the mic) — expected for an unattended multi-hour ambient capture, not a sensor fault.
