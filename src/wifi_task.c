/*
 * wifi_task.c — WiFi STA connection + TCP client send task.
 *
 * Responsibilities:
 *   1. Bring up WiFi STA and join WIFI_SSID / WIFI_PASS
 *   2. Wait for IP assignment (event group bit)
 *   3. Open TCP connection to SERVER_IP:SERVER_PORT
 *   4. Each iteration:
 *        a. xQueueReceive mic_frame_t (2 s timeout)
 *        b. xQueueReceive imu_frame_t (1.5 s timeout)
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
#define WIFI_MAX_RETRY      10

/* ---------- module state ---------- */

static EventGroupHandle_t s_wifi_event_group = NULL;
static int                s_retry_cnt        = 0;

/* Adaptive-sensing parameters — set by wifi_task on v2 reply, read by mic_task. */
volatile uint8_t g_adapt_overlap_pct = 0;
volatile uint8_t g_adapt_spec_avg_n  = SPEC_AVG_N;

/* Static receive buffers — kept out of the task stack to avoid stack overflow.
 * mic_frame_t ~2 KB (512 floats), imu_frame_t ~12 KB (3 × 1024 floats). */
static mic_frame_t s_mic;
static imu_frame_t s_imu;

/* ---------- WiFi event sub-handlers ---------- */

static void on_wifi_sta_start(void)
{
    led_set_state(LED_CONNECTING);
    ESP_LOGI(TAG, "STA started — connecting to \"%s\"...", WIFI_SSID);
    esp_wifi_connect();
}

static void on_wifi_disconnected(wifi_event_sta_disconnected_t *d)
{
    led_set_state(LED_CONNECTING);
    xEventGroupClearBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    s_retry_cnt++;
    ESP_LOGW(TAG, "Disconnected reason=%d (attempt %d)"
             " [15/203=wrong pw  200=SSID not found]",
             d->reason, s_retry_cnt);
    if (s_retry_cnt % WIFI_MAX_RETRY == 0) {
        ESP_LOGE(TAG, "WiFi: %d consecutive failures — verify SSID/password "
                 "in wifi_creds.h (reason %d)", s_retry_cnt, d->reason);
    }
    /* Do NOT vTaskDelay here — this runs in the system event loop.
     * Blocking triggers the interrupt watchdog. */
    esp_wifi_connect();
}

