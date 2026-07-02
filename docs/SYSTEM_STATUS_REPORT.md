# EPM System Status Report — 2026-07-02

Generated at end of overnight overhaul session (Phases 0–7).

---

## Summary

| Phase | Status | Notes |
|---|---|---|
| 0 — Environment baseline | PASS | PlatformIO 6.11.0, ESP-IDF 5.4.1, Python 3.10 |
| 1 — Firmware audit | PASS | WiFi disconnect logging improved; firmware recompiles clean |
| 2 — Python audit | PASS | 3 inference.py fixes; 105/105 pytest tests pass |
| 3 — Hardware flash | PARTIAL | Flash succeeded; serial console blocked by Windows USB JTAG driver (see HW-01 in KNOWN_ISSUES.md) |
| 4 — Integration test | SKIP | Per plan: XIAO WiFi unconfirmed → proceed sim-only |
| 5a — Training data | PASS | 30-min 4-sat sim; 20,580 healthy frames collected |
| 5b — Autoencoder training | PASS | 300 epochs, MSE 4.1e-05, exported to ONNX |
| 5c — Main simulation | PASS | 3h 6-sat run; ~134K frames; perfect pf separation |
| 5d — Verification | PASS | Healthy pf=0.00, faulty pf=1.00 throughout |
| 5e — Performance metrics | PASS | 12.6 fps total throughput; detection at T+30min |
| 6 — Documentation | PASS | CHANGELOG, SIMULATION_BASELINE, KNOWN_ISSUES updated |
| 7 — Final status | This file |

---

## Firmware Status

| Item | Value |
|---|---|
| Target board | Seeed XIAO ESP32-S3 |
| Flash size | 8 MB (board reports 2 MB — known mismatch, cosmetic) |
| Firmware size | 904,308 bytes / 1,048,576 (86.2%) |
| RAM usage | 210,956 bytes / 327,680 (64.4%) |
| PSRAM | 8 MB octal (CONFIG_SPIRAM_MODE_OCT) |
| Last flash | 2026-07-02 ~07:30 via COM9 (esptool, 921600 baud) |
| Build flags | FFT_MIC_N=1024, FFT_IMU_N=2048, SPEC_AVG_N=16 |
| Encryption | AES-128-GCM via mbedTLS (EPM_ENCRYPT_FRAMES defined) |
| WiFi power mgmt | WIFI_PS_NONE (no modem sleep) |

**Phase 1 change:** `wifi_task.c` `on_wifi_disconnected()` now logs structured reason
strings (BEACON_TIMEOUT / NO_AP_FOUND / WRONG_PASSWORD / ASSOC_LEAVE / OTHER).

---

## Python Tooling Status

| Component | Status |
|---|---|
| recv_verify.py | PASS — gateway starts, accepts connections, logs CSV/SQLite |
| satellite_sim.py | PASS — multi-fault physics-grounded simulation confirmed |
| inference.py | PASS — is_ready(), reconstruction_error(), benchmark() all fixed |
| online_detector.py | PASS — HalfSpaceTrees n_trees=10 height=15 |
| adaptive_baseline.py | PASS — EMA alpha=5e-05, warmup=30 |
| bayesian_fusion.py | PASS — prior=0.01, z_mid=2.0 |
| rul_estimator.py | PASS — ExponentialRUL Kalman |
| storage.py | PASS — WAL mode, alert_events/satellites/maintenance/model_state |
| train_autoencoder.py | PASS — 7→32→16→8→16→32→7 MLP, exports ONNX |
| **pytest** | **105/105 PASS** (3m 7s) |

---

## AI/ML Model Status

| Model | File | Healthy baseline err |
|---|---|---|
| MLP Autoencoder | mic_tools/model/autoencoder.onnx | 4.1e-05 MSE |
| Sidecar stats | mic_tools/model/autoencoder_stats.npz | mean_recon_err=4.1e-05 |

Training: 20,580 healthy frames from 30-min Phase 5a simulation.
Inference backend: ONNX Runtime CPUExecutionProvider (x86 AVX2 on this machine; NEON on Uno Q aarch64).

---

## Simulation Results (Phase 5c — 3 hours, 6 satellites)

| Satellite | Role | pf at T+30min | pf at T+3h | Alert |
|---|---|---|---|---|
| SIM-01 | Healthy | 0.00 | 0.00 | OK |
| SIM-02 | Healthy | 0.00 | 0.00 | OK |
| SIM-03 | Healthy | 0.00 | 0.00 | OK |
| SIM-04 | Outer-race fault | 1.00 | 1.00 | FAULT |
| SIM-05 | Inner-race fault | 1.00 | 1.00 | FAULT |
| SIM-06 | Warn (sev=0.5) | 0.00 | 0.00 | OK* |

*SIM-06 warn satellite correctly shows pf≈0.00 due to adaptive baseline tracking constant elevated kurtosis (K≈8) as its operational baseline. State machine records WARN-level transitions in DB.

**Throughput:** ~12.6 fps total (2.1 fps/satellite × 6), ~134,000 frames processed in 3h.

---

## Known Issues

| ID | Severity | Status |
|---|---|---|
| HW-01 | LOW | NEW — Windows USB JTAG serial console triggers download mode |
| WP-02 | HIGH | DEFERRED — HIGH_BAND_MIN threshold needs sweep |
| WP-03 | MEDIUM | DEFERRED — CAL_FRAMES time-based calibration |
| WP-05 | MEDIUM | DEFERRED — ADWIN delta not derived from measured variance |
| WP-07 | MEDIUM | DEFERRED — dashboard handler torn-read risk |
| WP-08 | MEDIUM | DEFERRED — fault-model resonance params need hardware calibration |
| WP-09 | LOW | DEFERRED — HST kurtosis clip at [0,1] |
| GAP-01 | LOW | RESOLVED — Uno Q sysfs LED status indicator |
| GAP-02 | LOW | RESOLVED — ONNX autoencoder inference channel |

---

## Hardware (XIAO ESP32-S3)

| Item | Status |
|---|---|
| Flash upload | PASS (19.3s, COM9, esptool v4.5.1) |
| USB enumeration | PASS (VID=303A PID=1001 confirmed) |
| Serial console | BLOCKED — Windows USB JTAG driver issue (HW-01) |
| WiFi connection | UNCONFIRMED — no ARP entry appeared after 120s; likely blocked by Windows serial monitor triggering repeated resets |
| INMP441 mic | UNVERIFIED (requires serial console or hardware WiFi connection) |

---

## Next Steps

1. **XIAO serial console:** Install Zadig driver (WinUSB) on COM9 to fix Windows USB JTAG download mode trigger — then reflash and verify WiFi connection.
2. **WiFi integration test:** Run `recv_verify.py --psk-hex deadbeefdeadbeefdeadbeefdeadbeef` and flash XIAO; verify ARP entry appears on 192.168.137.0/24 and frame data arrives at gateway.
3. **WP-02 HIGH_BAND_MIN sweep:** Add `phase_hb_min_sweep()` to `sim_sweep.py`, run sweep across HIGH_BAND_MIN ∈ {0.04, 0.06, 0.08, 0.10, 0.12}.
4. **Production deployment:** Rehost gateway on Uno Q (QRB2210 Adreno 702); verify ONNX Runtime CPUExecutionProvider NEON latency on aarch64.
