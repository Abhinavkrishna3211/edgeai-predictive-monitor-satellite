/*
 * wifi_task.c — WiFi STA lifecycle + power management.
 *
 * Responsibilities:
 *   1. Bring up WiFi STA and join WIFI_SSID / WIFI_PASS
 *   2. Wait for IP assignment (event group bit)
 *   3. Cap TX power and configure dynamic CPU frequency scaling
 *
 * Revived from src/threads/tcp_task.c (Phase 7a) once the raw-TCP+AES
 * transport that used to share that file was retired in favor of MQTT
 * (docs/decisions/ADR-023-transport-adrs-superseded.md). See
 * docs/decisions/ADR-022-wifi-task-revived.md for why this lifecycle+PM
 * code got its own file again instead of folding into net_task.c.
 *
 * On WIFI_EVENT_STA_DISCONNECTED: clears WIFI_CONNECTED_BIT and reconnects.
 */

#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_pm.h"

#include "epm_config.h"
#include "hal/hal_display.h"
#include "threads/wifi_task.h"

static const char *TAG = "wifi_task";

#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_MAX_RETRY      10

/* ---------- module state ---------- */

static EventGroupHandle_t s_wifi_event_group  = NULL;
static int                s_retry_cnt         = 0;
static uint32_t           s_connects          = 0;
static uint32_t           s_disconnects       = 0;

/* JTAG-readable: 0=init 1=rf_init_done 2=sta_start 3=connecting 4=got_ip */
volatile uint32_t         g_wifi_debug_state  = 0;

/* ---------- WiFi event sub-handlers ---------- */

static void on_wifi_sta_start(void)
{
    g_wifi_debug_state = 2;  /* sta_start event received */
    rgb_led_set_state(RGB_WIFI_CONN);
    ESP_LOGI(TAG, "STA started — connecting to \"%s\"...", WIFI_SSID);
    g_wifi_debug_state = 3;  /* esp_wifi_connect() about to be called */
    esp_wifi_connect();
}

static void on_wifi_disconnected(wifi_event_sta_disconnected_t *d)
{
    g_wifi_debug_state = 3;  /* back to connecting state */
    rgb_led_set_state(RGB_WIFI_CONN);
    xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    s_retry_cnt++;
    s_disconnects++;
    const char *reason_str =
        (d->reason == 200) ? "BEACON_TIMEOUT"         :
        (d->reason == 201) ? "NO_AP_FOUND"            :
        (d->reason == 202) ? "AUTH_FAIL"              :
        (d->reason == 210) ? "NO_AP_COMPAT_SECURITY"  :
        (d->reason == 211) ? "NO_AP_AUTHMODE"         :
        (d->reason == 212) ? "NO_AP_RSSI"             :
        (d->reason == 15)  ? "4WAY_TIMEOUT"           :
        (d->reason == 17)  ? "IE_4WAY_DIFFERS"        :
        (d->reason == 8)   ? "ASSOC_LEAVE"            : "OTHER";
    ESP_LOGW(TAG, "Disconnect reason: %s (%d) attempt=%d",
             reason_str, d->reason, s_retry_cnt);
    if (s_retry_cnt % WIFI_MAX_RETRY == 0) {
        ESP_LOGE(TAG, "WiFi: %d consecutive failures [%s] — verify SSID/password "
                 "in wifi_creds.h", s_retry_cnt, reason_str);
    }
    /* Do NOT vTaskDelay here — this runs in the system event loop.
     * Blocking triggers the interrupt watchdog. */
    esp_wifi_connect();
}

static void on_got_ip(ip_event_got_ip_t *ev)
{
    g_wifi_debug_state = 4;  /* IP obtained */
    ESP_LOGI(TAG, "Got IP: " IPSTR " (after %d attempt(s))",
             IP2STR(&ev->ip_info.ip), s_retry_cnt + 1);
    s_retry_cnt = 0;
    s_connects++;
    rgb_led_set_state(RGB_TCP_CONN);
    xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        on_wifi_sta_start();
        return;
    }
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        on_wifi_disconnected((wifi_event_sta_disconnected_t *)event_data);
        return;
    }
    if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        on_got_ip((ip_event_got_ip_t *)event_data);
    }
}

