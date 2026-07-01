# Parameter Inventory — EPM System

All tunable constants, thresholds, hyperparameters, and architectural choices
across the Python gateway and ESP32-S3 firmware (simulated). Collected 2026-06-30
as the Phase 0 foundation for the full simulation sweep.

**Testable-in-simulation legend**  
`SIM` — exercised by mic_tools/sim_sweep.py without hardware  
`HW` — requires real ESP32-S3, KX134, or INMP441 to measure  
`BOTH` — firmware constant exercised by both paths

---

## 1 — Half-Space Trees (online_detector.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `n_trees` | online_detector.py:31 | **10** (was 25) | Ensemble size; more trees = slower but smoother scores | Phase 2 sweep + Phase 10 combined: n_trees=10 → +11% cohen_d, −71% CPU; applied 2026-07-01 | SIM |
| `height` | online_detector.py:31 | 15 | Max tree depth; controls granularity of half-space partitions | ADR-001: height=15 covers 2^15=32768 partitions >> feature space | SIM |
| `window` | online_detector.py:31 | 250 | Sliding window for HST mass accumulation | ADR-001: ~2 minutes at 2 fps — covers typical warm-up period | SIM |
| `seed` | online_detector.py:31 | 42 | RNG seed for HST partition initialization | Reproducibility; no documented justification for value 42 | SIM |
| `drift_delta` | online_detector.py:31 | 0.002 | ADWIN sensitivity threshold | ADR-004: lower delta = more sensitive to drift; 0.002 from paper defaults | SIM |
| `_score_ema` | online_detector.py:49 | 0.5 | Initial EMA baseline for raw score normalization | No documented justification; healthy data targets ~0.5 | SIM |
| `_score_ema_alpha` | online_detector.py:50 | 0.05 | EMA decay weight for score normalization baseline | No documented justification; slow decay for stable reference | SIM |
| Warmup threshold | online_detector.py:60 | 30 | Min samples before Welford normalization activates | Matches CAL_FRAMES; no separate justification | SIM |
| z-norm offset | online_detector.py:65 | 3.0 | Maps z-score center to [0,1] midpoint | (z+3.0)/6.0 → ±3σ maps to [0,1]; no documented justification | SIM |
| z-norm scale | online_detector.py:65 | 6.0 | Denominator for z-score normalization | See above | SIM |
| `is_warmed_up` threshold | online_detector.py:133 | window (250) | Frames before HST Bayesian channel activates | Matches window size; no separate justification | SIM |

---

## 2 — Exponential RUL Estimator (rul_estimator.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `k_fail` | rul_estimator.py:49 | 40.0 | Kurtosis at imminent bearing failure | ADR-002: ISO 13381-1 rolling-element bearing severe-stage threshold | SIM |
| `process_noise` | rul_estimator.py:50 | 1e-6 | Kalman Q matrix diagonal; controls state drift rate | ADR-002: slow drift allowed; no measurement-derived justification | SIM |
| `obs_noise` | rul_estimator.py:51 | 0.05 | Kalman R; observation noise in log-K space | ADR-002: no measurement-derived justification for 0.05 | SIM |
| Initial `x[0]` (ln K0) | rul_estimator.py:53 | ln(3.0) | Prior on healthy kurtosis baseline | Gaussian kurtosis = 3.0; well-justified | SIM |
| Initial `x[1]` (λ) | rul_estimator.py:53 | 0.0 | Prior on degradation rate | Assume healthy on install; well-justified | SIM |
| Initial `P[0,0]` | rul_estimator.py:55 | 1.0 | Prior variance on ln(K0) | Loose prior; no documented justification | SIM |
| Initial `P[1,1]` | rul_estimator.py:55 | 1e-3 | Prior variance on λ | Tight prior (assume healthy); no documented justification | SIM |
| Min updates threshold | rul_estimator.py:90 | 30 | Updates before RUL is reported | Prevents early noise; matches CAL_FRAMES | SIM |
| λ min threshold | rul_estimator.py:90 | 1e-6 | Minimum positive degradation rate to report | Prevents RUL report on numerical noise | SIM |
| Confidence divisor | rul_estimator.py:114 | 10.0 | Scales λ/σ_λ to [0,1] confidence | No documented justification for divisor=10 | SIM |
| CI z-score | rul_estimator.py:110 | 1.96 | 95% credible interval multiplier | Standard 95% CI; well-justified | SIM |
| Kurtosis floor | rul_estimator.py:73 | 1e-3 | Prevents log(0) in Kalman observation | Defensive guard; no documented justification | SIM |

---