static void on_got_ip(ip_event_got_ip_t *ev)
{
    ESP_LOGI(TAG, "Got IP: " IPSTR " (after %d attempt(s))",
             IP2STR(&ev->ip_info.ip), s_retry_cnt + 1);
    s_retry_cnt = 0;
    led_set_state(LED_CONNECTING);
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

/* ---------- TCP helpers ---------- */

static int tcp_connect(void)
{
    struct sockaddr_in dest_addr = {
        .sin_family = AF_INET,
        .sin_port   = htons(SERVER_PORT),
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

    int flag = 1;
    setsockopt(sock, SOL_SOCKET,  SO_KEEPALIVE, &flag, sizeof(flag));
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY,  &flag, sizeof(flag));

    /* 10-second send timeout — avoids the 75-second lwIP default block
     * when the gateway is unreachable. */
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

static int tcp_send_all(int sock, const void *buf, size_t len)
{
    const uint8_t *ptr = (const uint8_t *)buf;
    size_t remaining   = len;

    while (remaining > 0) {
        int sent = send(sock, ptr, remaining, 0);
        if (sent <= 0) {
            /* sent == 0 means gateway closed connection; sent < 0 is a real error */
            ESP_LOGE(TAG, "send() %s: errno %d",
                     sent == 0 ? "connection closed" : "failed", errno);
            return -1;
        }
        ptr       += sent;
        remaining -= (size_t)sent;
    }
    return 0;
}

/* ---------- Connection helpers ---------- */

static bool send_hello(int sock)
{
    epm_hello_t hello = {0};
    hello.magic    = EPM_HELLO_MAGIC;
    hello.fw_major = 1;
    hello.fw_minor = 0;
    esp_wifi_get_mac(WIFI_IF_STA, hello.mac);
    snprintf(hello.name, sizeof(hello.name), "SAT-%02X%02X",
             hello.mac[4], hello.mac[5]);

    if (tcp_send_all(sock, &hello, sizeof(hello)) != 0) {
        ESP_LOGE(TAG, "Hello send failed");
        return false;
    }

    ESP_LOGI(TAG, "Hello sent: name=%s  MAC=%02X:%02X:%02X:%02X:%02X:%02X",
             hello.name,
             hello.mac[0], hello.mac[1], hello.mac[2],
             hello.mac[3], hello.mac[4], hello.mac[5]);
    return true;
}

static void apply_recv_timeout(int sock)
{
    /* 100 ms timeout on recv() so the alert byte read never blocks the frame loop.
     * Non-fatal if this fails; worst case alert recv blocks ~75 s per frame. */
    struct timeval rcvto = {.tv_sec = 0, .tv_usec = 100000};
    if (setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &rcvto, sizeof(rcvto)) != 0) {
        ESP_LOGW(TAG, "SO_RCVTIMEO setsockopt failed: errno %d", errno);
    }
}

/* Wait up to 10 s for WiFi, open TCP, send hello, configure timeouts.
 * Returns a ready socket or -1 on any failure. */
static int connect_to_gateway(void)
{
    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group, WIFI_CONNECTED_BIT,
        pdFALSE, pdTRUE, pdMS_TO_TICKS(10000));

    if (!(bits & WIFI_CONNECTED_BIT)) {
        ESP_LOGW(TAG, "Still waiting for WiFi...");
        return -1;
    }

    int sock = tcp_connect();
    if (sock < 0) {
        ESP_LOGW(TAG, "TCP connect failed — retrying in 2 s");
        return -1;
    }

    if (!send_hello(sock)) {
        close(sock);
        return -1;
    }

    apply_recv_timeout(sock);
    return sock;
}

static void drop_connection(int *sock)
{
    close(*sock);
    *sock = -1;
    led_set_state(LED_CONNECTING);
}

/* ---------- Frame helpers ---------- */

static bool recv_mic_and_imu(QueueHandle_t mic_q, QueueHandle_t imu_q)
{
    if (xQueueReceive(mic_q, &s_mic, pdMS_TO_TICKS(2000)) != pdTRUE) {
        ESP_LOGW(TAG, "mic_q timeout — no data from mic_task");
        return false;
    }
    if (xQueueReceive(imu_q, &s_imu, pdMS_TO_TICKS(1500)) != pdTRUE) {
        ESP_LOGW(TAG, "imu_q timeout — no data from imu_task");
        return false;
    }
    return true;
}

static void build_header(epm_header_t *hdr, uint32_t frame_id)
{
    memset(hdr, 0, sizeof(*hdr));
    hdr->magic        = (uint32_t)EPM_MAGIC;
    hdr->frame_id     = frame_id;
    hdr->timestamp_ms = s_mic.timestamp_ms;
    hdr->mic_bins     = (uint16_t)(FFT_MIC_N / 2);
    hdr->imu_bins     = (uint16_t)(FFT_IMU_N / 2);
    hdr->imu_axes     = 3;
    hdr->mic_rms      = s_mic.rms;
    hdr->mic_crest    = s_mic.crest;
    hdr->mic_dc       = s_mic.dc;
    hdr->mic_kurtosis = s_mic.kurtosis;
    hdr->mic_clip     = s_mic.clip;
    hdr->imu_rms      = fmaxf(s_imu.rms_x, fmaxf(s_imu.rms_y, s_imu.rms_z));
    hdr->imu_crest    = fmaxf(s_imu.crest_x, fmaxf(s_imu.crest_y, s_imu.crest_z));
    hdr->imu_dc       = s_imu.dc_x;  /* X-axis DC offset — gravity/tilt component */
    hdr->imu_clip     = s_imu.clip;
}