/* ---------- public API ---------- */

/*
 * Phase 1: WiFi RF init — call BEFORE any I2S/DMA tasks are started.
 *
 * I2S DMA interrupts firing during WiFi's RF scan phase disrupt the WiFi
 * firmware's internal RF state-machine timing, causing TG1WDT_SYS_RST at
 * ~600 ms on every boot.  Starting WiFi before the I2S engine is armed
 * eliminates that interference window entirely.
 */
void wifi_rf_init(void)
{
    g_wifi_debug_state = 1;  /* rf_init started */
    s_wifi_event_group = xEventGroupCreate();
    configASSERT(s_wifi_event_group != NULL);

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    /* Register handlers AFTER esp_wifi_init() — this is the required order:
     * esp_wifi_init() sets up the WIFI_EVENT posting infrastructure; handlers
     * registered before it are placed in the default event loop but may never
     * receive WIFI_EVENT_STA_START on some IDF versions. */
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &wifi_event_handler, NULL));

    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASS,
            /* WPA_WPA2_PSK: accept WPA or WPA2 AP.  Avoids the silent stall
             * caused by WIFI_AUTH_WPA2_PSK + pmf_capable=true on Windows Mobile
             * Hotspot (WPA2-Personal without 802.11w). */
            .threshold.authmode = WIFI_AUTH_WPA_WPA2_PSK,
            .pmf_cfg            = { .capable = false, .required = false },
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* HW-OPT: cap TX power to 17 dBm to reduce peak current draw on the
     * XIAO USB-C 3.3 V rail.  Negligible range loss at <10 m deployment.
     * Unit: quarter-dBm.  WIFI_TX_POWER_QTR_DBM=68 → 17.0 dBm. */
    if (esp_wifi_set_max_tx_power(WIFI_TX_POWER_QTR_DBM) == ESP_OK) {
        ESP_LOGI(TAG, "WiFi TX power capped to %.1f dBm (%d q-dBm)",
                 WIFI_TX_POWER_QTR_DBM / 4.0f, (int)WIFI_TX_POWER_QTR_DBM);
    } else {
        ESP_LOGW(TAG, "esp_wifi_set_max_tx_power failed — using default");
    }

    /* Dynamic CPU frequency scaling: 240 MHz during active DSP/net bursts,
     * 80 MHz during idle gaps between frames.  Requires CONFIG_PM_ENABLE=y
     * and CONFIG_FREERTOS_USE_TICKLESS_IDLE=y in sdkconfig.defaults.
     *
     * Previously configured inside tcp_task.c's task loop, after the first
     * successful connection (docs/decisions/ADR-015). Now that there is no
     * task loop, this runs here instead — at RF init, before WIFI_PS_NONE
     * or the connection even exists — which is strictly earlier than
     * before, so no window opens where DFS is unconfigured that didn't
     * already exist previously. */
    esp_pm_config_t pm_cfg = {
        .max_freq_mhz       = 240,
        .min_freq_mhz       = 80,
        .light_sleep_enable = false,
    };
    if (esp_pm_configure(&pm_cfg) != ESP_OK) {
        ESP_LOGW(TAG, "esp_pm_configure failed — fixed 240 MHz");
    }
    /* WIFI_PS_NONE (set above) must not be overridden elsewhere.
     * WIFI_PS_MIN_MODEM causes the radio to enter DTIM sleep between beacon
     * intervals, which upstream code (net_task's MQTT keepalive) relies on
     * not happening. */

    ESP_LOGI(TAG, "WiFi RF init — SSID: \"%s\"", WIFI_SSID);
}

/* Phase 2: block until IP assigned (or timeout). Returns true if connected. */
bool wifi_wait_connected(TickType_t ticks_to_wait)
{
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
                                           WIFI_CONNECTED_BIT,
                                           pdFALSE, pdTRUE,
                                           ticks_to_wait);
    return (bits & WIFI_CONNECTED_BIT) != 0;
}

void wifi_task_get_stats(struct wifi_task_stats *out)
{
    if (out == NULL) return;
    out->connects    = s_connects;
    out->disconnects = s_disconnects;
    out->retry_cnt   = (uint32_t)s_retry_cnt;
}
