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

/* ─── Alert LED ──────────────────────────────────────────────────────────── */

/* Frames before HST z-score baseline is considered valid.
 * wifi_task counts received frames; below this count RGB_CALIBRATING is shown. */
#ifndef LED_CAL_FRAMES
#define LED_CAL_FRAMES  30
#endif

/* ─── Fault thresholds ───────────────────────────────────────────────────── */

/* Consecutive mic_capture_read_block failures before escalating to LOGE.
 * At ~62 ms/block (FFT_MIC_N=1024, Fs=16 kHz) this gives ~3 s before alarm. */
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
 *   Core 0: wifi_task(4), mic_task(5), imu_task(5), diagnostics_task(1)
 *   Core 1: dsp_task(6), rgb_led_task(3)
 *
 *   6 = dsp_task   : compute-bound, must complete FFT before next DMA buffer fills
 *   5 = mic/imu    : DMA callbacks, must service within DMA_FRAME_NUM/sample_rate
 *   4 = wifi_task  : TCP blocking I/O, preemptible by capture tasks
 *   3 = rgb_led    : nearly always blocked on queue/notify, lowest real-time need
 *   1 = diagnostics: background HWM monitoring, runs every 30 s, never time-critical
 *
 * Stack sizes are set conservatively above spec minimums:
 *   mic=8192 (spec 4096) — float kurtosis buffers on task stack safety margin
 *   dsp=16384 (spec 8192) — FFT + feature compute on core 1, no headroom issues
 *   imu=8192 (spec 4096) — 3-axis FFT, cosf(), safety margin
 *   wifi=10240 (spec 6144) — mbedTLS GCM + mDNS + TCP + nvs overhead
 *   diag=3072 (spec 3072) — only vTaskGetRunTimeStats 512-byte static buffer
 */
#define TASK_STACK_MIC   8192
#define TASK_STACK_DSP   16384
#define TASK_STACK_IMU   8192
#define TASK_STACK_WIFI  10240
#define TASK_STACK_DIAG  3072

#define TASK_PRIO_MIC    5   /* I2S DMA callback — must not be starved by DSP */
#define TASK_PRIO_DSP    6   /* FFT compute — highest: must drain raw_rb before next block */
#define TASK_PRIO_IMU    5   /* SPI DMA capture (stub) */
#define TASK_PRIO_WIFI   4   /* TCP I/O — preemptible by DMA tasks */
#define TASK_PRIO_DIAG   1   /* background health monitor */

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
    float    kurtosis;               /* 4th moment / variance^2 — ISO bearing   */
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
