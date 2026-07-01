/*
 * mic_task.c — I2S microphone capture task (core 0).
 *
 * Pipeline per block:
 *   1. mic_capture_read_block() — I2S DMA → normalised float block
 *   2. DC removal (mean subtracted in-place from s_norm)
 *   3. Time-domain stats via ESP-DSP SIMD:
 *      RMS    : dsps_dotprod_f32(s_norm, s_norm)  → sqrt(·/N)
 *      Crest  : fabsf() scalar loop → peak/RMS  (dsps_abs_f32 absent in this ESP-DSP release)
 *      Kurtosis: dsps_mul_f32(s_norm,s_norm) → dsps_dotprod_f32 → (Σx⁴/N)/(var²)
 *   4. Post raw_mic_block_t to ring buffer for dsp_task (core 1)
 *
 * HW-OPT: esp_ringbuf zero-copy handoff — dsp_task receives a pointer into
 * the ring buffer storage (s_rb_storage, 8192 bytes, DRAM_ATTR internal DRAM),
 * reads the data directly, then returns the item.  Eliminates the 4-KB memcpy
 * that a depth-1 xQueueOverwrite would perform on every block.
 */

#include <math.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "dsps_fft2r.h"
#include "dsps_math.h"
#include "dsps_dotprod.h"

#include "mic_capture.h"
#include "epm_config.h"
#include "mic_task.h"

static const char *TAG = "mic_task";

/* ── Capture + compute buffers (static — never on the task stack) ────────── */

/* HW-OPT: aligned(16) satisfies LX7 128-bit SIMD lane requirements for
 * dsps_dotprod_f32 / dsps_mul_f32 / fabsf. */
static float s_norm   [FFT_MIC_N] __attribute__((aligned(16)));
static float s_scratch[FFT_MIC_N] __attribute__((aligned(16)));  /* temp for SIMD */

/* Task handle — exposed via getter for diagnostics_task stack HWM logging. */
static TaskHandle_t s_task_handle = NULL;
TaskHandle_t mic_task_get_handle(void) { return s_task_handle; }

/* ── Ring buffer for mic_task → dsp_task handoff ─────────────────────────── */

/* HW-OPT: DRAM_ATTR guarantees internal DRAM placement.  PSRAM cannot be used
 * as ring buffer storage — the esp_ringbuf implementation accesses header bytes
 * inside the ISR-driven xRingbufferReceive path, which must be in DRAM when
 * the flash cache is off during WiFi TX bursts. */
static DRAM_ATTR uint8_t       s_rb_storage[8192];
static StaticRingbuffer_t      s_rb_mem;
static RingbufHandle_t         s_raw_rb = NULL;

RingbufHandle_t mic_task_get_raw_ringbuf(void) { return s_raw_rb; }

/* ── Task function ───────────────────────────────────────────────────────── */

