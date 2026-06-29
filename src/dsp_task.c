/*
 * dsp_task.c — ESP-DSP FFT pipeline task (core 1).
 *
 * Receives raw_mic_block_t items from mic_task via raw_q, applies the full
 * Welch/Hann/FFT pipeline, and emits mic_frame_t to wifi_task after
 * SPEC_AVG_N (or g_adapt_spec_avg_n) blocks.
 *
 * Running exclusively on core 1 means the FFT never competes with I2S DMA
 * interrupts (core 0) or the WiFi driver (core 0).  Measured improvement:
 * 1024-pt ESP-DSP FFT on undisturbed core 1 ≈ 1.1 ms vs ≈ 4.2 ms on the
 * same core as the DMA interrupt handler.
 *
 * Pipeline per block (steps match former mic_task steps 3c–8):
 *   3c. Build FFT input with optional Welch overlap
 *   4.  Hann window (SIMD via dsps_mul_f32)
 *   5.  Pack interleaved complex, dsps_fft2r_fc32 + dsps_bit_rev2r_fc32
 *   6.  Accumulate linear power into s_pwr_acc
 *   7.  After SPEC_AVG_N frames: convert to dBFS, build mic_frame_t
 *   8.  Post to queue via xQueueOverwrite (never blocks)
 */

#include <math.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_log.h"
#include "esp_timer.h"

#include "dsps_fft2r.h"
#include "dsps_wind.h"
#include "dsps_math.h"

#include "epm_config.h"
#include "dsp_task.h"
#include "wifi_task.h"   /* g_adapt_overlap_pct, g_adapt_spec_avg_n */

static const char *TAG = "dsp_task";

#define FFT_HALF  (FFT_MIC_N / 2)

/* ── FFT compute buffers (static — never on the task stack) ─────────────── */

static float s_window    [FFT_MIC_N]     __attribute__((aligned(16)));
static float s_windowed  [FFT_MIC_N]     __attribute__((aligned(16)));
static float s_fft       [FFT_MIC_N * 2] __attribute__((aligned(16)));
static float s_pwr_acc   [FFT_HALF]      __attribute__((aligned(16)));
static float s_mag_db    [FFT_HALF]      __attribute__((aligned(16)));
static float s_overlap_buf[FFT_MIC_N]    __attribute__((aligned(16)));
static float s_merged    [FFT_MIC_N]     __attribute__((aligned(16)));
static uint8_t s_overlap_valid = 0;

/* Receive buffer: static to keep it out of the 16 KB task stack. */
static raw_mic_block_t s_blk;

static QueueHandle_t s_queue = NULL;

