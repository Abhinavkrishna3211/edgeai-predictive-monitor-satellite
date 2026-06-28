/*
 * mic_task.c — I2S microphone capture + FFT task.
 *
 * Pipeline per block:
 *   1. mic_capture_read_block() — I2S DMA → normalised float block
 *   2. Time-domain stats (RMS, crest, DC, clip)
 *   3. DC removal
 *   4. Hann window (SIMD via dsps_mul_f32)
 *   5. Pack interleaved complex, dsps_fft2r_fc32 + dsps_bit_rev2r_fc32
 *   6. Accumulate linear power into s_pwr_acc
 *   7. After SPEC_AVG_N frames: convert to dBFS, build mic_frame_t,
 *      post to queue via xQueueOverwrite (never blocks)
 *
 * Bin resolution: MIC_FS_HZ / FFT_MIC_N = 15.625 Hz/bin (at defaults).
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

#include "mic_capture.h"
#include "epm_config.h"
#include "mic_task.h"
#include "wifi_task.h"    /* g_adapt_overlap_pct, g_adapt_spec_avg_n */

/* ---------- module constants ---------- */

static const char *TAG = "mic_task";

#define FFT_HALF    (FFT_MIC_N / 2)

/* ---------- static FFT buffers (aligned for SIMD) ---------- */

static float s_norm[FFT_MIC_N]        __attribute__((aligned(16)));
static float s_window[FFT_MIC_N]      __attribute__((aligned(16)));
static float s_windowed[FFT_MIC_N]    __attribute__((aligned(16)));
static float s_fft[FFT_MIC_N * 2]     __attribute__((aligned(16)));
static float s_pwr_acc[FFT_HALF]      __attribute__((aligned(16)));
static float s_mag_db[FFT_HALF]       __attribute__((aligned(16)));

/*
 * Overlap buffer for Welch's windowed FFT method.
 *
 * When g_adapt_overlap_pct > 0, the last (overlap_pct/100 * FFT_MIC_N) samples
 * from the previous capture block are prepended to the next FFT window.  The
 * first DMA block populates s_overlap_buf[] and sets s_overlap_valid = 1; from
 * then on each FFT window is formed as:
 *
 *   [ s_overlap_buf[FFT_MIC_N - overlap_n .. FFT_MIC_N-1]   ← overlap tail ]
 *   [ s_norm[0 .. FFT_MIC_N - overlap_n - 1]                ← new samples  ]
 *
 * s_merged must be static — 4 KB is too large for the task stack.
 */
static float   s_overlap_buf[FFT_MIC_N] __attribute__((aligned(16)));
static float   s_merged[FFT_MIC_N]      __attribute__((aligned(16)));
static uint8_t s_overlap_valid = 0;

/* ---------- module state ---------- */

static QueueHandle_t s_queue = NULL;

/* ---------- internal task function ---------- */

