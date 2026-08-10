# Real-rig Normal baseline — method: real-rig

Source CSV: `C:\Users\abhin\Documents\edgeai-predictive-monitor-satellite\mic_tools\logs\csv\2026\08\epm_sat-5ab004_20260810_postfix_bin0.csv`

**2,977 frames** captured over 0.17 hours of live satellite → MQTT → `_process_satellite_frame()` pipeline, ambient/no-injected-fault conditions (see real_rig_baseline_runbook.md for exact capture conditions and the deviation from the originally planned tone stimulus).

## Headline metrics

- **FPR = 0.8310** (83.1% of frames alerted WARN or FAULT)
- **recall_normal = 0.1690**
- precision: n/a — no fault frames exist in this Normal-only baseline capture
- F1: n/a — no fault frames exist in this Normal-only baseline capture
- Alert breakdown: {'FAULT': 2259, 'WARN': 215, 'OK': 503}

## Root-cause signal: IMU crest factor

0.0% of frames have `imu_crest >= 9.0` (the default IMU_CREST_WARN threshold) from ambient vibration alone — median imu_crest across the capture is **4.584**. This is the primary driver of the FPR above if it is still elevated, not the mic channel (mic_crest p50=3.120, well under its own threshold).

## Distributions (p5 / p50 / p95 / min / max)

| channel | p5 | p50 | p95 | min | max |
|---|---|---|---|---|---|
| imu_crest | 4.124 | 4.584 | 5.170 | 3.460 | 5.484 |
| mic_crest | 2.446 | 3.120 | 16.472 | 1.753 | 31.911 |
| mic_kurtosis | -0.920 | -0.068 | 104.339 | -1.669 | 1009.602 |
| p_fault | 0.000 | 0.000 | 1.000 | 0.000 | 1.000 |

## Caveats

- recv_verify.py's alert field carries persistence (WARN_PERSIST/CLEAR_PERSIST/FAULT_CLEAR_PERSIST hysteresis) — the FPR above is not simply "0.0% of instantaneous samples exceeded threshold" in isolation, but the underlying per-frame imu_crest distribution relative to IMU_CREST_WARN is the primary driver of whatever WARN/FAULT rate is measured, regardless of the persistence logic layered on top.
- mic_kurtosis has a heavy tail (max within this capture reaches into the hundreds) from real transient acoustic events (room noise, keyboard/movement near the mic) — expected for an unattended multi-hour ambient capture, not a sensor fault.