## 3 — Bayesian Fusion (bayesian_fusion.py / recv_verify.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `prior` | bayesian_fusion.py:61 | 0.01 | Prior P(fault) per frame (1%) | ADR-003: industrial base rate estimate; no dataset-derived value | SIM |
| `z_mid` | bayesian_fusion.py:61 | **2.0** (was 3.0) | Z-score for 50% fault likelihood per channel | Phase 3 sweep + Phase 10 combined: z_mid=2.0 → +34% cohen_d isolation; applied 2026-07-01 | SIM |
| `temperature` | bayesian_fusion.py:62 | 1.0 | Logistic transition sharpness | ADR-003: standard logistic; no sweep-derived justification | SIM |
| `P_FUSION_WARN` | recv_verify.py:199 | 0.70 | Posterior threshold to escalate to WARN | No documented justification; 70% plausibly safe | SIM |
| `P_FUSION_FAULT` | recv_verify.py:200 | 0.95 | Posterior threshold to escalate to FAULT | No documented justification; 95% conservative | SIM |
| HST z-score mapping offset | recv_verify.py:750 | `_score_ema` (dynamic) | HST score at z≈0 (healthy baseline) | WP-04 fix: z_hst=(score-detector._score_ema)/0.05; offset tracks EMA not hardcoded 0.3 | SIM |
| HST z-score mapping scale | recv_verify.py:750 | 0.05 | HST score → z-score conversion divisor | No documented justification; (score-ema)/0.05 | SIM |

---

## 4 — Adaptive Baseline (adaptive_baseline.py / recv_verify.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `EMA_ALPHA` | adaptive_baseline.py:31 | **5e-05** (was 0.0005) | EMA weight per OK frame | Phase 4 sweep: 5e-05 → detect@241 vs 0.0005 → detect@1444; applied 2026-07-01 | SIM |
| `WARMUP_N` | adaptive_baseline.py:30 | 30 | Welford warm-up before EMA activates | Matches CAL_FRAMES; no separate justification | SIM |
| Variance floor | adaptive_baseline.py:46 | 1e-6 | Prevents division by zero in z-score | Defensive guard; well-justified | SIM |
| `Z_WARN_SIGMA` | recv_verify.py:218 | 4.0 | Adaptive z-score → WARN threshold | No documented justification; 4-sigma = 1/15787 false rate under Gaussian | SIM |
| `Z_FAULT_SIGMA` | recv_verify.py:219 | 6.0 | Adaptive z-score → FAULT threshold | No documented justification; 6-sigma = 1/10^9 false rate under Gaussian | SIM |
| `Z_HB_SIGMA` | recv_verify.py:220 | 3.0 | High-band adaptive z to bypass HIGH_BAND_MIN filter | No documented justification | SIM |
| `AB_WARMUP_FRAMES` | recv_verify.py:221 | 30 | Min updates before adaptive z-scores are trusted | Matches CAL_FRAMES | SIM |

---

## 5 — Alert Persistence / Hysteresis (recv_verify.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `WARN_PERSIST` | recv_verify.py:181 | 2 | Consecutive non-OK frames to raise WARN | Single-frame transient rejection; no documented justification for value 2 | SIM |
| `CLEAR_PERSIST` | recv_verify.py:182 | 3 | Consecutive OK frames to clear WARN | Hysteresis; no documented justification for value 3 | SIM |
| `FAULT_CLEAR_PERSIST` | recv_verify.py:183 | 8 | Consecutive OK frames to clear FAULT | Longer hold so alarm is noticed; no documented justification for value 8 | SIM |
| `HIGH_BAND_MIN` | recv_verify.py:187 | 0.12 | Min fraction of energy in 2-8kHz to allow alert | Bearing fault excites this band; no sweep-derived value | SIM |

---

## 6 — Alert Thresholds (recv_verify.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `CAL_FRAMES` | recv_verify.py:176 | 30 | Frames to collect z-score calibration baseline | No documented justification; roughly 15s at 2 fps | SIM |
| `K_WARN` | recv_verify.py:172 | 6.0 | Kurtosis → WARN threshold | Gaussian=3, early bearing fault empirically ~6–10 | SIM |
| `K_FAULT` | recv_verify.py:173 | 12.0 | Kurtosis → FAULT threshold | Advanced fault empirically 12+; no codebase-specific source | SIM |
| `K_FAIL` | recv_verify.py:174 | 40.0 | Kurtosis at imminent failure (RUL target) | ISO 13381-1; well-justified | SIM |
| `CREST_WARN` | recv_verify.py:170 | 5.0 | Crest factor → WARN | No documented justification | SIM |
| `CREST_FAULT` | recv_verify.py:171 | 10.0 | Crest factor → FAULT | No documented justification | SIM |

---