/* Returns false on send failure — caller must drop connection and reconnect. */
static bool send_frame(int sock, const epm_header_t *hdr)
{
    uint32_t payload_bytes =
        (uint32_t)(sizeof(epm_header_t)
                   + (FFT_MIC_N / 2) * sizeof(float)
                   + (FFT_IMU_N / 2) * sizeof(float) * 3);

    int err = tcp_send_all(sock, &payload_bytes, sizeof(payload_bytes));
    if (!err) err = tcp_send_all(sock, hdr,          sizeof(*hdr));
    if (!err) err = tcp_send_all(sock, s_mic.fft_db, (FFT_MIC_N / 2) * sizeof(float));
    if (!err) err = tcp_send_all(sock, s_imu.fft_x,  (FFT_IMU_N / 2) * sizeof(float));
    if (!err) err = tcp_send_all(sock, s_imu.fft_y,  (FFT_IMU_N / 2) * sizeof(float));
    if (!err) err = tcp_send_all(sock, s_imu.fft_z,  (FFT_IMU_N / 2) * sizeof(float));

    return err == 0;
}

/*
 * Read the gateway reply after each frame.  Handles both protocol versions:
 *
 *   v1 (legacy) — 1 byte: 0x00=OK, 0x01=WARN, 0x02=FAULT.
 *   v2          — 8 bytes: epm_alert_v2_t starting with EPM_PROTO_V2_MAGIC (0xA2).
 *
 * Disambiguation: the three valid v1 bytes are 0x00/0x01/0x02.  EPM_PROTO_V2_MAGIC
 * is 0xA2, which cannot occur in a v1 reply, so the first byte unambiguously
 * identifies the protocol version — no timeout heuristic needed.
 *
 * On recv() timeout (EAGAIN/EWOULDBLOCK), *alert_out is left unchanged so the
 * LED never flickers to OK just because the gateway was slow to reply once.
 */
static bool read_gateway_alert(int sock, uint8_t *alert_out)
{
    uint8_t first = 0;
    int n = recv(sock, &first, 1, 0);

    if (n == 0) {
        ESP_LOGW(TAG, "Gateway closed connection — reconnecting");
        return false;
    }
    if (n < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return true;   /* timeout — keep previous alert level */
        }
        ESP_LOGW(TAG, "recv() error: errno %d — reconnecting", errno);
        return false;
    }

    /* ── v2 reply: 0xA2 header → read remaining 7 bytes ───────────────────── */
    if (first == EPM_PROTO_V2_MAGIC) {
        uint8_t rest[7];
        int got = 0;
        while (got < 7) {
            int r = recv(sock, rest + got, 7 - got, 0);
            if (r <= 0) {
                if (r == 0 || errno == EAGAIN || errno == EWOULDBLOCK) {
                    ESP_LOGW(TAG, "v2 reply truncated after %d bytes", 1 + got);
                    return (r == 0) ? false : true;
                }
                ESP_LOGW(TAG, "v2 recv() error: errno %d", errno);
                return false;
            }
            got += r;
        }

        epm_alert_v2_t v2;
        v2.proto_ver       = first;
        v2.alert_state     = rest[0];
        v2.fault_posterior = (uint16_t)(rest[1] | ((uint16_t)rest[2] << 8));
        v2.fft_overlap_pct = rest[3];
        v2.spec_avg_n      = rest[4];
        /* rest[5..6] = reserved, ignored */

        *alert_out = v2.alert_state;

        /* Clamp incoming values to safe ranges before writing globals */
        uint8_t new_overlap = v2.fft_overlap_pct;
        uint8_t new_avg     = v2.spec_avg_n;
        if (new_overlap != 0 && new_overlap != 25 &&
            new_overlap != 50 && new_overlap != 75) {
            new_overlap = 0;  /* reject out-of-spec values */
        }
        if (new_avg < 1 || new_avg > 16) {
            new_avg = SPEC_AVG_N;
        }

        if (new_overlap != g_adapt_overlap_pct || new_avg != g_adapt_spec_avg_n) {
            ESP_LOGI(TAG, "Adapt: overlap=%u%%  avg_n=%u  (was %u%%/%u)  p_fault=%.2f%%",
                     new_overlap, new_avg,
                     g_adapt_overlap_pct, g_adapt_spec_avg_n,
                     v2.fault_posterior / 100.0f);
            g_adapt_overlap_pct = new_overlap;
            g_adapt_spec_avg_n  = new_avg;
        }

        if (v2.alert_state != EPM_ALERT_OK) {
            ESP_LOGW(TAG, "v2 alert=%u  p_fault=%.2f%%  overlap=%u%%  avg=%u",
                     v2.alert_state, v2.fault_posterior / 100.0f,
                     new_overlap, new_avg);
        }
        return true;
    }

    /* ── v1 reply: single byte 0x00/0x01/0x02 ──────────────────────────────── */
    *alert_out = first;
    if (first != EPM_ALERT_OK) {
        ESP_LOGW(TAG, "v1 alert: 0x%02x", first);
    }
    return true;
}

