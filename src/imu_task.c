/*
 * imu_task.c — KX134 3-axis accelerometer capture + FFT task.
 *
 * Three independent FFTs (X radial, Y radial, Z axial) computed sequentially
 * using one shared set of compute buffers.  Per-axis power accumulators are
 * kept across SPEC_AVG_N blocks, then converted to dBFS and posted.
 *
 * Memory layout (sequential compute avoids 3× duplication):
 *   s_block[N]      — raw sample buffer, filled once per axis per call
 *   s_window[N]     — Hann coefficients, computed once and shared
 *   s_windowed[N]   — Hann-weighted block, reused per axis
 *   s_fft[2N]       — interleaved complex FFT output, reused per axis
 *   s_pwr_x/y/z[N/2] — per-axis power accumulators (kept for SPEC_AVG_N avg)
 *   s_db_x/y/z[N/2]  — per-axis dBFS output (built into imu_frame_t)
 *
 * Stub signal:
 *   X: 50 Hz sine 0.005g  + noise (radial imbalance at shaft fundamental)
 *   Y: 50 Hz + 150 Hz     + noise (same shaft, slightly different radial path)
 *   Z: 100 Hz sine 0.003g + noise (2× shaft — typical mild misalignment)
 * Replace generate_stub_axis() with KX134 FIFO burst-read when hw arrives.
 */

#warning "IMU task is a stub — replace with KX134 SPI driver when hardware arrives"

#include <math.h>
#include <string.h>

#include "esp_random.h"   /* hardware TRNG — thread-safe, no global shared state */

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "esp_log.h"
#include "esp_timer.h"

#include "dsps_fft2r.h"
#include "dsps_wind.h"
#include "dsps_math.h"

#include "epm_config.h"
#include "imu_task.h"

static const char *TAG = "imu_task";

#define IMU_HALF    (FFT_IMU_N / 2)

/* ─── Shared compute buffers (sequential axis processing) ─────────────────── */

static float s_block   [FFT_IMU_N]     __attribute__((aligned(16)));
static float s_window  [FFT_IMU_N]     __attribute__((aligned(16)));
static float s_windowed[FFT_IMU_N]     __attribute__((aligned(16)));
static float s_fft     [FFT_IMU_N * 2] __attribute__((aligned(16)));

/* Per-axis power accumulators — kept across SPEC_AVG_N blocks for averaging */
static float s_pwr_x[IMU_HALF] __attribute__((aligned(16)));
static float s_pwr_y[IMU_HALF] __attribute__((aligned(16)));
static float s_pwr_z[IMU_HALF] __attribute__((aligned(16)));

static QueueHandle_t s_queue = NULL;
static imu_frame_t   s_frame;          /* 12 KB — too large for task stack */

/* ─── Stub signal generator ───────────────────────────────────────────────── */

/*
 * Fill s_block with a synthetic single-axis signal.
 * freq1_hz: primary tone amplitude, freq2_hz: secondary tone (0 = none).
 * block_offset keeps phase continuous across calls.
 */
static void generate_stub_axis(float *phase1, float *phase2,
                                float freq1_hz, float amp1,
                                float freq2_hz, float amp2,
                                float noise_amp)
{
    /* Accumulate phase in radians and wrap to keep cosf() arguments small.
     * Large arguments (after hours of operation) force expensive range
     * reduction inside cosf() and cause a gradual fps drop. */
    const float pi2   = 2.0f * 3.14159265f;
    const float inc1  = pi2 * freq1_hz / (float)IMU_FS_HZ;
    const float inc2  = pi2 * freq2_hz / (float)IMU_FS_HZ;

    for (int i = 0; i < FFT_IMU_N; i++) {
        float v = amp1 * cosf(*phase1);
        *phase1 += inc1;
        if (*phase1 >= pi2) *phase1 -= pi2;

        if (freq2_hz > 0.0f) {
            v += amp2 * cosf(*phase2);
            *phase2 += inc2;
            if (*phase2 >= pi2) *phase2 -= pi2;
        }
        /* esp_random() — hardware TRNG, thread-safe, no global shared state.
         * Cast to int32_t before dividing to get a signed value in [-1, 1]. */
        v += noise_amp * ((float)(int32_t)esp_random() / 2147483648.0f);
        s_block[i] = v;
    }
}

/* ─── Per-axis stats ──────────────────────────────────────────────────────── */

typedef struct { float rms; float peak; float dc; uint8_t clip; } axis_stats_t;

static axis_stats_t compute_axis_stats(void)
{
    double sum = 0.0, sq = 0.0;
    float  peak = 0.0f;
    uint32_t clip = 0;

    for (int i = 0; i < FFT_IMU_N; i++) {
        float v = s_block[i];
        float a = fabsf(v);
        sum += v;
        sq  += (double)v * v;
        if (a > peak) peak = a;
        if (a >= 1.0f) clip++;
    }

    axis_stats_t st;
    st.dc   = (float)(sum / FFT_IMU_N);
    st.rms  = sqrtf((float)(sq  / FFT_IMU_N));
    st.peak = peak;
    st.clip = clip > 0 ? 1 : 0;
    return st;
}

/* ─── Single-axis FFT → accumulate power ─────────────────────────────────── */

