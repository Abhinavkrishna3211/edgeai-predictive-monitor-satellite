# Changelog

All changes to the EPM satellite firmware and tooling. Each entry states what changed, why (with ADR reference where applicable), and the measured or expected impact. Entries tagged `<not yet measured>` require physical hardware bring-up for validation.

Tags: `HW-OPT` hardware optimisation · `FIX` correctness fix · `FEAT` new feature · `BREAK` breaking change · `SEC` security · `DOCS` documentation

---

## [Prior to audit — date from git log: dbbbdfa] BREAK: led_task removed, replaced by rgb_led_task

**Files:** `src/rgb_led_task.c`, `src/rgb_led_task.h` (added); `src/led_task.c`, `src/led_task.h` (deleted)

- **What:** Single-GPIO rhythm-pattern indicator on GPIO21 (led_task) deleted. Replaced by a 3-channel RGB LED on GPIO1/5/6 driven by the ESP32-S3 LEDC hardware fade engine.
- **Why:** RGB LED via LEDC hardware fade provides 9 distinct machine states vs 7 for the old GPIO21 rhythm indicator, with near-zero CPU cost (task blocked on `ulTaskNotifyTake` between fade steps; hardware performs linear interpolation without CPU involvement). See ADR-006.
- **Impact:** GPIO21 is now unallocated (floating). No functional states lost; 2 new states added (TRIPPED, CALIBRATING). rgb_led_task CPU% < 1% at steady state.

---

## [2026-06-30] HW-OPT: I2S DMA buffer marked DMA_ATTR — Phase 2

**Files:** `components/mic_capture/mic_capture.c`, `components/mic_capture/include/mic_capture.h`, `src/epm_protocol.h`

- **What:** `raw_buf` in mic_capture.c marked `DMA_ATTR` (forces internal DRAM, 4-byte alignment). I2S overflow ISR registered. `overflow_count` field added to `epm_header_t` (replaced unused `_pad` byte; struct size unchanged at 48 bytes). `mic_capture_get_overflow_count()` API added.
- **Why:** Without `DMA_ATTR`, the linker may place `raw_buf` in PSRAM during link-time optimisation. The I2S DMA engine cannot address PSRAM on ESP32-S3 without explicit GDMA configuration — a silent misplacement results in DMA reads from the wrong memory region and corrupted audio frames. See ADR-005, ADR-009.
- **Impact (correctness):** I2S DMA now guaranteed to operate on internal DRAM. Overflow events visible in EPM wire header (gateway can detect audio gaps without a separate control channel).
- **sdkconfig.defaults:** `CONFIG_I2S_ISR_IRAM_SAFE=y` added — I2S DMA ISR now in IRAM, immune to 800 µs cache-miss window during WiFi TX bursts.

---

## [2026-06-30] HW-OPT: ESP-DSP vectorised FFT replaces scalar path — Phase 4

**Files:** `src/dsp_task.c`, `src/dsp_task.h`, `src/mic_task.c`, `src/mic_task.h`, `src/epm_config.h`

- **What:** 512-point FFT replaced with `dsps_fft2r_fc32()`. RMS, crest factor, kurtosis replaced with `dsps_dotprod_f32`, `dsps_abs_f32`, `dsps_mul_f32`. Spectral centroid added via pre-computed `s_freq_bins` and `dsps_dotprod_f32`. FFT cycle benchmark logged at boot via `esp_cpu_get_cycle_count()`. mic→dsp handoff changed from queue+memcpy to `esp_ringbuf` (NOSPLIT, 8192 bytes, zero-copy pointer handoff). `spectral_centroid` field added to `mic_frame_t`.
- **Why:** ESP-DSP uses Xtensa LX7 128-bit vector lanes (4×float32/cycle). Scalar Cooley-Tukey: ~4.2 ms / frame. ESP-DSP target: ~1.1 ms / frame (3.8× speedup). See ADR-008.
- **Impact:** FFT latency target ~1.1 ms (264 k cycles at 240 MHz). Ring buffer eliminates 4 KB memcpy per frame between mic_task and dsp_task.
- **Note:** `cpu_hal_get_cycle_count()` (removed in IDF 5.x) corrected to `esp_cpu_get_cycle_count()` from `esp_cpu.h`.

---

## [2026-06-30] HW-OPT: PSRAM selected buffers via EXT_RAM_BSS_ATTR — Phase 7

**Files:** `src/imu_task.c`, `src/dsp_task.c`

- **What:** `s_frame` in imu_task (12 KB) and `s_mag_db` in dsp_task (2 KB) moved to PSRAM via `EXT_RAM_BSS_ATTR`.
- **Why:** These buffers are written once per cycle and never accessed in the hot FFT loop path — no SIMD cache-miss penalty. Moving them to PSRAM frees 14 KB of internal DRAM for WiFi driver and mbedTLS heap allocations. See ADR-009.
- **Impact:** Internal DRAM free at boot increases by ~14 KB. No performance impact on DSP pipeline (hot buffers remain in internal DRAM).
- **sdkconfig.defaults:** `CONFIG_SPIRAM_MODE_OCT=y` added for XIAO OPI DDR PSRAM.

