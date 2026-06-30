/*
 * main.c — EPM (EdgeAI Predictive Monitor) entry point.
 *
 * Initialises system services and starts the FreeRTOS tasks:
 *
 *   Core 0 — Radio and peripheral capture (time-critical I/O)
 *   ┌────────────────────────────────────────────────────────┐
 *   │ wifi_task        priority 4   stack 10240              │
 *   │ mic_task         priority 5   stack 8192  (I2S DMA)    │
 *   │ imu_task         priority 5   stack 8192  (SPI DMA)    │
 *   │ diagnostics_task priority 1   stack 3072  (health mon) │
 *   └────────────────────────────────────────────────────────┘
 *
 *   Core 1 — Compute (no radio interference)
 *   ┌────────────────────────────────────────────────────────┐
 *   │ dsp_task       priority 6   stack 16384  (FFT compute) │
 *   │ rgb_led_task   priority 3   stack 3072   (LEDC HW)     │
 *   └────────────────────────────────────────────────────────┘
 *
 * FFT table note: dsps_fft2r_init_fc32 with NULL uses a shared static table
 * and is NOT thread-safe.  We initialise once here with the larger FFT_IMU_N
 * (2048) before any tasks start.  A 2048-pt table is a superset of 1024-pt —
 * both tasks can use it concurrently without re-initialising.
 */

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_wifi.h"
#include "esp_heap_caps.h"

#include "dsps_fft2r.h"

#include "epm_config.h"
#include "rgb_led_task.h"
#include "mic_task.h"
#include "dsp_task.h"
#include "imu_task.h"
#include "wifi_task.h"
#include "mic_capture.h"

static const char *TAG = "main";

/* ── Diagnostics task ─────────────────────────────────────────────────────── */

typedef struct {
    TaskHandle_t h_mic;
    TaskHandle_t h_dsp;
    TaskHandle_t h_wifi;
    TaskHandle_t h_rgb;
} diag_args_t;

static diag_args_t s_diag_args;

static void diagnostics_task_fn(void *arg)
{
    diag_args_t  *a     = (diag_args_t *)arg;
    TaskHandle_t  h_diag = xTaskGetCurrentTaskHandle();

    /* Static buffer: vTaskGetRunTimeStats writes a text table (~400 bytes
     * for 6 tasks).  Static keeps it off the 3072-byte task stack. */
    static char s_stats[512];

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(30000));   /* 30 s health interval */

        /* Stack watermarks — minimum ever-free stack words × 4 bytes.
         * A value approaching 0 signals an imminent stack overflow. */
        ESP_LOGI("DIAG", "Stack HWM (bytes free): mic=%lu dsp=%lu wifi=%lu rgb=%lu diag=%lu",
            (unsigned long)uxTaskGetStackHighWaterMark(a->h_mic)  * 4,
            (unsigned long)uxTaskGetStackHighWaterMark(a->h_dsp)  * 4,
            (unsigned long)uxTaskGetStackHighWaterMark(a->h_wifi) * 4,
            (unsigned long)uxTaskGetStackHighWaterMark(a->h_rgb)  * 4,
            (unsigned long)uxTaskGetStackHighWaterMark(h_diag)    * 4);

        /* CPU runtime statistics (requires CONFIG_FREERTOS_GENERATE_RUN_TIME_STATS=y) */
        vTaskGetRunTimeStats(s_stats);
        ESP_LOGI("DIAG", "CPU runtime:\n%s", s_stats);

        /* Heap health */
        ESP_LOGI("DIAG", "Heap free: internal=%lu PSRAM=%lu IRAM=%lu",
            (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
            (unsigned long)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
            (unsigned long)heap_caps_get_free_size(MALLOC_CAP_EXEC));

        /* I2S DMA overflow health — non-zero means audio gaps occurred */
        uint32_t ov = mic_capture_get_overflow_count();
        if (ov > 0) {
            ESP_LOGW("DIAG", "I2S DMA overflows (cumulative): %lu — check CPU load",
                     (unsigned long)ov);
        } else {
            ESP_LOGI("DIAG", "I2S DMA overflows: 0 (clean)");
        }
    }
}

/* ── app_main ─────────────────────────────────────────────────────────────── */

void app_main(void)
{
    /* --- System services --- */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    /* HW-OPT: Boot memory map — logged before any tasks allocate heap.
     * Use these numbers in HARDWARE_AUDIT_RESULTS.md baseline table. */
    ESP_LOGI(TAG, "Boot memory (before tasks): "
             "DRAM free=%lu PSRAM free=%lu IRAM free=%lu",
        (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
        (unsigned long)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
        (unsigned long)heap_caps_get_free_size(MALLOC_CAP_EXEC));

    /* Initialise FFT twiddle-factor table once with the largest needed size.
     * FFT_IMU_N >= FFT_MIC_N, so a single init covers both tasks.
     * NULL → use the built-in static twiddle table in DROM (flash, read-only). */
#if FFT_IMU_N >= FFT_MIC_N
    ESP_ERROR_CHECK(dsps_fft2r_init_fc32(NULL, FFT_IMU_N));
#else
    ESP_ERROR_CHECK(dsps_fft2r_init_fc32(NULL, FFT_MIC_N));
#endif

    /* --- RGB LED: init hardware and start task on core 1, priority 3 --- */
    rgb_led_task_init();
    static TaskHandle_t h_rgb = NULL;
    xTaskCreatePinnedToCore(rgb_led_task, "rgb_led", 3072, NULL, 3, &h_rgb, 1);
    rgb_led_set_state(RGB_BOOT);

    /* --- Start WiFi RF before any I2S/DMA ---
     * I2S DMA interrupts disrupt the WiFi firmware RF state-machine timing
     * if armed concurrently, causing TG1WDT_SYS_RST during the initial scan.
     * Connect first, then start the DSP tasks. */
    wifi_rf_init();
    if (!wifi_wait_connected(pdMS_TO_TICKS(30000))) {
        ESP_LOGW(TAG, "WiFi not connected after 30 s — starting DSP anyway");
    }

    /* --- Start application tasks --- */
    mic_task_start();
    dsp_task_start(mic_task_get_raw_ringbuf());
    imu_task_start();
    wifi_task_start(dsp_task_get_queue(), imu_task_get_queue());

    /* --- diagnostics_task (core 0, priority 1) ---
     * Collects task handles after all *_task_start() calls complete.
     * Priority 1 ensures it never preempts any application task. */
    s_diag_args.h_mic  = mic_task_get_handle();
    s_diag_args.h_dsp  = dsp_task_get_handle();
    s_diag_args.h_wifi = wifi_task_get_handle();
    s_diag_args.h_rgb  = h_rgb;

    static TaskHandle_t h_diag = NULL;
    xTaskCreatePinnedToCore(diagnostics_task_fn, "diag", TASK_STACK_DIAG,
                            &s_diag_args, TASK_PRIO_DIAG, &h_diag, 0);

    ESP_LOGI(TAG, "EPM: mic=%d-pt imu=%d-pt avg=%d | %s:%d",
             FFT_MIC_N, FFT_IMU_N, SPEC_AVG_N, SERVER_IP, SERVER_PORT);

    while (1) {
        vTaskDelay(portMAX_DELAY);
    }
}
