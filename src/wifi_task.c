/*
 * wifi_task.c — WiFi STA connection + TCP client send task.
 *
 * Responsibilities:
 *   1. Bring up WiFi STA and join WIFI_SSID / WIFI_PASS
 *   2. Wait for IP assignment (event group bit)
 *   3. Open TCP connection to SERVER_IP:SERVER_PORT
 *   4. Each iteration:
 *        a. xQueueReceive mic_frame_t (2 s timeout)
 *        b. xQueueReceive imu_frame_t (500 ms timeout)
 *        c. Build epm_header_t + length prefix
 *        d. send() length, header, mic FFT, imu FFT
 *        e. On any send failure: close socket, retry connect with 2 s delay
 *
 * On WIFI_EVENT_STA_DISCONNECTED: clears WIFI_CONNECTED_BIT and reconnects.
 *
 * Static frame buffers (NOT on stack — mic+imu frames ~6 KB combined).
 */

#include <string.h>
#include <errno.h>
#include <math.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "lwip/err.h"

#include "epm_config.h"
#include "epm_protocol.h"
#include "led_task.h"
#include "wifi_task.h"

/* ---------- module constants ---------- */

static const char *TAG = "wifi_task";

#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_MAX_RETRY      10      /* connection attempts before giving up */

/* ---------- module state ---------- */

static EventGroupHandle_t s_wifi_event_group = NULL;
static int                s_retry_cnt        = 0;

/* Static receive buffers — kept out of the task stack to avoid stack overflow.
 * mic_frame_t ~2 KB (512 floats), imu_frame_t ~12 KB (3 × 1024 floats). */
static mic_frame_t s_mic;
static imu_frame_t s_imu;

/* ---------- WiFi event handler ---------- */

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        led_set_state(LED_WIFI_CONN);
        ESP_LOGI(TAG, "STA started — connecting to \"%s\"...", WIFI_SSID);
        esp_wifi_connect();

    } else if (event_base == WIFI_EVENT &&
               event_id == WIFI_EVENT_STA_DISCONNECTED) {

        led_set_state(LED_WIFI_CONN);
        xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);

        /* Log the reason code so we can diagnose failures:
         *   15 / 203 = wrong password (4-way handshake timeout)
         *   200       = AP not found  (SSID mismatch or hotspot off)
         *   201       = auth rejected
         *   2         = auth expired  (was connected, AP rebooted)
         *
         * IMPORTANT: do NOT call vTaskDelay here — this handler runs in
         * the system event loop task.  Blocking it triggers the interrupt
         * watchdog and crashes the ESP32.  Immediate retry is correct;
         * the WiFi scan itself takes ~300 ms naturally. */
        wifi_event_sta_disconnected_t *d =
            (wifi_event_sta_disconnected_t *)event_data;
        s_retry_cnt++;
        ESP_LOGW(TAG, "Disconnected reason=%d (attempt %d) "
                 "— retrying  [15/203=wrong pw  200=SSID not found]",
                 d->reason, s_retry_cnt);
        if (s_retry_cnt % WIFI_MAX_RETRY == 0) {
            ESP_LOGE(TAG, "WiFi: %d consecutive failures — verify SSID/password "
                     "in wifi_creds.h (reason %d)", s_retry_cnt, d->reason);
        }
        esp_wifi_connect();

    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR " (after %d attempt(s))",
                 IP2STR(&ev->ip_info.ip), s_retry_cnt + 1);
        s_retry_cnt = 0;
        led_set_state(LED_TCP_CONN);
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

/* ---------- TCP helpers ---------- */

/*
 * tcp_connect() — creates a TCP socket and connects to SERVER_IP:SERVER_PORT.
 * Sets SO_KEEPALIVE and TCP_NODELAY for low-latency streaming.
 * Returns the socket fd on success, -1 on failure.
 */
static int tcp_connect(void)
{
    struct sockaddr_in dest_addr = {
        .sin_family      = AF_INET,
        .sin_port        = htons(SERVER_PORT),
    };
    if (inet_aton(SERVER_IP, &dest_addr.sin_addr) == 0) {
        ESP_LOGE(TAG, "Invalid SERVER_IP: %s", SERVER_IP);
        return -1;
    }

    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        return -1;
    }

    /* Low-latency options */
    int flag = 1;
    setsockopt(sock, SOL_SOCKET,  SO_KEEPALIVE, &flag, sizeof(flag));
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY,  &flag, sizeof(flag));

    /* 10-second connection timeout — prevents 75-second lwIP default block
     * when the gateway is unreachable (e.g., hotspot off). */
    struct timeval sndto = {.tv_sec = 10, .tv_usec = 0};
    if (setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &sndto, sizeof(sndto)) != 0) {
        ESP_LOGW(TAG, "SO_SNDTIMEO setsockopt failed: errno %d", errno);
    }

    if (connect(sock, (struct sockaddr *)&dest_addr, sizeof(dest_addr)) != 0) {
        ESP_LOGE(TAG, "connect() to %s:%d failed: errno %d",
                 SERVER_IP, SERVER_PORT, errno);
        close(sock);
        return -1;
    }

    ESP_LOGI(TAG, "TCP connected to %s:%d", SERVER_IP, SERVER_PORT);
    return sock;
}

