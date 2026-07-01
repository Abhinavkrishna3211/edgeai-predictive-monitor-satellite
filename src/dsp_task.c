/*
 * dsp_task.c — ESP-DSP FFT pipeline task (core 1).
 *
 * Receives raw_mic_block_t items from mic_task via ring buffer, applies the
 * full Welch/Hann/FFT pipeline, and emits mic_frame_t to wifi_task after
 * SPEC_AVG_N (or g_adapt_spec_avg_n) blocks.
 *
 * Running exclusively on core 1 means the FFT never competes with I2S DMA
 * interrupts (core 0) or the WiFi driver (core 0).
 *
 * HW-OPT: ring buffer zero-copy — raw_mic_block_t is accessed directly from
 * the ring buffer storage (internal DRAM), no intermediate memcpy.  Item is
 * returned to the ring buffer immediately after samples are copied to the
 * Welch overlap buffer, typically within 2 µs of receipt.
 *
 * HW-OPT: ESP-DSP dsps_fft2r_fc32 uses the LX7 vectorisation unit (128-bit
 * SIMD, 4 floats/cycle).  Benchmark logged once at startup; expected ~1.1 ms
 * (≈264 k CPU cycles at 240 MHz) vs ~4.2 ms for a scalar Cooley-Tukey.
 *
 * Pipeline per block:
 *   1.  xRingbufferReceive — zero-copy pointer to raw_mic_block_t
 *   2.  Welch overlap (optional)
 *   3.  vRingbufferReturnItem — release ring buffer slot
 *   4.  Hann window (dsps_mul_f32, SIMD)
 *   5.  Pack interleaved complex, dsps_fft2r_fc32 + dsps_bit_rev2r_fc32
 *   6.  Accumulate linear power into s_pwr_acc (internal DRAM)
 *   7.  After SPEC_AVG_N blocks:
 *       7a. Spectral centroid via dsps_dotprod_f32 on s_pwr_acc
 *       7b. Convert s_pwr_acc → dBFS → s_mag_db (PSRAM)
 *       7c. Build mic_frame_t (static internal DRAM) and post to queue
 */

#include <math.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/ringbuf.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_attr.h"
#include "esp_cpu.h"         /* esp_cpu_get_cycle_count() — HW cycle counter (IDF 5.x) */

#include "dsps_fft2r.h"
#include "dsps_wind.h"
#include "dsps_math.h"
#include "dsps_dotprod.h"

#include "epm_config.h"
#include "dsp_task.h"
#include "wifi_task.h"     /* g_adapt_overlap_pct, g_adapt_spec_avg_n */
#include "rgb_led_task.h"  /* rgb_led_set_state, RGB_OK */

/* Set to true when 250 averaged frames have been processed (HST warm-up done).
 * Read by wifi_task on core 0 — volatile ensures cross-core visibility. */
volatile bool g_hst_warmed_up = false;

static const char *TAG = "dsp_task";

#define FFT_HALF  (FFT_MIC_N / 2)

/* Task handle — exposed via getter for diagnostics_task stack HWM logging. */
static TaskHandle_t s_task_handle = NULL;
TaskHandle_t dsp_task_get_handle(void) { return s_task_handle; }

/* ── FFT working buffers (internal DRAM — fast, SIMD-accessible) ─────────── */

/* HW-OPT: aligned(16) satisfies LX7 128-bit SIMD lane requirements. */
static float s_window    [FFT_MIC_N]     __attribute__((aligned(16)));
static float s_windowed  [FFT_MIC_N]     __attribute__((aligned(16)));
static float s_fft       [FFT_MIC_N * 2] __attribute__((aligned(16)));
static float s_pwr_acc   [FFT_HALF]      __attribute__((aligned(16)));  /* accumulator: fast DRAM */
static float s_overlap_buf[FFT_MIC_N]    __attribute__((aligned(16)));
static float s_merged    [FFT_MIC_N]     __attribute__((aligned(16)));
static uint8_t s_overlap_valid = 0;

/* ── Spectral centroid support — pre-computed frequency-bin table ─────────── */