static void update_led(uint8_t alert, uint32_t cal_frames)
{
    if (cal_frames < LED_CAL_FRAMES) {
        led_set_state(LED_CONNECTING);
        return;
    }
    if (alert == EPM_ALERT_FAULT) { led_set_state(LED_FAULT); return; }
    if (alert == EPM_ALERT_WARN)  { led_set_state(LED_WARN);  return; }
    led_set_state(LED_OK);
}

/* ---------- main task ---------- */

typedef struct {
    QueueHandle_t mic_q;
    QueueHandle_t imu_q;
} wifi_task_args_t;

static wifi_task_args_t s_task_args;

static void wait_for_wifi(void)
{
    ESP_LOGI(TAG, "Waiting for WiFi...");
    xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_LOGI(TAG, "WiFi ready");
}

static void wifi_task_fn(void *arg)
{
    wifi_task_args_t *args = (wifi_task_args_t *)arg;
    QueueHandle_t mic_q   = args->mic_q;
    QueueHandle_t imu_q   = args->imu_q;

    uint32_t frame_id   = 0;
    uint32_t cal_frames = 0;
    uint8_t  last_alert = EPM_ALERT_OK;  /* persists across frames; resets on reconnect */
    int sock = -1;

    wait_for_wifi();

    while (1) {
        if (sock < 0) {
            sock = connect_to_gateway();
            if (sock < 0) {
                vTaskDelay(pdMS_TO_TICKS(2000));
                continue;
            }
            cal_frames = 0;
            last_alert = EPM_ALERT_OK;
        }

        if (!recv_mic_and_imu(mic_q, imu_q)) {
            continue;
        }

        epm_header_t hdr;
        build_header(&hdr, frame_id++);

        if (!send_frame(sock, &hdr)) {
            ESP_LOGE(TAG, "TCP send failed on frame %lu — reconnecting",
                     (unsigned long)(frame_id - 1));
            drop_connection(&sock);
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }

        /* read_gateway_alert leaves last_alert unchanged on timeout so the LED
         * never flickers back to OK just because the gateway was slow to reply */
        uint8_t alert = last_alert;
        if (!read_gateway_alert(sock, &alert)) {
            drop_connection(&sock);
            continue;
        }
        last_alert = alert;

        update_led(last_alert, ++cal_frames);

        ESP_LOGD(TAG, "frame %lu: mic_rms=%.4f imu_crest=%.2f alert=0x%02x",
                 (unsigned long)(frame_id - 1), s_mic.rms, hdr.imu_crest, last_alert);
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
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
            .pmf_cfg = { .capable = true, .required = false },
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
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