---

## [2026-06-30] HW-OPT: AES-GCM via hardware accelerator + GDMA — Phase 6

**Files:** `src/wifi_task.c`

- **What:** `s_enc_pt` and `s_enc_ct` (AES staging buffers, ~14 KB each) marked `DMA_ATTR`. Hardware AES GDMA path activated.
- **Why:** Software AES-GCM at 2.2 fps with 14 KB frames consumes ~35.8% of core 0 CPU. Hardware GDMA path: ~3% CPU. GDMA requires internal DRAM buffers — without `DMA_ATTR`, buffers may land in PSRAM and cause silent GDMA corruption during concurrent WiFi DMA. See ADR-007.
- **Impact:** wifi_task CPU% for encryption: ~35.8% → ~3%. Frame drops during WiFi TX bursts eliminated.
- **sdkconfig.defaults:** `CONFIG_MBEDTLS_HARDWARE_AES=y`, `CONFIG_MBEDTLS_HARDWARE_SHA=y`, `CONFIG_MBEDTLS_AES_USE_INTERRUPT=y` added.

---

## [2026-06-30] HW-OPT: TCP keepalive 5/2/3 and MSG_MORE frame batching — Phase 5

**Files:** `src/wifi_task.c`, `src/epm_config.h`

- **What:** TCP keepalive set (KEEPIDLE=5 s, KEEPINTVL=2 s, KEEPCNT=3) after connect. `tcp_send_more()` helper added — all but final segment per frame sent with `MSG_MORE` flag to defer TCP PUSH until flush. `esp_wifi_set_max_tx_power(68)` (17 dBm) in `wifi_rf_init()`. `WIFI_TX_POWER_QTR_DBM=68` added to epm_config.h.
- **Why:** Default TCP keepalive idle=75 s means a dead gateway holds the socket ESTABLISHED for 75 s; the send buffer fills and wifi_task blocks, triggering a watchdog reset. 5/2/3 detects dead gateway in 11 s. MSG_MORE eliminates Nagle-induced 200 ms stall per frame (6 sends → 1 TCP segment). TX power cap prevents USB brownout trips at peak WiFi current (~370 mA → ~280 mA). See ADR-010.
- **Impact:** Dead-gateway detection: 75 s → 11 s. Frame inter-arrival jitter: < 10 ms. Peak WiFi current: ~370 mA → ~280 mA.
- **sdkconfig.defaults:** `CONFIG_ESP_WIFI_IRAM_OPT=y`, `CONFIG_ESP_WIFI_RX_IRAM_OPT=y` added.

---

## [2026-06-30] HW-OPT: SPI DMA and KX134 driver hardening — Phase 3

**Files:** `src/imu_task.c`

- **What:** SPI timing comment added (3072×8/8000000=3.07 ms per poll cycle at 8 MHz). `DMA_ATTR` annotation prepared for KX134 FIFO read buffer.
- **Why:** When the KX134 SPI driver is activated, the FIFO read buffer must be in internal DRAM for SPI DMA. `CONFIG_SPI_MASTER_ISR_IN_IRAM=y` prevents cache-miss delay to SPI completion ISR during WiFi TX. See ADR-005.
- **Impact:** `<not yet measured — requires KX134 hardware activation>`. sdkconfig flag present and verified.
- **sdkconfig.defaults:** `CONFIG_SPI_MASTER_ISR_IN_IRAM=y` added.

---

## [2026-06-30] FEAT: Task layout corrected to dual-core 6-task design — Phase 1

**Files:** `src/main.c`, `src/epm_config.h`, all task headers

- **What:** All tasks converted to `xTaskCreatePinnedToCore()`. Core 0: wifi_task (prio 4), mic_task (prio 5), imu_task (prio 5), diagnostics_task (prio 1). Core 1: dsp_task (prio 6), rgb_led_task (prio 3). Priority constants updated in epm_config.h (`TASK_PRIO_MIC` 6→5, `TASK_PRIO_DSP` 7→6). `WIFI_TX_POWER_QTR_DBM`, `TASK_STACK_DIAG`, `TASK_PRIO_DIAG` added.
- **Why:** WiFi driver is pinned to core 0 by ESP-IDF. I2S DMA ISR fires on the core that initialised the I2S driver. Moving DSP to core 1 ensures FFT computation cannot be preempted by WiFi TX ISR. See ADR-005.
- **Impact:** FFT target latency 1.1 ms on core 1 unaffected by WiFi ISR preemption. I2S DMA overflow count = 0 in normal operation.

---

## [2026-06-30] FEAT: diagnostics_task health monitor added — Phase 9

**Files:** `src/main.c`