/* HW-OPT: pre-computing freq_bins once avoids N multiplications inside the
 * averaging loop.  dsps_dotprod_f32 computes Σ(f_i × P_i) in a single SIMD
 * pass; the result divided by ΣP_i gives the spectral centroid in Hz. */
static float s_freq_bins[FFT_HALF] __attribute__((aligned(16)));  /* Hz per bin */
static float s_ones_half[FFT_HALF] __attribute__((aligned(16)));  /* all-1 for Σ P_i */

/* ── FFT output buffer in PSRAM ──────────────────────────────────────────── */

/* HW-OPT: EXT_RAM_BSS_ATTR places s_mag_db (2 KB) in PSRAM, saving internal
 * DRAM.  It is written once per SPEC_AVG_N frames (not in the hot compute
 * loop) so PSRAM access latency is not a bottleneck.  The FFT working
 * buffers above (s_pwr_acc, s_fft, etc.) remain in fast internal DRAM. */
static EXT_RAM_BSS_ATTR float s_mag_db[FFT_HALF];

/* ── Frame output buffer (static to keep 2 KB off the task stack) ─────────── */
static mic_frame_t s_out_frame;

static QueueHandle_t s_queue = NULL;

/* ── DSP task ─────────────────────────────────────────────────────────────── */

static void dsp_task_fn(void *arg)
{
    RingbufHandle_t raw_rb = (RingbufHandle_t)arg;

    int      avg_cnt          = 0;
    int      local_spec_avg_n  = SPEC_AVG_N;
    int      local_overlap_pct = 0;
    uint32_t hst_frame_count   = 0;
    bool     fft_benchmarked   = false;

    float   last_rms      = 0.0f;
    float   last_crest    = 0.0f;
    float   last_kurtosis = 3.0f;
    float   last_dc       = 0.0f;
    uint8_t last_clip     = 0;

    /* Pre-compute frequency-bin table and ones array (done once at startup). */
    const float hz_per_bin = (float)MIC_FS_HZ / FFT_MIC_N;
    for (int i = 0; i < FFT_HALF; i++) {
        s_freq_bins[i] = (float)i * hz_per_bin;
        s_ones_half[i] = 1.0f;
    }

    while (1) {
        /* --- 1. Zero-copy receive from ring buffer --- */
        size_t item_sz = 0;
        raw_mic_block_t *blk = (raw_mic_block_t *)
            xRingbufferReceive(raw_rb, &item_sz, pdMS_TO_TICKS(2000));

        if (blk == NULL) {
            ESP_LOGW(TAG, "raw_rb timeout — no data from mic_task");
            continue;
        }

        /* --- Latch adaptive parameters at cycle start --- */
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

        /* Latch per-block stats from the ring buffer item. */
        last_rms      = blk->rms;
        last_crest    = blk->crest;
        last_kurtosis = blk->kurtosis;
        last_dc       = blk->dc;
        last_clip     = blk->clip;

        /* --- 2. Welch overlap (uses blk->samples before returning item) --- */
        const float *fft_src = blk->samples;
        if (local_overlap_pct > 0 && s_overlap_valid) {
            int overlap_n = (local_overlap_pct * FFT_MIC_N) / 100;
            if (overlap_n > 0 && overlap_n < FFT_MIC_N) {
                memcpy(s_merged,
                       s_overlap_buf + (FFT_MIC_N - overlap_n),
                       (size_t)overlap_n * sizeof(float));
                memcpy(s_merged + overlap_n,
                       blk->samples,
                       (size_t)(FFT_MIC_N - overlap_n) * sizeof(float));
                fft_src = s_merged;
            }
        }
        memcpy(s_overlap_buf, blk->samples, FFT_MIC_N * sizeof(float));
        s_overlap_valid = 1;

        /* --- 3. Return ring buffer item — done reading blk->samples --- */
        vRingbufferReturnItem(raw_rb, blk);

        /* --- 4. Hann window (SIMD) --- */
        dsps_mul_f32(fft_src, s_window, s_windowed, FFT_MIC_N, 1, 1, 1);

        /* --- 5. FFT --- */
        for (int i = 0; i < FFT_MIC_N; i++) {
            s_fft[2 * i]     = s_windowed[i];
            s_fft[2 * i + 1] = 0.0f;
        }

        /* HW-OPT: esp_cpu_get_cycle_count() benchmark — logged once at startup.
         * dsps_fft2r_fc32 uses LX7 128-bit SIMD butterfly units.
         * Expected: ~264 k cycles (~1.1 ms at 240 MHz) vs ~1008 k (~4.2 ms) scalar. */
        if (!fft_benchmarked) {
            uint32_t t0 = esp_cpu_get_cycle_count();
            dsps_fft2r_fc32(s_fft, FFT_MIC_N);
            dsps_bit_rev2r_fc32(s_fft, FFT_MIC_N);
            uint32_t t1 = esp_cpu_get_cycle_count();
            ESP_LOGI(TAG, "FFT benchmark: %lu cycles (%.2f ms at 240 MHz) for %d-pt",
                     (unsigned long)(t1 - t0),
                     (float)(t1 - t0) / 240000.0f,
                     FFT_MIC_N);
            fft_benchmarked = true;
        } else {
            dsps_fft2r_fc32(s_fft, FFT_MIC_N);
            dsps_bit_rev2r_fc32(s_fft, FFT_MIC_N);
        }

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

        /* --- 7a. Spectral centroid from accumulated linear power (SIMD) --- */
        /* Σ(f_i × P_i) / Σ(P_i) — computed on raw accumulator so division by
         * local_spec_avg_n cancels in numerator and denominator. */
        float freq_weighted = 0.0f, power_total = 0.0f;
        dsps_dotprod_f32(s_pwr_acc, s_freq_bins, &freq_weighted, FFT_HALF);
        dsps_dotprod_f32(s_pwr_acc, s_ones_half, &power_total,   FFT_HALF);
        float spectral_centroid = (power_total > 1e-20f)
                                  ? freq_weighted / power_total : 0.0f;

        /* --- 7b. Convert averaged linear power → dBFS (PSRAM output) --- */
        const float inv_n = 1.0f / (float)local_spec_avg_n;
        for (int i = 0; i < FFT_HALF; i++) {
            s_mag_db[i]  = 10.0f * log10f(s_pwr_acc[i] * inv_n + 1e-12f);
            s_pwr_acc[i] = 0.0f;
        }
        s_mag_db[0] = -120.0f;   /* DC bin */
        avg_cnt = 0;

        /* --- 7c. Build frame and post to wifi_task queue --- */
        memcpy(s_out_frame.fft_db, s_mag_db, sizeof(s_out_frame.fft_db));
        s_out_frame.rms              = last_rms;
        s_out_frame.crest            = last_crest;
        s_out_frame.kurtosis         = last_kurtosis;
        s_out_frame.dc               = last_dc;
        s_out_frame.spectral_centroid = spectral_centroid;
        s_out_frame.clip             = last_clip;
        s_out_frame.timestamp_ms     = (uint32_t)(esp_timer_get_time() / 1000);

        xQueueOverwrite(s_queue, &s_out_frame);

        hst_frame_count++;
        if (!g_hst_warmed_up && hst_frame_count >= 250) {
            g_hst_warmed_up = true;
            rgb_led_set_state(RGB_OK);
            ESP_LOGI(TAG, "HST warmed up at frame %lu", (unsigned long)hst_frame_count);
        }
    }
}

QueueHandle_t dsp_task_get_queue(void)
{
    return s_queue;
}

void dsp_task_start(RingbufHandle_t raw_rb)
{
    s_queue = xQueueCreate(1, sizeof(mic_frame_t));
    configASSERT(s_queue != NULL);

    dsps_wind_hann_f32(s_window, FFT_MIC_N);

    ESP_LOGI(TAG, "dsp_task starting (FFT core 1): %d-pt, avg=%d (adaptive), "
             "%.2f Hz/bin",
             FFT_MIC_N, SPEC_AVG_N, (float)MIC_FS_HZ / FFT_MIC_N);

    xTaskCreatePinnedToCore(dsp_task_fn, "dsp_task", TASK_STACK_DSP, raw_rb,
                            TASK_PRIO_DSP, &s_task_handle, 1);
}
