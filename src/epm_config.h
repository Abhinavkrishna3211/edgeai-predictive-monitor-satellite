/*
 * epm_config.h — Compile-time configuration for the EPM firmware.
 *
 * All #defines can be overridden via build_flags in platformio.ini:
 *   build_flags = -DFFT_MIC_N=2048 -DSERVER_PORT=5200
 */

#pragma once

#include <stdint.h>
#include <stdbool.h>

/* Pull in wifi_creds.h if it exists (defines WIFI_SSID, WIFI_PASS, SERVER_IP,
 * SERVER_PORT).  This file is gitignored; credentials stay out of build flags
 * and work correctly even when the SSID/password contain spaces or symbols. */
#if __has_include("wifi_creds.h")
#include "wifi_creds.h"
#endif

/* ─── FFT / averaging ────────────────────────────────────────────────────── */

#ifndef FFT_MIC_N
#define FFT_MIC_N   1024    /* microphone FFT window size — must be power-of-2 */
#endif

#ifndef FFT_IMU_N
#define FFT_IMU_N   2048    /* IMU FFT window size — must be power-of-2       */
#endif

#ifndef SPEC_AVG_N
#define SPEC_AVG_N  4       /* spectral frames to average before sending      */
#endif
#if SPEC_AVG_N <= 0
#error "SPEC_AVG_N must be > 0 (division by zero in mic_task / imu_task)"
#endif

/* ─── Sample rates ───────────────────────────────────────────────────────── */

#ifndef MIC_FS_HZ
#define MIC_FS_HZ   16000   /* I2S mic ODR (Hz)                               */
#endif

#ifndef IMU_FS_HZ
#define IMU_FS_HZ   25600   /* KX134 ODR — configure register at runtime too  */
#endif

/* ─── WiFi / network ─────────────────────────────────────────────────────── */

#ifndef WIFI_SSID
#define WIFI_SSID   "EPM_Hotspot"
#endif

#ifndef WIFI_PASS
#define WIFI_PASS   "epm12345"
#endif

#ifndef SERVER_IP
/* Common defaults:
 *   Android hotspot : 192.168.43.1
 *   iPhone hotspot  : 172.20.10.1
 *   Windows hotspot : 192.168.137.1
 *   macOS hotspot   : 192.168.2.1
 */
#define SERVER_IP   "192.168.43.1"
#endif

#ifndef SERVER_PORT
#define SERVER_PORT 5100
#endif

/* ─── Wire-format ────────────────────────────────────────────────────────── */

#define EPM_MAGIC   0xEA1DF00DUL

/* ─── MQTT telemetry (Phase 0.5 — additive alongside the TCP path above) ──── *
 * See docs/decisions/ADR-011-mqtt-transport-added.md. Broker host/port are
 * NOT here: they're private to components/epm_drivers/link_mqtt.c (this
 * header belongs to the main component only; the driver component must not
 * depend back on it — src already depends on epm_drivers). These are the
 * values src/threads/net_task.c needs to build the synthetic section-list
 * frame each publish cycle. */

#ifndef EPM_MODEL_SPECTRUM_BINS
#define EPM_MODEL_SPECTRUM_BINS 128 /* bins per channel expected by the base station's model */
#endif

#ifndef EPM_MIC_FS_HZ
#define EPM_MIC_FS_HZ    48000.0f
#endif
#ifndef EPM_MIC_FFT_SIZE
#define EPM_MIC_FFT_SIZE 2048
#endif

#ifndef EPM_ACCEL_FS_HZ
#define EPM_ACCEL_FS_HZ    6400.0f
#endif
#ifndef EPM_ACCEL_FFT_SIZE
#define EPM_ACCEL_FFT_SIZE 1024
#endif

#ifndef EPM_NET_PUBLISH_INTERVAL_MS
#define EPM_NET_PUBLISH_INTERVAL_MS 200 /* matches the reference satellite's FUSER_EPOCH_MS */
#endif