static void fft_axis_accumulate(float *pwr_acc)
{
    /* DC removal */
    float dc = 0.0f;
    for (int i = 0; i < FFT_IMU_N; i++) dc += s_block[i];
    dc /= FFT_IMU_N;
    for (int i = 0; i < FFT_IMU_N; i++) s_block[i] -= dc;

    /* Hann window (SIMD) */
    dsps_mul_f32(s_block, s_window, s_windowed, FFT_IMU_N, 1, 1, 1);

    /* Pack interleaved complex (imag = 0) */
    for (int i = 0; i < FFT_IMU_N; i++) {
        s_fft[2 * i]     = s_windowed[i];
        s_fft[2 * i + 1] = 0.0f;
    }

    dsps_fft2r_fc32(s_fft, FFT_IMU_N);
    dsps_bit_rev2r_fc32(s_fft, FFT_IMU_N);

    /* Accumulate linear power */
    const float nf = 2.0f / FFT_IMU_N;
    for (int i = 0; i < IMU_HALF; i++) {
        float re = s_fft[2 * i]     * nf;
        float im = s_fft[2 * i + 1] * nf;
        pwr_acc[i] += re * re + im * im;
    }
}

/* ─── Convert accumulated power to dBFS array, reset accumulator ─────────── */

static void pwr_to_db(float *pwr_acc, float *db_out)
{
    const float inv_n = 1.0f / SPEC_AVG_N;
    for (int i = 0; i < IMU_HALF; i++) {
        db_out[i]  = 10.0f * log10f(pwr_acc[i] * inv_n + 1e-12f);
        pwr_acc[i] = 0.0f;
    }
    db_out[0] = -120.0f; /* zero DC bin */
}

/* ─── Task function ───────────────────────────────────────────────────────── */

static void imu_task_fn(void *arg)
{
    (void)arg;

    /* Phase accumulators — keep arguments to cosf() in [0, 2π) to avoid
     * slow range-reduction inside cosf() after hours of operation. */
    float ph_x1 = 0.0f, ph_x2 = 0.0f;
    float ph_y1 = 0.0f, ph_y2 = 0.0f;
    float ph_z1 = 0.0f, ph_z2 = 0.0f;

    /* Local to this task — single owner, no cross-task access. */
    int avg_cnt = 0;

    /* Per-axis stats from the final block of the averaging window */
    axis_stats_t st_x = {0}, st_y = {0}, st_z = {0};

    while (1) {
        /* Simulate acquisition time for one block at the target ODR.
         * Explicit parens ensure (N*1000) is computed first — without them
         * integer division (1000/IMU_FS_HZ) truncates to 0 when Fs > 1000. */
        vTaskDelay(pdMS_TO_TICKS((FFT_IMU_N * 1000) / IMU_FS_HZ));

        /* ── X axis (radial A): 50 Hz imbalance tone ── */
        generate_stub_axis(&ph_x1, &ph_x2, 50.0f, 0.005f, 0.0f, 0.0f, 0.001f);
        st_x = compute_axis_stats();
        fft_axis_accumulate(s_pwr_x);

        /* ── Y axis (radial B): 50 Hz + 150 Hz (3× harmonic) ── */
        generate_stub_axis(&ph_y1, &ph_y2, 50.0f, 0.004f, 150.0f, 0.002f, 0.001f);
        st_y = compute_axis_stats();
        fft_axis_accumulate(s_pwr_y);

        /* ── Z axis (axial): 100 Hz (2× shaft — mild misalignment) ── */
        generate_stub_axis(&ph_z1, &ph_z2, 100.0f, 0.003f, 0.0f, 0.0f, 0.0008f);
        st_z = compute_axis_stats();
        fft_axis_accumulate(s_pwr_z);

        avg_cnt++;

        if (avg_cnt < SPEC_AVG_N) continue;

        /* ── Build frame after SPEC_AVG_N blocks ── */
        pwr_to_db(s_pwr_x, s_frame.fft_x);
        pwr_to_db(s_pwr_y, s_frame.fft_y);
        pwr_to_db(s_pwr_z, s_frame.fft_z);

        s_frame.rms_x   = st_x.rms;
        s_frame.rms_y   = st_y.rms;
        s_frame.rms_z   = st_z.rms;
        s_frame.crest_x = (st_x.rms > 1e-8f) ? st_x.peak / st_x.rms : 0.0f;
        s_frame.crest_y = (st_y.rms > 1e-8f) ? st_y.peak / st_y.rms : 0.0f;
        s_frame.crest_z = (st_z.rms > 1e-8f) ? st_z.peak / st_z.rms : 0.0f;
        s_frame.dc_x         = st_x.dc;   /* X-axis gravity/tilt component — useful for mounting angle detection */
        s_frame.clip         = st_x.clip | st_y.clip | st_z.clip;
        s_frame.timestamp_ms = (uint32_t)(esp_timer_get_time() / 1000);

        avg_cnt = 0;
        xQueueOverwrite(s_queue, &s_frame);
    }
}

/* ─── Public API ──────────────────────────────────────────────────────────── */

QueueHandle_t imu_task_get_queue(void) { return s_queue; }

void imu_task_start(void)
{
    s_queue = xQueueCreate(1, sizeof(imu_frame_t));
    configASSERT(s_queue != NULL);

    dsps_wind_hann_f32(s_window, FFT_IMU_N);
    /* FFT twiddle-factor table initialised in app_main */

    ESP_LOGI(TAG, "imu_task starting (3-axis STUB): %d-pt × 3 axes, "
             "avg=%d, %.2f Hz/bin, Fs=%d Hz",
             FFT_IMU_N, SPEC_AVG_N, (float)IMU_FS_HZ / FFT_IMU_N, IMU_FS_HZ);

    xTaskCreatePinnedToCore(imu_task_fn, "imu_task", TASK_STACK_IMU, NULL,
                            TASK_PRIO_IMU, NULL, 0);
}
