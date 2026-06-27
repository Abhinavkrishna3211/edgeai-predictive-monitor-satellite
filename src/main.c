/*
 * main.c — EPM (EdgeAI Predictive Monitor) entry point.
 *
 * Initialises system services and starts the three FreeRTOS tasks:
 *   mic_task  — I2S capture + FFT (FFT_MIC_N pt, SPEC_AVG_N avg)
 *   imu_task  — KX134 stub/driver + FFT (FFT_IMU_N pt, SPEC_AVG_N avg)
 *   wifi_task — waits for both queues, TCP-sends concatenated FFT payload
 *
 * NVS flash must be initialised before WiFi (esp_wifi_init internally
 * reads/writes NVS calibration data).
 *
 * FFT table note: dsps_fft2r_init_fc32 with NULL uses a shared static
 * table and is NOT thread-safe.  We initialise once here with the larger
 * FFT_IMU_N (2048) before any tasks start.  A 2048-pt table is a superset
 * of 1024-pt — both tasks can use it concurrently without re-initialising.
 */

#include "esp_log.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_wifi.h"    /* esp_netif_create_default_wifi_sta */

#include "dsps_fft2r.h"

#include "epm_config.h"
#include "led_task.h"
#include "mic_task.h"
#include "imu_task.h"
#include "wifi_task.h"

static const char *TAG = "main";

void app_main(void)
{
    /* --- System services --- */

    /* NVS: required by esp_wifi_init for calibration and PMF storage */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* TCP/IP stack and default event loop */
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    /* Default STA netif — must exist before esp_wifi_start() */
    esp_netif_create_default_wifi_sta();

    /* Initialise FFT twiddle-factor table once with the largest needed size.
     * FFT_IMU_N >= FFT_MIC_N, so a single init covers both tasks. */
#if FFT_IMU_N >= FFT_MIC_N
    ESP_ERROR_CHECK(dsps_fft2r_init_fc32(NULL, FFT_IMU_N));
#else
    ESP_ERROR_CHECK(dsps_fft2r_init_fc32(NULL, FFT_MIC_N));
#endif

    /* --- LED task: start first so the board gives visual feedback during boot --- */
    led_task_start();

    /* --- Start WiFi RF before any I2S/DMA ---
     * I2S DMA interrupts disrupt the WiFi firmware's RF state-machine timing
     * if armed concurrently, causing TG1WDT_SYS_RST during the initial scan.
     * Connect first, then start the DSP tasks. */
    wifi_rf_init();
    if (!wifi_wait_connected(pdMS_TO_TICKS(30000))) {
        ESP_LOGW(TAG, "WiFi not connected after 30 s — starting DSP anyway");
    }

    /* --- Start DSP tasks (I2S DMA arms here, WiFi already connected) --- */
    mic_task_start();
    imu_task_start();
    wifi_task_start(mic_task_get_queue(), imu_task_get_queue());

    ESP_LOGI(TAG, "EPM: mic=%d-pt imu=%d-pt avg=%d | %s:%d",
             FFT_MIC_N, FFT_IMU_N, SPEC_AVG_N, SERVER_IP, SERVER_PORT);

    /* Keep main_task alive — prevents vTaskDelete from running during
     * FreeRTOS SMP context switch while WiFi driver is initialising. */
    while (1) {
        vTaskDelay(portMAX_DELAY);
    }
}
