/*
 * epm_protocol.h — Binary wire-format for EPM TCP frames.
 *
 * On-wire layout (little-endian):
 *
 *   [uint32_t payload_bytes]          4 bytes  ← does NOT count itself
 *   [epm_header_t]                   48 bytes
 *   [float mic_fft  [mic_bins]]       mic_bins × 4 bytes
 *   [float imu_x_fft[imu_bins]]       imu_bins × 4 bytes  (radial A)
 *   [float imu_y_fft[imu_bins]]       imu_bins × 4 bytes  (radial B)
 *   [float imu_z_fft[imu_bins]]       imu_bins × 4 bytes  (axial)
 *
 * payload_bytes = sizeof(epm_header_t)
 *               + mic_bins  * 4
 *               + imu_bins  * 4 * imu_axes          (imu_axes = 3)
 *
 * Python unpack string for the header (48 bytes):
 *   struct.unpack('<IIIHHffffBfffBBx', header_bytes)
 *   fields: magic, frame_id, ts, mic_bins, imu_bins,
 *           mic_rms, mic_crest, mic_dc, mic_kurtosis, mic_clip,
 *           imu_rms_max, imu_crest_max, imu_dc_x,
 *           imu_clip, imu_axes
 */

#pragma once

#include <stdint.h>
#include "epm_config.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * epm_header_t — 48-byte packed header.
 *
 * imu_rms and imu_crest carry the per-frame MAX across all three axes so the
 * receiver can threshold on a single number.  Per-axis detail is in the FFT
 * arrays themselves.
 *
 * Byte layout (packed, little-endian):
 *  0  magic         uint32   4
 *  4  frame_id      uint32   4
 *  8  timestamp_ms  uint32   4
 * 12  mic_bins      uint16   2
 * 14  imu_bins      uint16   2   (bins per axis = FFT_IMU_N/2)
 * 16  mic_rms       float    4
 * 20  mic_crest     float    4
 * 24  mic_dc        float    4
 * 28  mic_kurtosis  float    4   (4th moment/var^2 — ISO bearing fault metric)
 * 32  mic_clip      uint8    1
 * 33  imu_rms       float    4   (max across X/Y/Z)
 * 37  imu_crest     float    4   (max across X/Y/Z)
 * 41  imu_dc        float    4   (X-axis DC for reference)
 * 45  imu_clip      uint8    1
 * 46  imu_axes      uint8    1   (= 3 for KX134, 1 for stub compat)
 * 47  _pad          uint8    1
 * Total: 48 bytes
 */
typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t frame_id;
    uint32_t timestamp_ms;
    uint16_t mic_bins;
    uint16_t imu_bins;      /* bins per IMU axis */
    float    mic_rms;
    float    mic_crest;
    float    mic_dc;
    float    mic_kurtosis;  /* 4th statistical moment of zero-mean signal */
    uint8_t  mic_clip;
    float    imu_rms;       /* max(rms_x, rms_y, rms_z) */
    float    imu_crest;     /* max(crest_x, crest_y, crest_z) */
    float    imu_dc;        /* X-axis DC offset */
    uint8_t  imu_clip;
    uint8_t  imu_axes;      /* number of IMU FFT arrays in payload (3) */
    uint8_t  _pad;
} epm_header_t;

_Static_assert(sizeof(epm_header_t) == 48,
               "epm_header_t must be exactly 48 bytes");

/* ── Satellite identification ─────────────────────────────────────────────── */

#define EPM_HELLO_MAGIC  0xEA1D0000UL

/*
 * epm_hello_t — sent by every satellite immediately after TCP connect,
 * before the first data frame.  Lets the gateway register the node.
 *
 * Wire layout (24 bytes, packed, little-endian):
 *   0  magic      uint32  4   EPM_HELLO_MAGIC
 *   4  mac        uint8×6 6   WiFi STA MAC (big-endian order, as read from esp_wifi_get_mac)
 *  10  fw_major   uint8   1
 *  11  fw_minor   uint8   1
 *  12  name       char×12 12  null-padded asset name, e.g. "SAT-A3B4"
 */
typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint8_t  mac[6];
    uint8_t  fw_major;
    uint8_t  fw_minor;
    char     name[12];
} epm_hello_t;

_Static_assert(sizeof(epm_hello_t) == 24, "epm_hello_t must be 24 bytes");

/* 1-byte alert code sent by gateway → satellite after each data frame */
#define EPM_ALERT_OK     0x00   /* normal */
#define EPM_ALERT_WARN   0x01   /* kurtosis or crest factor above WARN threshold */
#define EPM_ALERT_FAULT  0x02   /* kurtosis or crest factor above FAULT threshold */

#ifdef __cplusplus
}
#endif