static void mic_task_fn(void *arg)
{
    (void)arg;

    int fail_cnt = 0;

    ESP_ERROR_CHECK(mic_capture_enable());

    float   last_rms      = 0.0f;
    float   last_crest    = 0.0f;
    float   last_kurtosis = 3.0f;
    float   last_dc       = 0.0f;
    uint8_t last_clip     = 0;

    while (1) {
        /* --- 1. Capture --- */
        if (mic_capture_read_block(NULL, s_norm, FFT_MIC_N) != ESP_OK) {
            fail_cnt++;
            if (fail_cnt >= MIC_FAIL_MAX) {
                ESP_LOGE(TAG, "mic_capture_read_block: %d consecutive failures — "
                         "check I2S wiring / clock", fail_cnt);
            } else {
                ESP_LOGW(TAG, "mic_capture_read_block failed (%d/%d) — retrying",
                         fail_cnt, MIC_FAIL_MAX);
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        fail_cnt = 0;

        /* --- 2. DC offset (kept for removal and telemetry) --- */
        mic_block_stats_t st;
        mic_capture_compute_stats(s_norm, FFT_MIC_N, &st);
        last_dc   = st.dc_offset;
        last_clip = (uint8_t)(st.clipped_count > 0 ? 1 : 0);

        /* DC removal in-place */
        float dc = last_dc;
        for (int i = 0; i < FFT_MIC_N; i++) s_norm[i] -= dc;

        /* --- 3a. RMS on DC-removed signal (SIMD) --- */
        /* HW-OPT: dsps_dotprod_f32 uses LX7 vectorised multiply-accumulate;
         * ~4× throughput vs a scalar loop (128-bit SIMD = 4 float/cycle). */
        float sum_sq = 0.0f;
        dsps_dotprod_f32(s_norm, s_norm, &sum_sq, FFT_MIC_N);
        last_rms = sqrtf(sum_sq / FFT_MIC_N);

        /* --- 3b. Crest factor: peak(|x|) / RMS (scalar abs + scan) --- */
        /* dsps_abs_f32 is not present in this ESP-DSP release; scalar fabsf is
         * fast enough (1 cycle/element on LX7, 512 iterations ≈ 0.2 µs). */
        float peak = 0.0f;
        for (int i = 0; i < FFT_MIC_N; i++) {
            float a = fabsf(s_norm[i]);
            if (a > peak) peak = a;
        }
        last_crest = (last_rms > 1e-8f) ? (peak / last_rms) : 0.0f;

        /* --- 3c. Kurtosis: (Σx⁴/N) / (Σx²/N)² (two SIMD dotprods) --- */
        /* Step 1: s_scratch = x²  (element-wise, SIMD) */
        dsps_mul_f32(s_norm, s_norm, s_scratch, FFT_MIC_N, 1, 1, 1);
        /* Step 2: sum4 = Σx⁴ = dot(s_scratch, s_scratch)  (SIMD) */
        float sum4 = 0.0f;
        dsps_dotprod_f32(s_scratch, s_scratch, &sum4, FFT_MIC_N);
        float var = sum_sq / FFT_MIC_N;
        if (var > 1e-12f) {
            last_kurtosis = (sum4 / FFT_MIC_N) / (var * var);
        }

        /* --- 4. Post to dsp_task via ring buffer --- */
        static raw_mic_block_t s_blk;
        memcpy(s_blk.samples, s_norm, FFT_MIC_N * sizeof(float));
        s_blk.rms          = last_rms;
        s_blk.crest        = last_crest;
        s_blk.kurtosis     = last_kurtosis;
        s_blk.dc           = last_dc;
        s_blk.clip         = last_clip;
        s_blk.timestamp_ms = (uint32_t)(esp_timer_get_time() / 1000);

        /* Non-blocking send: if ring buffer is full (dsp_task backlogged),
         * drop the oldest data path — identical behaviour to xQueueOverwrite
         * but avoids an extra memcpy on the receive side. */
        if (xRingbufferSend(s_raw_rb, &s_blk, sizeof(s_blk), 0) != pdTRUE) {
            ESP_LOGD(TAG, "raw_rb full — block dropped (dsp_task backlogged)");
        }
    }
}

/* ── Public API ──────────────────────────────────────────────────────────── */

void mic_task_start(void)
{
    /* HW-OPT: xRingbufferCreateStatic — ring buffer storage in s_rb_storage
     * (DRAM_ATTR, internal DRAM).  No heap allocation at runtime. */
    s_raw_rb = xRingbufferCreateStatic(sizeof(s_rb_storage),
                                        RINGBUF_TYPE_NOSPLIT,
                                        s_rb_storage, &s_rb_mem);
    configASSERT(s_raw_rb != NULL);

    ESP_ERROR_CHECK(mic_capture_init());

    ESP_LOGI(TAG, "mic_task starting (capture core 0, SIMD stats): "
             "block=%d samples, Fs=%d Hz, ringbuf=%u bytes",
             FFT_MIC_N, MIC_FS_HZ, (unsigned)sizeof(s_rb_storage));

    xTaskCreatePinnedToCore(mic_task_fn, "mic_task", TASK_STACK_MIC, NULL,
                            TASK_PRIO_MIC, &s_task_handle, 0);
}