static void mic_task_fn(void *arg)
{
    (void)arg;
    /* Local to this task — single owner, no cross-task access. */
    int avg_cnt  = 0;
    int fail_cnt = 0;   /* consecutive mic_capture_read_block failures */

    /* Local copies of adaptive-sensing parameters — latched at the start of
     * each averaging cycle so a gateway update never corrupts a partially
     * accumulated power spectrum.  Initialised to the compile-time defaults. */
    int local_spec_avg_n  = SPEC_AVG_N;
    int local_overlap_pct = 0;

    /* Enable I2S DMA from CPU1 so the DMA interrupt is allocated here,
     * away from the WiFi driver task on CPU0. */
    ESP_ERROR_CHECK(mic_capture_enable());

    /* Stats from the LAST block before the averaged frame is emitted.
     * We capture them here so we can store them in the frame. */
    float last_rms      = 0.0f;
    float last_crest    = 0.0f;
    float last_kurtosis = 3.0f;  /* Gaussian baseline */
    float last_dc       = 0.0f;
    uint8_t last_clip   = 0;

    while (1) {
        /* --- 0. Latch adaptive-sensing parameters at cycle start ---
         * Read globals only when avg_cnt == 0 so a change takes effect
         * cleanly on the next averaging cycle, never mid-accumulation. */
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

        /* --- 1. Capture one DMA block --- */
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

        /* Crest factor: peak / RMS */
        float peak_abs = fmaxf(fabsf((float)st.min_sample),
                               fabsf((float)st.max_sample)) / 8388608.0f;
        float crest = (st.rms > 1e-8f) ? (peak_abs / st.rms) : 0.0f;

        /* Save stats from this block (will be used if this is the last
         * block of an averaging window) */
        last_rms   = st.rms;
        last_crest = crest;
        last_dc    = st.dc_offset;
        last_clip  = (uint8_t)(st.clipped_count > 0 ? 1 : 0);

        /* --- 3. DC removal ---
         * Subtracting the block mean prevents the DC component from
         * leaking energy into bins 1-3 and masking low-frequency fault tones. */
        float dc = st.dc_offset;
        for (int i = 0; i < FFT_MIC_N; i++) {
            s_norm[i] -= dc;
        }

        /* --- 3b. Kurtosis on zero-mean signal ---
         * K = E[x^4] / (E[x^2])^2.  Gaussian noise → K≈3. Impulsive fault
         * events → K>6.  This is the ISO 10816 bearing fault indicator.
         *
         * Variance is computed directly from the DC-removed s_norm to avoid
         * catastrophic cancellation in (rms² - dc²) when DC is large. */
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

        /* --- 3c. Build FFT input with optional Welch overlap ---
         *
         * Welch's method: the FFT window is formed from the last overlap_n
         * samples of the PREVIOUS block (saved in s_overlap_buf) followed by
         * the first (FFT_MIC_N - overlap_n) samples of the CURRENT block.
         * This is equivalent to advancing the window by (FFT_MIC_N - overlap_n)
         * samples rather than FFT_MIC_N, giving finer time resolution at the
         * cost of correlated successive spectra.
         *
         * If no previous block exists (first capture) or overlap is 0%, the
         * current block feeds the FFT directly (no copy overhead).
         */
        const float *fft_src = s_norm;   /* default: no overlap */
        if (local_overlap_pct > 0 && s_overlap_valid) {
            int overlap_n = (local_overlap_pct * FFT_MIC_N) / 100;
            if (overlap_n > 0 && overlap_n < FFT_MIC_N) {
                /* Tail of previous block */
                memcpy(s_merged,
                       s_overlap_buf + (FFT_MIC_N - overlap_n),
                       (size_t)overlap_n * sizeof(float));
                /* Head of current block */
                memcpy(s_merged + overlap_n,
                       s_norm,
                       (size_t)(FFT_MIC_N - overlap_n) * sizeof(float));
                fft_src = s_merged;
            }
        }
        /* Save current block for the next overlap (always, regardless of overlap_pct
         * so that enabling overlap mid-run starts cleanly on the first cycle). */
        memcpy(s_overlap_buf, s_norm, FFT_MIC_N * sizeof(float));
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

        /* --- 6. Accumulate linear power ---
         * Normalise by 2/N so that a full-scale sine → 0 dBFS peak. */
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
        uint32_t ts_ms = (uint32_t)(esp_timer_get_time() / 1000);
        const float inv_n = 1.0f / (float)local_spec_avg_n;
        for (int i = 0; i < FFT_HALF; i++) {
            s_mag_db[i]  = 10.0f * log10f(s_pwr_acc[i] * inv_n + 1e-12f);
            s_pwr_acc[i] = 0.0f;
        }
        /* Zero the DC bin — should be near -120 dBFS after DC removal
         * but explicitly set to avoid it ever appearing as a spurious peak. */
        s_mag_db[0] = -120.0f;
        avg_cnt = 0;

        /* --- 8. Build frame and post to queue --- */
        mic_frame_t frame;
        memcpy(frame.fft_db, s_mag_db, sizeof(frame.fft_db));
        frame.rms          = last_rms;
        frame.crest        = last_crest;
        frame.kurtosis     = last_kurtosis;
        frame.dc           = last_dc;
        frame.clip         = last_clip;
        frame.timestamp_ms = ts_ms;

        /* xQueueOverwrite so the mic task never blocks — the wifi_task
         * consumes at its own pace and always gets the freshest frame. */
        xQueueOverwrite(s_queue, &frame);
    }
}

/* ---------- public API ---------- */

QueueHandle_t mic_task_get_queue(void)
{
    return s_queue;
}

void mic_task_start(void)
{
    /* Create queue depth=1 to hold exactly one mic_frame_t */
    s_queue = xQueueCreate(1, sizeof(mic_frame_t));
    configASSERT(s_queue != NULL);

    /* Initialise I2S microphone */
    ESP_ERROR_CHECK(mic_capture_init());

    /* Pre-compute Hann window coefficients */
    dsps_wind_hann_f32(s_window, FFT_MIC_N);

    /* FFT twiddle-factor table is initialised once in app_main (not here).
     * app_main calls dsps_fft2r_init_fc32(NULL, FFT_IMU_N) which covers
     * all FFT sizes <= FFT_IMU_N, including FFT_MIC_N. */

    ESP_LOGI(TAG, "mic_task starting: %d-pt FFT, avg=%d (adaptive), %.2f Hz/bin, Fs=%d Hz",
             FFT_MIC_N, SPEC_AVG_N,
             (float)MIC_FS_HZ / FFT_MIC_N, MIC_FS_HZ);

    xTaskCreatePinnedToCore(mic_task_fn, "mic_task", TASK_STACK_MIC, NULL,
                            TASK_PRIO_MIC, NULL, 1);
}
