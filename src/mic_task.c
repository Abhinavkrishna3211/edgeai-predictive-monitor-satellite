/*
 * mic_task.c — I2S microphone capture task (core 0).
 *
 * Pipeline per block:
 *   1. mic_capture_read_block() — I2S DMA → normalised float block
 *   2. Time-domain stats (RMS, crest, DC, clip)
 *   3. DC removal
 *   4. Kurtosis on DC-removed signal
 *   5. Post raw_mic_block_t to raw_q for dsp_task (core 1)
 *
 * FFT, Welch overlap, and spectral averaging run in dsp_task on core 1,
 * keeping core 0 free for I2S DMA, WiFi driver, and IMU data acquisition.
 */

#include <math.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_log.h"
#include "esp_timer.h"

#include "mic_capture.h"
#include "epm_config.h"
#include "mic_task.h"

static const char *TAG = "mic_task";

static float         s_norm[FFT_MIC_N] __attribute__((aligned(16)));
static QueueHandle_t s_raw_q = NULL;

static void mic_task_fn(void *arg)
{
    (void)arg;

    int fail_cnt = 0;

    /* Enable I2S DMA from this task — DMA interrupt is allocated to CPU0. */
    ESP_ERROR_CHECK(mic_capture_enable());

    float last_rms      = 0.0f;
    float last_crest    = 0.0f;
    float last_kurtosis = 3.0f;
    float last_dc       = 0.0f;
    uint8_t last_clip   = 0;

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

        /* --- 2. Time-domain stats --- */
        mic_block_stats_t st;
        mic_capture_compute_stats(s_norm, FFT_MIC_N, &st);

        float peak_abs = fmaxf(fabsf((float)st.min_sample),
                               fabsf((float)st.max_sample)) / 8388608.0f;
        last_rms   = st.rms;
        last_crest = (st.rms > 1e-8f) ? (peak_abs / st.rms) : 0.0f;
        last_dc    = st.dc_offset;
        last_clip  = (uint8_t)(st.clipped_count > 0 ? 1 : 0);

        /* --- 3. DC removal --- */
        float dc = st.dc_offset;
        for (int i = 0; i < FFT_MIC_N; i++) {
            s_norm[i] -= dc;
        }

        /* --- 4. Kurtosis on zero-mean signal --- */
        {
            float sum2 = 0.0f, sum4 = 0.0f;
            for (int i = 0; i < FFT_MIC_N; i++) {
                float v2 = s_norm[i] * s_norm[i];
                sum2 += v2;
                sum4 += v2 * v2;
            }
            float var = sum2 / FFT_MIC_N;
            if (var > 1e-12f) {
                last_kurtosis = (sum4 / FFT_MIC_N) / (var * var);
            }
        }

        /* --- 5. Post to dsp_task --- */
        static raw_mic_block_t s_blk;
        memcpy(s_blk.samples, s_norm, FFT_MIC_N * sizeof(float));
        s_blk.rms          = last_rms;
        s_blk.crest        = last_crest;
        s_blk.kurtosis     = last_kurtosis;
        s_blk.dc           = last_dc;
        s_blk.clip         = last_clip;
        s_blk.timestamp_ms = (uint32_t)(esp_timer_get_time() / 1000);

        xQueueOverwrite(s_raw_q, &s_blk);
    }
}

QueueHandle_t mic_task_get_raw_queue(void)
{
    return s_raw_q;
}

void mic_task_start(void)
{
    s_raw_q = xQueueCreate(1, sizeof(raw_mic_block_t));
    configASSERT(s_raw_q != NULL);

    ESP_ERROR_CHECK(mic_capture_init());

    ESP_LOGI(TAG, "mic_task starting (capture-only, core 0): "
             "block=%d samples, Fs=%d Hz",
             FFT_MIC_N, MIC_FS_HZ);

    xTaskCreatePinnedToCore(mic_task_fn, "mic_task", TASK_STACK_MIC, NULL,
                            TASK_PRIO_MIC, NULL, 0);
}