/*
 * tcp_send_all() — ensures all bytes are written even if send() returns
 * a short count (common on embedded TCP stacks).
 * Returns 0 on success, -1 on error.
 */
static int tcp_send_all(int sock, const void *buf, size_t len)
{
    const uint8_t *ptr = (const uint8_t *)buf;
    size_t remaining   = len;

    while (remaining > 0) {
        int sent = send(sock, ptr, remaining, 0);
        if (sent < 0) {
            ESP_LOGE(TAG, "send() failed: errno %d", errno);
            return -1;
        }
        ptr       += sent;
        remaining -= (size_t)sent;
    }
    return 0;
}

/* ---------- main task function ---------- */

typedef struct {
    QueueHandle_t mic_q;
    QueueHandle_t imu_q;
} wifi_task_args_t;

static wifi_task_args_t s_task_args; /* static — lives beyond xTaskCreate */

static void wifi_task_fn(void *arg)
{
    wifi_task_args_t *args = (wifi_task_args_t *)arg;
    QueueHandle_t mic_q    = args->mic_q;
    QueueHandle_t imu_q    = args->imu_q;

    uint32_t frame_id  = 0;
    uint32_t cal_frames = 0;  /* frames since last TCP connect — first LED_CAL_FRAMES show LED_CALIBRATING */

    /* Wait until WiFi is up before attempting TCP */
    ESP_LOGI(TAG, "Waiting for WiFi...");
    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_LOGI(TAG, "WiFi ready");

    int sock = -1;

    while (1) {
        /* --- Ensure we have a live TCP connection --- */
        if (sock < 0) {
            /* Wait for IP if we lost WiFi */
            EventBits_t bits = xEventGroupWaitBits(
                s_wifi_event_group, WIFI_CONNECTED_BIT,
                pdFALSE, pdTRUE, pdMS_TO_TICKS(10000));

            if (!(bits & WIFI_CONNECTED_BIT)) {
                ESP_LOGW(TAG, "Still waiting for WiFi...");
                continue;
            }

            sock = tcp_connect();
            if (sock < 0) {
                ESP_LOGW(TAG, "TCP connect failed — retrying in 2 s");
                vTaskDelay(pdMS_TO_TICKS(2000));
                continue;
            }

            /* Send hello — gateway uses this to register and name the satellite */
            epm_hello_t hello = {0};   /* zero-init: ensures name is null-padded */
            hello.magic    = EPM_HELLO_MAGIC;
            hello.fw_major = 1;
            hello.fw_minor = 0;
            esp_wifi_get_mac(WIFI_IF_STA, hello.mac);
            snprintf(hello.name, sizeof(hello.name), "SAT-%02X%02X",
                     hello.mac[4], hello.mac[5]);
            if (tcp_send_all(sock, &hello, sizeof(hello)) != 0) {
                ESP_LOGE(TAG, "Hello send failed — reconnecting");
                led_set_state(LED_TCP_CONN);
                close(sock); sock = -1; continue;
            }
            cal_frames = 0;
            led_set_state(LED_CALIBRATING);
            ESP_LOGI(TAG, "Hello sent: name=%s MAC=%02X:%02X:%02X:%02X:%02X:%02X",
                     hello.name,
                     hello.mac[0], hello.mac[1], hello.mac[2],
                     hello.mac[3], hello.mac[4], hello.mac[5]);

            /* 100 ms recv timeout — used for the per-frame alert byte */
            struct timeval rcvto = {.tv_sec = 0, .tv_usec = 100000};
            if (setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &rcvto, sizeof(rcvto)) != 0) {
                ESP_LOGE(TAG, "SO_RCVTIMEO setsockopt failed: errno %d — "
                         "alert recv may block indefinitely", errno);
            }
        }

        /* --- 1. Receive mic frame (wait up to 2 s) --- */
        if (xQueueReceive(mic_q, &s_mic, pdMS_TO_TICKS(2000)) != pdTRUE) {
            ESP_LOGW(TAG, "mic_q timeout — no data from mic_task");
            continue;
        }

        /* --- 2. Receive imu frame (wait up to 1500 ms) --- */
        if (xQueueReceive(imu_q, &s_imu, pdMS_TO_TICKS(1500)) != pdTRUE) {
            ESP_LOGW(TAG, "imu_q timeout — no data from imu_task");
            continue;
        }

        /* --- 3. Build packet header --- */
        epm_header_t hdr;
        memset(&hdr, 0, sizeof(hdr));
        hdr.magic        = (uint32_t)EPM_MAGIC;
        hdr.frame_id     = frame_id++;
        hdr.timestamp_ms = s_mic.timestamp_ms;
        hdr.mic_bins     = (uint16_t)(FFT_MIC_N / 2);
        hdr.imu_bins     = (uint16_t)(FFT_IMU_N / 2);
        hdr.imu_axes     = 3;  /* X, Y, Z */
        hdr.mic_rms      = s_mic.rms;
        hdr.mic_crest    = s_mic.crest;
        hdr.mic_dc       = s_mic.dc;
        hdr.mic_kurtosis = s_mic.kurtosis;
        hdr.mic_clip     = s_mic.clip;
        /* imu_rms / imu_crest carry the max across axes for easy thresholding */
        hdr.imu_rms   = fmaxf(s_imu.rms_x, fmaxf(s_imu.rms_y, s_imu.rms_z));
        hdr.imu_crest = fmaxf(s_imu.crest_x, fmaxf(s_imu.crest_y, s_imu.crest_z));
        hdr.imu_dc    = 0.0f;  /* DC removed before FFT — always ≈ 0 */
        hdr.imu_clip  = s_imu.clip;

        /* --- 4. Compute total payload bytes and send --- */
        uint32_t payload_bytes =
            (uint32_t)(sizeof(epm_header_t)
                       + (FFT_MIC_N / 2) * sizeof(float)
                       + (FFT_IMU_N / 2) * sizeof(float) * 3);  /* 3 axes */

        /* 4a–4d. Send length prefix → header → MIC FFT → IMU X/Y/Z.
         * Short-circuit on first failure: no point continuing on a broken socket. */
        int err = tcp_send_all(sock, &payload_bytes, sizeof(payload_bytes));
        if (!err) err = tcp_send_all(sock, &hdr, sizeof(hdr));
        if (!err) err = tcp_send_all(sock, s_mic.fft_db,
                                     (FFT_MIC_N / 2) * sizeof(float));
        if (!err) err = tcp_send_all(sock, s_imu.fft_x, (FFT_IMU_N / 2) * sizeof(float));
        if (!err) err = tcp_send_all(sock, s_imu.fft_y, (FFT_IMU_N / 2) * sizeof(float));
        if (!err) err = tcp_send_all(sock, s_imu.fft_z, (FFT_IMU_N / 2) * sizeof(float));

        /* --- 5. Handle send failure --- */
        if (err != 0) {
            ESP_LOGE(TAG, "TCP send failed on frame %lu — reconnecting",
                     (unsigned long)frame_id - 1);
            led_set_state(LED_TCP_CONN);
            close(sock);
            sock = -1;
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }

        /* --- 6. Read 1-byte alert from gateway (100 ms timeout from setsockopt) --- */
        uint8_t alert = EPM_ALERT_OK;
        int n = recv(sock, &alert, 1, 0);
        if (n == 1) {
            if (alert != EPM_ALERT_OK) {
                ESP_LOGW(TAG, "frame %lu: alert=0x%02x from gateway",
                         (unsigned long)(frame_id - 1), alert);
            }
        }
        /* n==0 → gateway closed; n<0 with errno EAGAIN/EWOULDBLOCK → timeout (OK) */
        if (n == 0) {
            ESP_LOGW(TAG, "Gateway closed connection — reconnecting");
            led_set_state(LED_TCP_CONN);
            close(sock); sock = -1; continue;
        }

        /* Update LED state: hold CALIBRATING for first LED_CAL_FRAMES frames,
         * then switch to alert-driven state (OK / WARN / FAULT). */
        cal_frames++;
        if (cal_frames < LED_CAL_FRAMES) {
            led_set_state(LED_CALIBRATING);
        } else {
            led_set_state(alert == EPM_ALERT_FAULT ? LED_FAULT :
                          alert == EPM_ALERT_WARN  ? LED_WARN  : LED_OK);
        }

        ESP_LOGD(TAG, "frame %lu sent: mic_rms=%.4f imu_max_crest=%.2f",
                 (unsigned long)(frame_id - 1), s_mic.rms, hdr.imu_crest);
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
    s_wifi_event_group = xEventGroupCreate();
    configASSERT(s_wifi_event_group != NULL);

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               &wifi_event_handler, NULL));

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,   /* WPA2 only — WPA (TKIP) is crackable */
            .pmf_cfg = { .capable = true, .required = false },
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    esp_wifi_set_ps(WIFI_PS_NONE);   /* disable modem sleep before start — full TCP throughput */
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "WiFi RF init — SSID: \"%s\", target: %s:%d",
             WIFI_SSID, SERVER_IP, SERVER_PORT);
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

/* Phase 3: create TCP sender task — call after mic/imu tasks exist. */
void wifi_task_start(QueueHandle_t mic_q, QueueHandle_t imu_q)
{
    s_task_args.mic_q = mic_q;
    s_task_args.imu_q = imu_q;

    xTaskCreate(wifi_task_fn, "wifi_task", TASK_STACK_WIFI,
                &s_task_args, TASK_PRIO_WIFI, NULL);
}