## 7 — Fault Models (fault_models.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `K0` | fault_models.py:67 | 3.0 | Healthy Gaussian kurtosis baseline | Gaussian distribution theoretical kurtosis; well-justified | SIM |
| `K_MAX` | fault_models.py:68 | 16.0 | Severe fault kurtosis ceiling for severity model | Conservative ceiling below ISO 13381-1 K_fail=40; no dataset source | SIM |
| `BETA` | fault_models.py:69 | 1.5 | Power-law exponent for severity → kurtosis | Late-stage acceleration observed in run-to-failure datasets; no explicit citation | SIM |
| `DEFAULT_SHAFT_HZ` | fault_models.py:63 | 25.0 | Default shaft frequency (1500 RPM) | Common industrial motor speed; arbitrary default | SIM |
| `fault_amplitude_max` | fault_models.py:188 | 0.45 | Max linear amplitude at severity=1 | No documented justification | SIM |
| Fault jitter std | fault_models.py:191 | 0.005 | ±0.5% speed variation in bearing tones | Realistic shaft speed variation; no source cited | SIM |
| Resonance band low | fault_models.py:226 | 2000 Hz | Lower bound of structural resonance band | Physical bearing fault excitation range; IEEE/ISO cited in ADR-001 | SIM |
| Resonance band high | fault_models.py:227 | 8000 Hz | Upper bound of structural resonance band | See above | SIM |
| Resonance centre | fault_models.py:230 | 4000.0 Hz | Gaussian resonance bump centre | No documented justification for centre choice | SIM |
| Resonance σ | fault_models.py:230 | 1500.0 Hz | Resonance bump width | No documented justification | SIM |
| Resonance amplitude | fault_models.py:230 | 0.12 × severity | Max broadband resonance amplitude | No documented justification | SIM |
| Kurtosis noise std | fault_models.py:258 | 5% of value | Measurement variance on kurtosis | Approximate; no experimental source cited | SIM |
| Pink noise scale | fault_models.py:123 | 1e-4 | 1/f floor spectral density | No documented justification | SIM |
| Pink noise knee | fault_models.py:123 | 80.0 Hz | 1/f rolloff knee frequency | No documented justification | SIM |
| White noise scale | fault_models.py:135 | 2e-6 | White Gaussian measurement noise | No documented justification | SIM |
| `dBFS` floor | fault_models.py (to_dbfs) | 1e-6 | `20*log10(abs(pwr) + 1e-6)` | Prevents -inf; NOTE: recv_verify.py uses `10^(db/10)` (power not amplitude) | SIM |

---

## 8 — Satellite Simulator (satellite_sim.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `MIC_BINS` | satellite_sim.py:54 | 512 | FFT bins (FFT_MIC_N/2) | Matches firmware FFT_MIC_N=1024 | BOTH |
| `IMU_BINS` | satellite_sim.py:55 | 1024 | IMU FFT bins (FFT_IMU_N/2) | Matches firmware FFT_IMU_N=2048 | BOTH |
| Frame interval | satellite_sim.py:235 | 0.45s | Inter-frame delay (≈2.2 fps) | Matches firmware SPEC_AVG_N×FFT capture rate | BOTH |
| Severity WARN thresh | satellite_sim.py:123 | 0.3 | Simulator's internal label for WARN ground truth | Arbitrary split for labelling; not a detector threshold | SIM |
| Severity FAULT thresh | satellite_sim.py:123 | 0.65 | Simulator's internal label for FAULT ground truth | Arbitrary split for labelling; not a detector threshold | SIM |
| Satellite seed formula | satellite_sim.py (seed) | `sat_id * 7919` | Per-satellite deterministic RNG seed | 7919 is prime → low collision probability; no documented justification | SIM |
| Thread stagger | satellite_sim.py:329 | 0.35s | Delay between satellite thread starts | Prevents thundering-herd on gateway; no documented justification | SIM |

---

## 9 — Firmware Constants (src/epm_config.h)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `FFT_MIC_N` | epm_config.h:23 | 1024 | Microphone FFT window (samples) | ADR-008: frequency resolution 16000/1024=15.6 Hz/bin | HW |
| `FFT_IMU_N` | epm_config.h:27 | 2048 | IMU FFT window (samples) | ADR-008: frequency resolution 25600/2048=12.5 Hz/bin | HW |
| `SPEC_AVG_N` | epm_config.h:31 | 4 | Spectral frames to average before sending | Noise reduction; reduces frame rate to ~2.2 fps | HW |
| `MIC_FS_HZ` | epm_config.h:40 | 16000 | I2S mic ODR | INMP441 supported rate; Nyquist = 8kHz covers bearing resonance band | HW |
| `IMU_FS_HZ` | epm_config.h:45 | 25600 | KX134 ODR | KX134 max ODR; Nyquist = 12.8kHz | HW |
| `LED_CAL_FRAMES` | epm_config.h:80 | 30 | Frames before RGB switches from CALIBRATING state | Matches CAL_FRAMES; consistent | HW |
| `MIC_FAIL_MAX` | epm_config.h:88 | 50 | Consecutive mic failures before LOGE escalation | ~3s at 62ms/block; no documented justification for 50 | HW |
| `WIFI_TX_POWER_QTR_DBM` | epm_config.h:99 | 68 (17 dBm) | WiFi TX power cap | ADR-010: reduces peak current 310→220 mA with negligible range loss at <10m | HW |
| `TASK_STACK_MIC` | epm_config.h:122 | 8192 bytes | mic_task stack | 2× spec minimum; kurtosis buffer safety margin | HW |
| `TASK_STACK_DSP` | epm_config.h:123 | 16384 bytes | dsp_task stack | 2× spec minimum; FFT + feature compute on Core 1 | HW |
| `TASK_STACK_IMU` | epm_config.h:124 | 8192 bytes | imu_task stack | 2× spec minimum; 3-axis FFT + cosf() margin | HW |
| `TASK_STACK_WIFI` | epm_config.h:125 | 10240 bytes | wifi_task stack | mbedTLS + mDNS + TCP overhead | HW |
| `TASK_STACK_DIAG` | epm_config.h:126 | 3072 bytes | diagnostics_task stack | Exactly at spec minimum; 512-byte vTaskGetRunTimeStats buffer | HW |

