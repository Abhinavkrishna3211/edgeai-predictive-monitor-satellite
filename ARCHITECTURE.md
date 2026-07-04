# EPM System Architecture

**As-built reference for the EdgeAI Predictive Monitor.** Describes what the code does
today (2026-07-04), not aspirational design. Satellite firmware = XIAO ESP32-S3;
gateway = `mic_tools/recv_verify.py` on a Windows laptop / Arduino Uno Q.

---

## 1. End-to-end data flow

```
┌───────────────────────────── XIAO ESP32-S3 satellite ─────────────────────────────┐
│                                                                                    │
│  I2S mic (INMP441/ICS-43434)          KX134 SPI IMU  [STUB — synthetic today]      │
│        │ 16 kHz, 32-bit slots               │ 25.6 kHz, 3 axes                     │
│        ▼                                     ▼                                      │
│  mic_capture.c  (I2S DMA, core 0)      imu_task.c (core 0, prio 3)                  │
│    • DC-remove, normalise               • generate_stub_axis() X/Y/Z               │
│    • RMS/crest/kurtosis (SIMD)          • Hann → 2048-pt FFT ×3 (sequential)        │
│        │ raw_mic_block_t                • power-avg over SPEC_AVG_N                 │
│        ▼  (esp_ringbuf, zero-copy)          │ imu_frame_t                          │
│  mic_task.c (core 0, prio 5) ──▶ raw_q ──▶  │                                       │
│                                             │                                       │
│  dsp_task.c (core 1, prio 6)                │                                       │
│    • Welch overlap (adaptive)               │                                       │
│    • Hann → 1024-pt FFT (SIMD)              │                                       │
│    • power-avg over spec_avg_n              │                                       │
│    • spectral centroid                      │                                       │
│        │ mic_frame_t                        │                                       │
│        ▼ dsp_q (depth-1 overwrite)          ▼ imu_q (depth-1 overwrite)             │
│  ┌──────────────────── wifi_task.c (core 0, prio 4) ─────────────────────┐         │
│  │  recv mic+imu → build 48-B header → pack header‖mic_fft‖imu_xyz        │         │
│  │  AES-128-GCM encrypt (HW accel, TRNG IV) → length-prefixed TCP send    │         │
│  │  read gateway reply (v1 byte / v2 8-B adaptive) → drive RGB LED        │         │
│  └───────────────────────────────┬───────────────────────────────────────┘         │
└──────────────────────────────────│─────────────────────────────────────────────────┘
                                    │  TCP :5100  (AES-128-GCM payloads; Hello plaintext)
                                    ▼
┌───────────────────────── Gateway — recv_verify.py ─────────────────────────────────┐
│  socket accept → satellite_thread(conn)                                            │
│    • Hello (24 B) → register satellite (SQLite satellites table)                   │
│    • per frame: bound payload_bytes → recv_exact → GCM decrypt → parse_frame       │
│    • replay guard (monotonic frame_id)                                             │
│    • features → OnlineDetector (HST) + AdaptiveBaseline z-scores                   │
│                → BayesianFusion posterior P(fault) → RULEstimator                  │
│    • reply: v2 adaptive (alert, P(fault), fft_overlap, spec_avg_n, snapshot req)   │
│    • persist: CSV per-satellite  +  SQLite epm.db (WAL)                            │
│  http.server :8080  ── _DashHandler ──▶ live dashboard                             │
│  zeroconf ── advertises epm-gateway._epm._tcp.local:5100                          │
└────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Firmware task & queue map

Boot order (`app_main`, [main.c](src/main.c)): NVS → netif/event loop → FFT twiddle
table (once, size `FFT_IMU_N`) → RGB task → **WiFi RF init before any I2S/DMA** (avoids
`TG1WDT_SYS_RST` from I2S interrupts during the RF scan) → wait ≤30 s for IP →
mic/dsp/imu/wifi tasks → diagnostics task.

| Task | Core | Prio | Stack | Role |
|---|:--:|:--:|:--:|---|
| `dsp_task` | 1 | 6 | 6144 | 1024-pt mic FFT, Welch overlap, power-avg, centroid |
| `mic_task` | 0 | 5 | 8192 | I2S capture, DC-remove, time-domain stats (SIMD) |
| `imu_task` | 0 | **3** | 3072 | 3× 2048-pt IMU FFT (**stub** signal source) |
| `wifi_task` | 0 | 4 | 16384 | frame assembly, AES-GCM, TCP client, adaptive reply |
| `rgb_led_task` | 1 | 3 | 3072 | LEDC hardware RGB animation (ISR-driven) |
| `diagnostics_task` | 0 | 1 | 3072 | 30 s stack-HWM / heap / I2S-overflow log |

> IMU is priority **3** (below `wifi_task`=4) by deliberate design so the WiFi/TCP
> stack is never starved. Note: the ASCII tables in [main.c:8-18](src/main.c#L8-L18)
> and [epm_config.h:105-113](src/epm_config.h#L105-L113) still say IMU=5 — **stale**
> (see MASTERPLAN Phase 1); the authoritative values are the `#define`s at
> [epm_config.h:129-139](src/epm_config.h#L129-L139).

### Inter-task channels

| Channel | Type | Producer → Consumer | Payload |
|---|---|---|---|
| `raw_q` | `esp_ringbuf` NOSPLIT, 10 KB DRAM, zero-copy | `mic_task` (c0) → `dsp_task` (c1) | `raw_mic_block_t` (~4120 B): DC-removed float block + rms/crest/kurtosis/dc/clip |
| `dsp_q` | queue depth-1, `xQueueOverwrite` | `dsp_task` (c1) → `wifi_task` (c0) | `mic_frame_t`: 512-bin dBFS FFT + stats + centroid |
| `imu_q` | queue depth-1, `xQueueOverwrite` | `imu_task` (c0) → `wifi_task` (c0) | `imu_frame_t`: 3× 1024-bin dBFS FFT + per-axis stats |
| `g_adapt_overlap_pct`, `g_adapt_spec_avg_n` | `volatile uint8_t` (atomic) | `wifi_task` (c0) → `dsp_task`/`mic_task` (c1/c0) | v2 adaptive-sensing feedback, latched at cycle start |
| `g_hst_warmed_up` | `volatile bool` | `dsp_task` (c1) → `wifi_task` (c0) | flips LED from LEARNING → alert-driven at frame 250 |
| RGB state | queue depth-1 + task notify | any task → `rgb_led_task` (c1) | `rgb_led_state_t` |

Cross-boundary summary: FFT-heavy compute lives on **core 1**; capture + radio on
**core 0**. Overwrite queues mean the newest frame always wins (a backlogged consumer
drops stale data rather than blocking a producer).

### Memory placement (deliberate)

- **Internal DRAM (DMA-safe):** AES scratch `s_enc_pt`/`s_enc_ct` (`DMA_ATTR`), I2S
  `raw_buf` (`DMA_ATTR`), mic→dsp ring storage (`DRAM_ATTR`), RGB pattern tables +
  anim state (`DRAM_ATTR`, ISR-reachable when flash cache is off), 16 KB static
  `wifi_task` stack (BSS). GDMA cannot safely reach PSRAM through cache during
  concurrent WiFi DMA, so these stay internal.
- **PSRAM (`EXT_RAM_BSS_ATTR`):** `dsp_task` `s_mag_db` (2 KB), `imu_task` `s_frame`
  (12 KB), and the 128 KB / 4 s snapshot pre-trigger ring (heap-allocated
  `MALLOC_CAP_SPIRAM`).

---

## 3. Wire protocol

All little-endian. Frames are length-prefixed by a `uint32_t payload_bytes` that does
**not** count itself.

**Hello (always plaintext, 24 B)** — `epm_hello_t`: `magic(0xEA1D0000)`, `mac[6]`,
`fw_major`, `fw_minor`, `name[12]` (e.g. `SAT-A3B4`). Registers the node before frames.

**Data frame (encrypted, default `EPM_ENCRYPT_FRAMES`):**
```
[uint32 payload_bytes = 12 + N + 16]
[uint8  iv[12]]           AES-128-GCM nonce, TRNG per frame
[uint8  ciphertext[N]]    enc(epm_header_t ‖ mic_fft ‖ imu_x ‖ imu_y ‖ imu_z)
[uint8  tag[16]]          GCM auth tag
```
Plaintext `N` = `sizeof(epm_header_t)=48` + `mic_bins·4` + `imu_bins·4·3`
(= 48 + 512·4 + 1024·4·3 = **14 384 B** at the current FFT sizes).

**`epm_header_t` (48 B, packed):** `magic(0xEA1DF00D)`, `frame_id`, `timestamp_ms`,
`mic_bins`, `imu_bins`, `mic_rms/crest/dc/kurtosis`, `mic_clip`,
`imu_rms/crest/dc` (max across axes / X-DC), `imu_clip`, `imu_axes(3)`,
`overflow_count`. The gateway parses all but `overflow_count` (`HEADER_FMT` trailing
`x` pad — see MASTERPLAN Phase 2).

**Gateway → satellite reply, read after every frame:**
- **v1** — 1 byte: `0x00` OK / `0x01` WARN / `0x02` FAULT.
- **v2** — 8 B `epm_alert_v2_t`, disambiguated by first byte `0xA2` (`EPM_PROTO_V2_MAGIC`,
  which can't collide with v1's 0x00–0x02): `alert_state`, `fault_posterior` (×10000),
  `fft_overlap_pct` (0/25/50/75), `spec_avg_n` (1..16), `flags`
  (`EPM_SNAPSHOT_REQUEST=0x01`), reserved. The satellite clamps these and updates its
  adaptive-sensing globals; if snapshot is requested it streams the PSRAM ring buffer
  as a length-prefixed raw `int16_t` stream in 4 KB chunks.

**Replay/robustness:** `payload_bytes` is bounded before read; `parse_frame` rejects
short/oversized payloads; a GCM tag failure logs a SECURITY event and keeps the
connection; `frame_id` must strictly increase within a connection (resets per
connection; firmware counter is monotonic across reconnects).

---

## 4. Gateway pipeline (`recv_verify.py`)

Per accepted connection, `satellite_thread(conn, addr)`:

1. **Handshake** — read Hello (`HELLO_FMT`), register/lookup satellite by MAC, create
   per-satellite `SatelliteState` with `AdaptiveBaseline` channels (kurtosis, crest,
   rms, high-band) at `alpha=5e-05` and an `OnlineDetector` (river HalfSpaceTrees,
   `n_trees=10`).
2. **Frame loop** — read `payload_bytes` (bounded) → `recv_exact` →
   `FrameDecryptor.decrypt` (AES-GCM, if `--psk-hex`) → `parse_frame` → replay guard.
3. **Feature extraction** — `_band_ratios` (lo/mid/hi power fractions), spectral
   centroid, header stats; optional ONNX autoencoder reconstruction error (`z_ae`) when
   `--autoencoder` is supplied.
4. **Detection & fusion** — per-channel z-scores from the adaptive baselines +
   HST anomaly score → `BayesianFusion` combines independent likelihood ratios into a
   posterior `P(fault | evidence)` → alert state (OK/WARN/FAULT with streak
   hysteresis) → `RULEstimator` (exponential-degradation 2-state Kalman) updates
   remaining useful life.
5. **Feedback** — build the v2 reply (alert, posterior, adaptive `fft_overlap` /
   `spec_avg_n`, optional snapshot request) and send it back.
6. **Persistence** — append a per-satellite CSV row (`wall_time, frame_id, device_ms,
   rms, kurtosis, crest, z-scores, p_fault, …`) and write events to SQLite `epm.db`
   (WAL mode) via `storage.py`; SECURITY and alert events go to the audit trail.
7. **Observability** — `http.server` dashboard on :8080 (`_DashHandler`), optional Uno
   Q sysfs RGB LEDs, and zeroconf mDNS advertisement of the gateway service.

**Supporting modules:** `online_detector.py` (HST), `bayesian_fusion.py` (posterior
fusion), `adaptive_baseline.py` (per-machine Welford/EMA baseline), `rul_estimator.py`
(Kalman RUL), `storage.py` (SQLite), `fault_models.py` + `satellite_sim.py`
(simulation / testing), `inference.py` / `inference_gpu.py` / `ml_infer.py` /
`ml_trainer.py` + `autoencoder.py` (ONNX autoencoder train/infer).

---

## 5. Build & platform

- **Toolchain:** PlatformIO `[env:xiao_esp32s3]`, `board = seeed_xiao_esp32s3`,
  `framework = espidf` (IDF 5.x). Active sdkconfig = `sdkconfig.xiao_esp32s3`
  (matches the env name); `sdkconfig.defaults` carries the tracked overrides.
- **Flash/PSRAM:** 8 MB flash, Octal PSRAM enabled
  (`CONFIG_SPIRAM=y`, `SPIRAM_MODE_OCT=y`, `ALLOW_BSS_SEG_EXTERNAL_MEMORY=y`),
  single-app factory partition (`partitions_simple_8mb.csv`, OTA off).
- **Key HW offloads:** hardware AES-GCM + SHA, I2S/SPI/LEDC/WiFi ISRs pinned to IRAM
  (cache-off safety during WiFi TX), `WIFI_PS_NONE`, dynamic 240↔80 MHz frequency
  scaling, TX power capped to 17 dBm, TCP `SND_BUF`/`WND` raised to 32 KB for 14 KB
  frames.
- **Upload/monitor:** `esp-builtin` JTAG upload + `monitor_port=COM7` with
  `monitor_dtr=0 / monitor_rts=0` (the HW-01 workaround so opening the port doesn't
  reset the S3 into download mode). `SPEC_AVG_N=4` via `build_flags`.