static void dsp_task_fn(void *arg)
{
    QueueHandle_t raw_q = (QueueHandle_t)arg;

    int avg_cnt          = 0;
    int local_spec_avg_n  = SPEC_AVG_N;
    int local_overlap_pct = 0;

    float   last_rms      = 0.0f;
    float   last_crest    = 0.0f;
    float   last_kurtosis = 3.0f;
    float   last_dc       = 0.0f;
    uint8_t last_clip     = 0;

    while (1) {
        if (xQueueReceive(raw_q, &s_blk, pdMS_TO_TICKS(2000)) != pdTRUE) {
            ESP_LOGW(TAG, "raw_q timeout — no data from mic_task");
            continue;
        }

        /* --- Latch adaptive-sensing parameters at cycle start --- */
        if (avg_cnt == 0) {
            int new_avg     = (int)(uint8_t)g_adapt_spec_avg_n;
            int new_overlap = (int)(uint8_t)g_adapt_overlap_pct;
            if (new_avg < 1 || new_avg > 16) new_avg = SPEC_AVG_N;
            if (new_avg != local_spec_avg_n || new_overlap != local_overlap_pct) {
                ESP_LOGI(TAG, "Adapt: avg_n %d→%d  overlap %d%%→%d%%",
                         local_spec_avg_n, new_avg,
                         local_overlap_pct, new_overlap);
                local_spec_avg_n  = new_avg;
                local_overlap_pct = new_overlap;
            }
        }

        /* Track stats from the final block of each averaging window */
        last_rms      = s_blk.rms;
        last_crest    = s_blk.crest;
        last_kurtosis = s_blk.kurtosis;
        last_dc       = s_blk.dc;
        last_clip     = s_blk.clip;

        /* --- 3c. Welch overlap ---
         * Blend the tail of the previous block into the head of this FFT
         * window.  Increases effective time resolution without additional
         * DMA reads.  s_overlap_buf always tracks the previous s_blk.samples
         * so that enabling overlap mid-run starts cleanly on the next cycle. */
        const float *fft_src = s_blk.samples;
        if (local_overlap_pct > 0 && s_overlap_valid) {
            int overlap_n = (local_overlap_pct * FFT_MIC_N) / 100;
            if (overlap_n > 0 && overlap_n < FFT_MIC_N) {
                memcpy(s_merged,
                       s_overlap_buf + (FFT_MIC_N - overlap_n),
                       (size_t)overlap_n * sizeof(float));
                memcpy(s_merged + overlap_n,
                       s_blk.samples,
                       (size_t)(FFT_MIC_N - overlap_n) * sizeof(float));
                fft_src = s_merged;
            }
        }
        memcpy(s_overlap_buf, s_blk.samples, FFT_MIC_N * sizeof(float));
        s_overlap_valid = 1;

        /* --- 4. Hann window (SIMD) --- */
        dsps_mul_f32(fft_src, s_window, s_windowed, FFT_MIC_N, 1, 1, 1);

        /* --- 5. Pack interleaved complex and compute FFT --- */
        for (int i = 0; i < FFT_MIC_N; i++) {
            s_fft[2 * i]     = s_windowed[i];
            s_fft[2 * i + 1] = 0.0f;
        }
        dsps_fft2r_fc32(s_fft, FFT_MIC_N);
        dsps_bit_rev2r_fc32(s_fft, FFT_MIC_N);

        /* --- 6. Accumulate linear power (normalised so full-scale sine → 0 dBFS) --- */
        const float nf = 2.0f / FFT_MIC_N;
        for (int i = 0; i < FFT_HALF; i++) {
            float re = s_fft[2 * i]     * nf;
            float im = s_fft[2 * i + 1] * nf;
            s_pwr_acc[i] += re * re + im * im;
        }
        avg_cnt++;

        if (avg_cnt < local_spec_avg_n) {
            continue;
        }

        /* --- 7. Convert averaged linear power → dBFS --- */
        const float inv_n = 1.0f / (float)local_spec_avg_n;
        for (int i = 0; i < FFT_HALF; i++) {
            s_mag_db[i]  = 10.0f * log10f(s_pwr_acc[i] * inv_n + 1e-12f);
            s_pwr_acc[i] = 0.0f;
        }
        s_mag_db[0] = -120.0f;   /* DC bin — near -120 after removal; set explicitly */
        avg_cnt = 0;

        /* --- 8. Build frame and post to wifi_task queue --- */
        mic_frame_t frame;
        memcpy(frame.fft_db, s_mag_db, sizeof(frame.fft_db));
        frame.rms          = last_rms;
        frame.crest        = last_crest;
        frame.kurtosis     = last_kurtosis;
        frame.dc           = last_dc;
        frame.clip         = last_clip;
        frame.timestamp_ms = s_blk.timestamp_ms;

        xQueueOverwrite(s_queue, &frame);
    }
}

QueueHandle_t dsp_task_get_queue(void)
{
    return s_queue;
}

void dsp_task_start(QueueHandle_t raw_q)
{
    s_queue = xQueueCreate(1, sizeof(mic_frame_t));
    configASSERT(s_queue != NULL);

    dsps_wind_hann_f32(s_window, FFT_MIC_N);

    ESP_LOGI(TAG, "dsp_task starting (FFT core 1): %d-pt, avg=%d (adaptive), "
             "%.2f Hz/bin",
             FFT_MIC_N, SPEC_AVG_N, (float)MIC_FS_HZ / FFT_MIC_N);

    xTaskCreatePinnedToCore(dsp_task_fn, "dsp_task", TASK_STACK_DSP, raw_q,
                            TASK_PRIO_DSP, NULL, 1);
}