#ifndef EPM_NET_FRAME_BUF_BYTES
#define EPM_NET_FRAME_BUF_BYTES 4096 /* 5-section synthetic frame is 2251 B; ample headroom */
#endif

/* ─── Alert LED ──────────────────────────────────────────────────────────── */

/* Frames before HST z-score baseline is considered valid.
 * wifi_task counts received frames; below this count RGB_CALIBRATING is shown. */
#ifndef LED_CAL_FRAMES
#define LED_CAL_FRAMES  30
#endif

/* ─── Fault thresholds ───────────────────────────────────────────────────── */

/* Consecutive mic_capture_read_block failures before escalating to LOGE.
 * At ~64 ms/block (FFT_MIC_N=1024, Fs=16 kHz) this gives ~3.2 s before alarm. */
#ifndef MIC_FAIL_MAX
#define MIC_FAIL_MAX  50
#endif

/* ─── WiFi TX power ──────────────────────────────────────────────────────── */

/* HW-OPT: WiFi TX power cap — limits peak current on 3.3V rail (XIAO USB-C).
 * 68 = 17 dBm (units: quarter-dBm). ESP32-S3 max is 20 dBm (80).
 * Reducing from 20 → 17 dBm cuts TX current ~30% (from ~310 mA peak to ~220 mA)
 * with negligible range loss at <10m industrial sensor deployment distance.
 * To restore max range: set WIFI_TX_POWER_QTR_DBM=80. */
#ifndef WIFI_TX_POWER_QTR_DBM
#define WIFI_TX_POWER_QTR_DBM  68
#endif

/* ─── FreeRTOS task sizing ───────────────────────────────────────────────── */

/*
 * Priority hierarchy (corrected — 5-task + diagnostics layout):
 *   Core 0: wifi_task(4), mic_task(5), imu_task(3), diagnostics_task(1)
 *   Core 1: dsp_task(6), rgb_led_task(3)
 *
 *   6 = dsp_task   : compute-bound, must complete FFT before next DMA buffer fills
 *   5 = mic_task   : DMA callbacks, must service within DMA_FRAME_NUM/sample_rate
 *   4 = wifi_task  : TCP blocking I/O, preemptible by capture tasks
 *   3 = imu/rgb_led: imu_task is kept below wifi_task so the WiFi/TCP stack is
 *                    never starved; rgb_led is nearly always blocked on queue/notify
 *   1 = diagnostics: background HWM monitoring, runs every 30 s, never time-critical
 *
 * Stack sizes are set conservatively above spec minimums:
 *   mic=8192  (spec 4096) — float kurtosis buffers on task stack safety margin
 *   dsp=6144  (HWM 2004) — FFT compute on core 1; was 16384 but 93% wasted;
 *                          6144 = 3× measured peak; saves 10240 bytes of heap
 *   imu=3072  (HWM 968)  — 3-axis FFT stub; was 8192 but 88% wasted;
 *                          3072 = 3× measured peak; saves 5120 bytes of heap
 *   wifi=16384 (was 10240) — mbedTLS GCM + TCP + NVS; old 10240 overflowed at 91%;
 *                            16 KB needs heap freed by DSP/IMU reductions to allocate
 *   diag=3072 (spec 3072) — only vTaskGetRunTimeStats 1024-byte static buffer
 *
 * DSP+IMU reduction frees 15360 bytes of heap so wifi_task's 16640-byte
 * (stack+TCB) allocation succeeds. Without this, xTaskCreatePinnedToCore()
 * fails silently leaving s_task_handle=NULL and no data is ever sent.
 */
#define TASK_STACK_MIC   8192
#define TASK_STACK_DSP   6144
#define TASK_STACK_IMU   3072
#define TASK_STACK_WIFI  16384
#define TASK_STACK_DIAG  3072
#define TASK_STACK_NET   4096  /* net_task: blocks on WiFi, then esp-mqtt publish loop */

