/*
 * epm_config.h — Compile-time configuration for the EPM firmware.
 *
 * All #defines can be overridden via build_flags in platformio.ini:
 *   build_flags = -DFFT_MIC_N=2048 -DSERVER_PORT=5200
 */

#pragma once

#include <stdint.h>

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

/* ── Single built-in LED (default) ──────────────────────────────────────────
 * GPIO21 = built-in user LED on XIAO ESP32-S3 (active-low: LOW = ON).
 * Driven by a 100 ms esp_timer — 5 distinct patterns (see led_task.h).   */
#define ALERT_LED_PIN   21

/* ── External RGB LED upgrade ────────────────────────────────────────────────
 * When you connect a common-cathode RGB LED, set EPM_LED_RGB=1 either here
 * or via build_flags in platformio.ini (-DEPM_LED_RGB=1) and choose three
 * free GPIOs for R, G, B.  led_task.c will switch to colour-based signalling
 * automatically — no other code changes needed.
 *
 * Suggested wiring for XIAO ESP32-S3:
 *   GPIO 1 (D0/A0) → 220 Ω → Red   anode
 *   GPIO 2 (D1/A1) → 220 Ω → Green anode
 *   GPIO 3 (D2/A2) → 220 Ω → Blue  anode
 *   GND                   → Common cathode
 *
 * Colour map (5 states, full list in led_task.h):
 *   BOOT=White  CONNECTING=Blue  OK=Green  WARN=Yellow  FAULT=Red          */
#ifndef EPM_LED_RGB
#define EPM_LED_RGB   0   /* 0 = single built-in LED,  1 = external RGB LED */
#endif

#ifndef LED_PIN_R
#define LED_PIN_R     1   /* GPIO for Red   (active-high, 3.3 V through 220 Ω) */
#endif
#ifndef LED_PIN_G
#define LED_PIN_G     2   /* GPIO for Green */
#endif
#ifndef LED_PIN_B
#define LED_PIN_B     3   /* GPIO for Blue  */
#endif

/* ─── Fault thresholds ───────────────────────────────────────────────────── */

/* Consecutive mic_capture_read_block failures before escalating to LOGE.
 * At ~62 ms/block (FFT_MIC_N=1024, Fs=16 kHz) this gives ~3 s before alarm. */
#ifndef MIC_FAIL_MAX
#define MIC_FAIL_MAX  50
#endif

/* ─── FreeRTOS task sizing ───────────────────────────────────────────────── */

#define TASK_STACK_MIC   8192
#define TASK_STACK_DSP   16384   /* FFT + feature buffers on core 1 */
#define TASK_STACK_IMU   8192
#define TASK_STACK_WIFI  10240

#define TASK_PRIO_MIC    6   /* higher than IMU — DMA overrun costs a whole block */
#define TASK_PRIO_DSP    7   /* must drain raw_q before the next block arrives */
#define TASK_PRIO_IMU    5
#define TASK_PRIO_WIFI   4

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
    float    fft_db[FFT_MIC_N / 2]; /* averaged power spectrum in dBFS    */
    float    rms;                    /* RMS of last block [-1, 1]          */
    float    crest;                  /* peak/RMS — impulse fault indicator */
    float    kurtosis;               /* 4th moment / variance^2 — ISO bearing fault metric */
    float    dc;                     /* DC offset of last block            */
    uint8_t  clip;                   /* 1 if any sample hit full-scale     */
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