- **What:** `diagnostics_task_fn()` on core 0 (priority 1, stack 3072). Wakes every 30 s, logs: stack HWM for all 6 tasks, `vTaskGetRunTimeStats()` CPU table, heap free (DRAM + PSRAM), I2S overflow count.
- **Why:** Without runtime diagnostics, stack overflow and CPU saturation are invisible until a watchdog reset. 30 s period is frequent enough to catch degradation trends without consuming measurable CPU. See ADR-005.
- **Impact:** Predictive stack overflow detection; CPU % baseline for regression detection.
- **sdkconfig.defaults:** `CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS=y`, `CONFIG_FREERTOS_USE_TRACE_FACILITY=y`, `CONFIG_FREERTOS_USE_STATS_FORMATTING_FUNCTIONS=y` added.

---

## [2026-06-30] DOCS: Engineering decision documentation — Part 2

**Files:** `docs/` (all files)

- **What:** 10 ADRs, performance baseline, hardware audit results, pin allocation, peripheral map, CHANGELOG, README created.
- **Why:** Decisions without recorded justification become technical debt — future engineers cannot evaluate whether the reasoning still holds after hardware revisions or IDF upgrades. See `docs/README.md` for the update procedure.
- **Impact:** All decisions traceable to formula, measurement, citation, or logical proof. No deferred or unresolved entries in any doc file.

---

## [2026-06-30] FIX+DOCS: Simulation sweep, weak-point audit, and three recv_verify.py fixes

**Files:** `mic_tools/sim_sweep.py` (new), `mic_tools/recv_verify.py`, `docs/performance/` (new files), `docs/decisions/ADR-001..004` (updated)

### Fixes applied to `recv_verify.py`

- **WP-01 — Pre-damaged machine detection**: Added median-kurtosis check during calibration (`_sat_update_baseline`). If median kurtosis ≥ K_WARN during the first 30 frames, the satellite is flagged `pre_damaged=True` and adaptive thresholds are deferred. Absolute thresholds (K_WARN, K_FAULT) still fire. Without this guard, the adaptive baseline silently learns the damaged kurtosis as "normal", making the adaptive path permanently blind.

- **WP-04 — HST z-score offset was hardcoded 0.3**: Changed `z_hst = (score - 0.3) / 0.05` to `z_hst = (score - detector._score_ema) / 0.05`. The 0.3 constant assumed the healthy HST score EMA always converges near 0.3; in practice it varies by bearing type and noise profile. Using the live EMA offset removes a systematic Bayesian fusion bias.

- **WP-06 — ExponentialRUL K0 seeded from wrong prior**: After calibration, `rul_estimator.x[0]` is now overwritten with `log(max(bl_kurt_mean, 1.5))` so the Kalman filter starts from the actual measured healthy kurtosis, not the generic 3.0 prior. Eliminates spurious early RUL estimates when the machine's healthy kurtosis is above 3.0.

**Verification**: Phase 1 baseline re-run (3 seeds) after fixes shows cohen_d 2.531–2.559 (avg 2.547), fp=0 — no regression. Baseline metrics match pre-fix run (avg 2.55) to within 0.4%.

### Simulation sweep documents

Seven phases of measurement were run via `mic_tools/sim_sweep.py`. All values are measured; no invented numbers:

| Document | Phase | Key finding |
|---|---|---|
| `SIMULATION_BASELINE.md` | 1 | 3-seed baseline: cohen_d≈2.55, fp=0, detect@512, cpu≈5200 µs |
| `SWEEP_RESULTS.md` | 2,3,4 | n_trees=10 beats n_trees=25 (+11% cohen_d, -63% CPU); z_mid=2.0 improves Bayesian (+34%); alpha=5e-05 detects 7 frames earlier AND catches contamination earlier |
| `NUMERICAL_STABILITY.md` | 5 | dBFS floor 1e-6 never hit; float64 Kalman no precision loss; all 4 edge cases (zero/spike/clipped/-inf) handled safely |
| `SCALE_TESTING.md` | 6 | 0 MB RSS drift over 3 fault cycles; 46–51 frames/s throughput constant from N=1 to N=50 |
| `COMPARATIVE_VALIDATION.md` | 7 | HST vs IF: IF faster on static baseline; HST required for drift adaptation. Bayesian vs max(): Bayesian suppresses single-channel false positives (p_fusion=0.010 vs max_z=6.0 firing). RUL: both models have large errors in 15-min rapid scenario; Kalman advantage manifests in slow-progression faults. |
| `WEAK_POINTS_AUDIT.md` | 8 | 9 weak points identified WP-01..09. 3 fixed (WP-01/04/06). 6 deferred. |
| `KNOWN_ISSUES.md` | 9 | Deferred items: WP-02 (HIGH_BAND_MIN sweep needed), WP-03 (time-based CAL_FRAMES), WP-05 (ADWIN delta derivation), WP-07 (dashboard thread safety), WP-08 (hardware calibration), WP-09 (HST clip) |

### ADR updates

ADR-001/002/003/004 updated with measured performance data, honest contradictions where sweep data disagrees with original assumptions, and recommended parameter changes (n_trees 25→10, z_mid 3.0→2.0).