---

## 10 — Storage / SQLite (storage.py / recv_verify.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| SQLite cache size | storage.py:74 | 8 MB | Page cache for SQLite | Balances RAM use vs disk I/O on gateway host | SIM |
| CSV rotation max age | storage.py:218 | 90 days | Age before CSV files are gzip-compressed | Operational retention choice; no documented justification | SIM |
| Alert model save interval | recv_verify.py:1082 | 500 frames | HST state checkpoint interval | No documented justification | SIM |
| Notify cooldown | recv_verify.py:141 | 300s | Min time between phone notifications per satellite | 5-min cooldown prevents spam; no documented justification | SIM |
| `N_TRAIN_FRAMES` | recv_verify.py:123 | 300 | OK frames before per-satellite auto-train | No documented justification; ~2.3 min at 2.2fps | SIM |

---

## 11 — Protocol / Wire Format (recv_verify.py)

| Parameter | File:Line | Current Value | Controls | Justification | Testable |
|---|---|---|---|---|---|
| `EPM_MAGIC` | recv_verify.py:155 | 0xEA1DF00D | Frame validation magic | ADR-010: collision-resistant 32-bit value | HW |
| `FEATURE_DIM` | recv_verify.py:196 | 7 | HST input dimension | 7 mic stats; IMU excluded from HST for simplicity | SIM |
| HST features | recv_verify.py:613–621 | kurtosis, crest, rms, spectral_centroid, lo_r, mid_r, hb | Feature vector definition | No documented justification for this 7-feature choice | SIM |

---

## 12 — Simulation Sweep Coverage Summary

Of 67 catalogued parameters:
- **37** have no documented justification beyond inline comments
- **14** cite an ADR or ISO standard
- **16** are firmware-specific (hardware-only testable)
- **51** are exercisable in simulation (sim_sweep.py)

Parameters with highest sensitivity risk (no justification + high impact on detection):
`prior`, `z_mid`, `P_FUSION_WARN/FAULT`, `K_WARN`, `K_FAULT`, `WARN_PERSIST`, `CLEAR_PERSIST`, `HIGH_BAND_MIN`, `drift_delta`

---

## 13 — Combined-Config Production Values (Phase 10, 2026-07-01)

Individual OVAT sweeps (Phases 2–4) each tested one parameter change in isolation.
Phase 10 applied all three recommendations simultaneously and re-ran the Phase 1 baseline
protocol to verify no interaction regression and confirm production values.

| Parameter | Sweep default | **Production value** | Evidence | Measured effect |
|---|---|---|---|---|
| `n_trees` | 25 | **10** | Phase 2 sweep; Phase 10 combined | CPU −71.7% (5134→1455 µs/frame); cohen_d +46% combined |
| `z_mid` | 3.0 | **2.0** | Phase 3 sweep; Phase 10 combined | Corr. 7b: p_fusion=0.1687 vs 0.010 (FP suppression still holds) |
| `EMA_ALPHA` | 0.0005 | **5e-05** | Phase 4 sweep; Phase 10 combined | Earlier drift detection; no FP increase |

**Combined-config result (Phase 10, 3 seeds, fault_type=outer, evo=1800s)**:
- cohen_d: 3.586 / 3.595 / 3.994 → avg **3.725** (baseline 2.547, +46.3%)
- False positives: **0** across all seeds
- Detection frame: **482** (baseline 512, 30 frames earlier)
- Fault recall: **0.870** (baseline 0.862)
- CPU µs/frame: **1455** (baseline 5134, 3.5× faster)

Regression verdict: **PASS** — no false-positive regression, cohen_d strictly above baseline.

These values are applied in `recv_verify.py` (production gateway code) as of 2026-07-01.