#define TASK_PRIO_MIC    5   /* I2S DMA callback — must not be starved by DSP */
#define TASK_PRIO_DSP    6   /* FFT compute — highest: must drain raw_rb before next block */
#define TASK_PRIO_IMU    3   /* SPI DMA capture — below wifi_task(4) so WiFi stack is never starved */
#define TASK_PRIO_WIFI   4   /* TCP I/O — preemptible by DMA tasks */
#define TASK_PRIO_DIAG   1   /* background health monitor */
#define TASK_PRIO_NET    4   /* MQTT publish — same tier as wifi_task, also radio-side I/O */

/* ─── Inter-task data structures ─────────────────────────────────────────── */

/*
 * raw_mic_block_t — one DMA capture block: mic_task → dsp_task handoff.
 *
 * Contains the DC-removed normalised float block plus the time-domain stats
 * computed by mic_task.  dsp_task reads from the raw_q queue, applies the
 * Welch/Hann/FFT pipeline, and emits a mic_frame_t after SPEC_AVG_N blocks.
 */
typedef struct {
    float    samples[FFT_MIC_N];  /* DC-removed, aligned(16) for SIMD */
    float    rms;
    float    crest;
    float    kurtosis;
    float    dc;
    uint8_t  clip;
    uint32_t timestamp_ms;
} raw_mic_block_t;

/*
 * mic_frame_t — one averaged FFT frame from the microphone task.
 * fft_db[0] = DC bin, explicitly set to -120 dBFS after DC removal.
 */
typedef struct {
    float    fft_db[FFT_MIC_N / 2]; /* averaged power spectrum in dBFS         */
    float    rms;                    /* RMS of AC (DC-removed) block            */
    float    crest;                  /* peak/RMS — impulse fault indicator      */
    float    kurtosis;               /* excess/Fisher, ADR-018 (Gaussian ≈ 0)   */
    float    dc;                     /* DC offset of last block                 */
    float    spectral_centroid;      /* Σ(f_i·P_i)/Σ(P_i) Hz — texture metric  */
    uint8_t  clip;                   /* 1 if any sample hit full-scale          */
    uint32_t timestamp_ms;
} mic_frame_t;

/*
 * imu_frame_t — three independent averaged FFT frames from the IMU task.
 *
 * Axis convention (matches KX134 datasheet when flat-mounted with USB up):
 *   X — radial direction A (perpendicular to shaft)
 *   Y — radial direction B (perpendicular to shaft, 90° from X)
 *   Z — axial direction    (parallel to shaft / thrust axis)
 *
 * Fault mapping:
 *   Imbalance           → X, Y (radial 1× shaft harmonic)
 *   Misalignment        → Z (axial 2× shaft harmonic) + radial
 *   Bearing inner race  → X, Y (BPFI = n/2 × shaft × (1 + d/D cos θ))
 *   Bearing outer race  → X, Y (BPFO = n/2 × shaft × (1 - d/D cos θ))
 *   Looseness           → X, Y (subharmonics, broadband)
 *   Thrust wear         → Z dominant
 *
 * The AI model on the Uno Q sees [fft_z | fft_x | fft_y | mic_fft] as one
 * concatenated feature vector.  Cross-axis correlations are learnable.
 */
/* ─── Shared runtime state ───────────────────────────────────────────────── */

/* Set by dsp_task when HST warm-up frame 250 is reached.
 * Read by wifi_task to switch LED from RGB_LEARNING → alert-driven states.
 * uint8_t-width write on Xtensa is atomic; volatile ensures visibility. */
extern volatile bool g_hst_warmed_up;

typedef struct {
    float    fft_x[FFT_IMU_N / 2];  /* X axis radial FFT in dBFS           */
    float    fft_y[FFT_IMU_N / 2];  /* Y axis radial FFT in dBFS           */
    float    fft_z[FFT_IMU_N / 2];  /* Z axis axial  FFT in dBFS           */
    float    rms_x, rms_y, rms_z;   /* per-axis RMS                        */
    float    crest_x, crest_y, crest_z; /* per-axis crest factor           */
    float    dc_x;                   /* X-axis DC offset (gravity component)*/
    uint8_t  clip;                   /* 1 if any axis clipped               */
    uint32_t timestamp_ms;
} imu_frame_t;
