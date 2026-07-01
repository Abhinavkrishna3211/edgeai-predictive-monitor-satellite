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
#include "nvs.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "lwip/err.h"

#include "mdns.h"
#include "mbedtls/gcm.h"
#include "esp_random.h"
#include "esp_pm.h"

#include "esp_attr.h"

#include "epm_config.h"
#include "epm_protocol.h"
#include "rgb_led_task.h"
#include "wifi_task.h"
#include "mic_capture.h"  /* snapshot_count(), snapshot_read_chunk(), get_overflow_count() */

/* ---------- module constants ---------- */

static const char *TAG = "wifi_task";

#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_MAX_RETRY      10

/* Total plaintext length per frame: header + mic FFT + 3 × IMU FFT */
#define EPM_PLAIN_LEN  ((size_t)( \
    sizeof(epm_header_t) + \
    (FFT_MIC_N / 2) * sizeof(float) + \
    (FFT_IMU_N / 2) * sizeof(float) * 3))

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

/* AES-128-GCM encryption state — hardware-accelerated on ESP32-S3 via mbedtls */
static mbedtls_gcm_context s_gcm_ctx;
static uint8_t             s_aes_key[16];

/* HW-OPT: DMA_ATTR places AES staging buffers in internal DRAM, aligned for
 * GDMA access.  The ESP32-S3 AES accelerator uses GDMA to transfer plaintext
 * and ciphertext — if these buffers were in PSRAM the GDMA would access them
 * through the PSRAM cache, causing silently incorrect ciphertext on cache
 * evictions that occur during concurrent WiFi GDMA operations.
 * (~14 KB each; internal DRAM headroom checked at boot via heap_caps_get_free_size.) */
static DMA_ATTR uint8_t s_enc_pt[EPM_PLAIN_LEN];
static DMA_ATTR uint8_t s_enc_ct[EPM_PLAIN_LEN];

/* ---------- WiFi event sub-handlers ---------- */

static void on_wifi_sta_start(void)
{
    rgb_led_set_state(RGB_WIFI_CONN);
    ESP_LOGI(TAG, "STA started — connecting to \"%s\"...", WIFI_SSID);
    esp_wifi_connect();
}

static void on_wifi_disconnected(wifi_event_sta_disconnected_t *d)
{
    rgb_led_set_state(RGB_WIFI_CONN);
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

/* ---------- mDNS discovery ---------- */

/* Query for the EPM gateway service via mDNS.
 * Returns the IPv4 address in network byte order, or 0 if not found / timed out. */
static uint32_t resolve_gateway_mdns(void)
{
    mdns_result_t *results = NULL;
    esp_err_t err = mdns_query_ptr("_epm-gateway", "_tcp", 3000, 1, &results);
    if (err != ESP_OK || !results) {
        return 0;
    }
    uint32_t ip = 0;
    mdns_ip_addr_t *addr = results->addr;
    while (addr) {
        if (addr->addr.type == IPADDR_TYPE_V4) {
            ip = addr->addr.u_addr.ip4.addr;
            break;
        }
        addr = addr->next;
    }
    mdns_query_results_free(results);
    return ip;
}

/* ---------- PSK key management ---------- */

static void load_psk_from_nvs(void)
{
    nvs_handle_t h;
    if (nvs_open("epm_sec", NVS_READONLY, &h) == ESP_OK) {
        size_t len = sizeof(s_aes_key);
        esp_err_t err = nvs_get_blob(h, "psk", s_aes_key, &len);
        nvs_close(h);
        if (err == ESP_OK && len == sizeof(s_aes_key)) {
            ESP_LOGI(TAG, "AES-128 key loaded from NVS (epm_sec/psk)");
            return;
        }
    }
#ifdef EPM_ENCRYPT_FRAMES
    memcpy(s_aes_key, EPM_PSK, sizeof(s_aes_key));
    ESP_LOGW(TAG, "AES key not in NVS — using build-time PSK "
             "(provision via nvs_set_blob(\"epm_sec\",\"psk\",...) for production)");
#else
    memset(s_aes_key, 0, sizeof(s_aes_key));
#endif
}

/* ---------- AES-128-GCM helpers ---------- */

static void encrypt_init(void)
{
    load_psk_from_nvs();
    mbedtls_gcm_init(&s_gcm_ctx);
    int ret = mbedtls_gcm_setkey(&s_gcm_ctx, MBEDTLS_CIPHER_ID_AES, s_aes_key, 128);
    if (ret != 0) {
        ESP_LOGE(TAG, "mbedtls_gcm_setkey failed: -0x%04X", (unsigned)(-ret));
    } else {
        ESP_LOGI(TAG, "AES-128-GCM ready (ESP32-S3 hardware AES accelerator)");
    }
}

/* Encrypt plaintext into ciphertext in-place; fills iv[12] and tag[16].
 * Uses the ESP32-S3 hardware TRNG for nonce generation — no seeding needed. */
static int encrypt_frame_data(const uint8_t *pt, size_t pt_len,
                               uint8_t *ct, uint8_t iv[12], uint8_t tag[16])
{
    esp_fill_random(iv, 12);
    return mbedtls_gcm_crypt_and_tag(
        &s_gcm_ctx, MBEDTLS_GCM_ENCRYPT,
        pt_len,
        iv, 12,
        NULL, 0,
        pt, ct,
        16, tag);
}

/* ---------- TCP helpers ---------- */

static int tcp_connect(void)
{
    struct sockaddr_in dest_addr = {
        .sin_family = AF_INET,
        .sin_port   = htons(SERVER_PORT),
    };

    /* Try mDNS discovery first; fall back to static SERVER_IP */
    uint32_t mdns_ip = resolve_gateway_mdns();
    if (mdns_ip != 0) {
        dest_addr.sin_addr.s_addr = mdns_ip;
        char ip_str[16];
        esp_ip4addr_ntoa((esp_ip4_addr_t *)&mdns_ip, ip_str, sizeof(ip_str));
        ESP_LOGI(TAG, "mDNS: discovered gateway at %s — SERVER_IP override not needed",
                 ip_str);
    } else {
        ESP_LOGW(TAG, "mDNS: no result in 3 s — falling back to SERVER_IP %s", SERVER_IP);
        if (inet_aton(SERVER_IP, &dest_addr.sin_addr) == 0) {
            ESP_LOGE(TAG, "Invalid SERVER_IP: %s", SERVER_IP);
            return -1;
        }
    }

    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket() failed: errno %d", errno);
        return -1;
    }

    int flag = 1;
    setsockopt(sock, SOL_SOCKET,  SO_KEEPALIVE, &flag, sizeof(flag));
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY,  &flag, sizeof(flag));

    /* HW-OPT: keepalive tuning (5/2/3) — detects a silently dead gateway
     * in 5 + 2×3 = 11 s vs the LWIP default which takes ~75 s to expire.
     * Without this, a crashed gateway keeps the satellite stuck on send() for
     * up to 75 s (SO_SNDTIMEO = 10 s, but keepalive fires first if set). */
    int keepidle  = 5;   /* seconds idle before first probe */
    int keepintvl = 2;   /* seconds between probes */
    int keepcnt   = 3;   /* probes before connection declared dead */
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPIDLE,  &keepidle,  sizeof(keepidle));
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPINTVL, &keepintvl, sizeof(keepintvl));
    setsockopt(sock, IPPROTO_TCP, TCP_KEEPCNT,   &keepcnt,   sizeof(keepcnt));

    /* 10-second send timeout — avoids the 75-second lwIP default block
     * when the gateway is unreachable. */
    struct timeval sndto = {.tv_sec = 10, .tv_usec = 0};
    if (setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &sndto, sizeof(sndto)) != 0) {
        ESP_LOGW(TAG, "SO_SNDTIMEO setsockopt failed: errno %d", errno);
    }

    if (connect(sock, (struct sockaddr *)&dest_addr, sizeof(dest_addr)) != 0) {
        char ip_s[16];
        esp_ip4addr_ntoa((esp_ip4_addr_t *)&dest_addr.sin_addr.s_addr, ip_s, sizeof(ip_s));
        ESP_LOGE(TAG, "connect() to %s:%d failed: errno %d", ip_s, SERVER_PORT, errno);
        close(sock);
        return -1;
    }
    {
        char ip_s[16];
        esp_ip4addr_ntoa((esp_ip4_addr_t *)&dest_addr.sin_addr.s_addr, ip_s, sizeof(ip_s));
        ESP_LOGI(TAG, "TCP connected to %s:%d", ip_s, SERVER_PORT);
    }
    return sock;
}

static int tcp_send_all(int sock, const void *buf, size_t len)
{
    const uint8_t *ptr = (const uint8_t *)buf;
    size_t remaining   = len;
    while (remaining > 0) {
        int sent = send(sock, ptr, remaining, 0);
        if (sent <= 0) {
            ESP_LOGE(TAG, "send() %s: errno %d",
                     sent == 0 ? "connection closed" : "failed", errno);
            return -1;
        }
        ptr       += sent;
        remaining -= (size_t)sent;
    }
    return 0;
}

/* HW-OPT: MSG_MORE batches intermediate send() calls into fewer TCP segments.
 * With TCP_NODELAY set, each send() would otherwise flush immediately.
 * MSG_MORE defers the push until the final send() (without MSG_MORE), reducing
 * segment count from 6 per frame to 1 per frame for the non-encrypted path.
 * Expected improvement: ~5 fewer ACK round-trips per frame at 2.2 fps. */
static int __attribute__((unused)) tcp_send_more(int sock, const void *buf, size_t len)
{
    const uint8_t *ptr = (const uint8_t *)buf;
    size_t remaining   = len;
    while (remaining > 0) {
        int sent = send(sock, ptr, remaining, MSG_MORE);
        if (sent <= 0) {
            ESP_LOGE(TAG, "send(MSG_MORE) %s: errno %d",
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
    rgb_led_set_state(RGB_WIFI_CONN);
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

    /* I2S DMA overflow count: delta since last frame, saturated at 255.
     * Gateway uses this to flag frames that may have a gap in audio data. */
    static uint32_t s_last_overflow = 0;
    uint32_t cur_overflow = mic_capture_get_overflow_count();
    uint32_t delta        = cur_overflow - s_last_overflow;
    hdr->overflow_count   = (uint8_t)(delta > 255u ? 255u : delta);
    s_last_overflow       = cur_overflow;
}

/* Returns false on send failure — caller must drop connection and reconnect. */
static bool send_frame(int sock, const epm_header_t *hdr)
{
#ifdef EPM_ENCRYPT_FRAMES
    /* Pack plaintext: header || mic_fft || imu_x || imu_y || imu_z */
    uint8_t *p = s_enc_pt;
    memcpy(p, hdr,          sizeof(*hdr));              p += sizeof(*hdr);
    memcpy(p, s_mic.fft_db, (FFT_MIC_N / 2) * sizeof(float));
    p += (FFT_MIC_N / 2) * sizeof(float);
    memcpy(p, s_imu.fft_x,  (FFT_IMU_N / 2) * sizeof(float));
    p += (FFT_IMU_N / 2) * sizeof(float);
    memcpy(p, s_imu.fft_y,  (FFT_IMU_N / 2) * sizeof(float));
    p += (FFT_IMU_N / 2) * sizeof(float);
    memcpy(p, s_imu.fft_z,  (FFT_IMU_N / 2) * sizeof(float));

    uint8_t iv[12], tag[16];
    if (encrypt_frame_data(s_enc_pt, EPM_PLAIN_LEN, s_enc_ct, iv, tag) != 0) {
        ESP_LOGE(TAG, "AES-GCM encryption failed");
        return false;
    }

    /* Wire: [payload_bytes=12+N+16][iv[12]][ciphertext[N]][tag[16]] */
    uint32_t payload_bytes = (uint32_t)(12u + EPM_PLAIN_LEN + 16u);
    int err = tcp_send_all(sock, &payload_bytes, sizeof(payload_bytes));
    if (!err) err = tcp_send_all(sock, iv,       12);
    if (!err) err = tcp_send_all(sock, s_enc_ct, EPM_PLAIN_LEN);
    if (!err) err = tcp_send_all(sock, tag,      16);
    return err == 0;
#else
    uint32_t payload_bytes =
        (uint32_t)(sizeof(epm_header_t)
                   + (FFT_MIC_N / 2) * sizeof(float)
                   + (FFT_IMU_N / 2) * sizeof(float) * 3);

    /* HW-OPT: tcp_send_more for all-but-last segment — see tcp_send_more() above. */
    int err = tcp_send_more(sock, &payload_bytes, sizeof(payload_bytes));
    if (!err) err = tcp_send_more(sock, hdr,          sizeof(*hdr));
    if (!err) err = tcp_send_more(sock, s_mic.fft_db, (FFT_MIC_N / 2) * sizeof(float));
    if (!err) err = tcp_send_more(sock, s_imu.fft_x,  (FFT_IMU_N / 2) * sizeof(float));
    if (!err) err = tcp_send_more(sock, s_imu.fft_y,  (FFT_IMU_N / 2) * sizeof(float));
    if (!err) err = tcp_send_all( sock, s_imu.fft_z,  (FFT_IMU_N / 2) * sizeof(float));
    return err == 0;
#endif
}

/* Send the PSRAM pre-trigger ring buffer to the gateway as a raw int16_t
 * stream prefixed by a uint32_t byte count.  Sent in 4 KB chunks to avoid
 * allocating a 128 KB contiguous stack buffer. */
static bool snapshot_send_tcp(int sock)
{
    size_t count = snapshot_count();
    if (count == 0) {
        ESP_LOGW(TAG, "snapshot requested but ring buffer empty or unavailable");
        return true;
    }
    uint32_t snap_len = (uint32_t)(count * sizeof(int16_t));
    ESP_LOGI(TAG, "Sending snapshot: %lu bytes (%.1f s)",
             (unsigned long)snap_len, (float)count / (float)MIC_FS_HZ);
    if (tcp_send_all(sock, &snap_len, sizeof(snap_len)) != 0) return false;

    static uint8_t s_snap_chunk[4096];
    size_t offset = 0;
    while (offset < snap_len) {
        size_t n = snapshot_read_chunk(offset, s_snap_chunk, sizeof(s_snap_chunk));
        if (n == 0) break;
        if (tcp_send_all(sock, s_snap_chunk, n) != 0) return false;
        offset += n;
    }
    return true;
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
static bool read_gateway_alert(int sock, uint8_t *alert_out, bool *snap_out)
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
        v2.flags           = rest[5];
        /* rest[6] = reserved */

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

        if (snap_out) {
            *snap_out = (v2.flags & EPM_SNAPSHOT_REQUEST) != 0;
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
        rgb_led_set_state(RGB_CALIBRATING); return;
    }
    if (!g_hst_warmed_up) {
        rgb_led_set_state(RGB_LEARNING); return;
    }
    if (alert == EPM_ALERT_FAULT) { rgb_led_set_state(RGB_FAULT); return; }
    if (alert == EPM_ALERT_WARN)  { rgb_led_set_state(RGB_WARN);  return; }
    rgb_led_set_state(RGB_OK);
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

    /* Dynamic CPU frequency scaling: 240 MHz during active DSP/TCP bursts,
     * 80 MHz during idle gaps between frames.  Light sleep between tasks
     * further cuts radio modem draw by ~30%.  Requires CONFIG_PM_ENABLE=y and
     * CONFIG_FREERTOS_USE_TICKLESS_IDLE=y in sdkconfig.defaults. */
    esp_pm_config_t pm_cfg = {
        .max_freq_mhz     = 240,
        .min_freq_mhz     = 80,
        .light_sleep_enable = true,
    };
    if (esp_pm_configure(&pm_cfg) != ESP_OK) {
        ESP_LOGW(TAG, "esp_pm_configure failed — fixed 240 MHz");
    }
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);

    /* mDNS — must be initialised after WiFi brings up the network interface */
    if (mdns_init() != ESP_OK) {
        ESP_LOGW(TAG, "mdns_init() failed — will use static SERVER_IP as fallback");
    }

    /* AES-128-GCM — load PSK (NVS or build-time) and arm the hardware cipher */
    encrypt_init();

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
        uint8_t alert      = last_alert;
        bool    snap_req   = false;
        if (!read_gateway_alert(sock, &alert, &snap_req)) {
            drop_connection(&sock);
            continue;
        }
        last_alert = alert;

        if (snap_req) {
            if (!snapshot_send_tcp(sock)) {
                ESP_LOGE(TAG, "snapshot TCP send failed — reconnecting");
                drop_connection(&sock);
                continue;
            }
        }

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

    /* HW-OPT: cap TX power to 17 dBm to reduce peak current draw on the
     * XIAO USB-C 3.3 V rail.  Negligible range loss at <10 m deployment.
     * Unit: quarter-dBm.  WIFI_TX_POWER_QTR_DBM=68 → 17.0 dBm. */
    if (esp_wifi_set_max_tx_power(WIFI_TX_POWER_QTR_DBM) == ESP_OK) {
        ESP_LOGI(TAG, "WiFi TX power capped to %.1f dBm (%d q-dBm)",
                 WIFI_TX_POWER_QTR_DBM / 4.0f, (int)WIFI_TX_POWER_QTR_DBM);
    } else {
        ESP_LOGW(TAG, "esp_wifi_set_max_tx_power failed — using default");
    }

    ESP_LOGI(TAG, "WiFi RF init — SSID: \"%s\", target: %s:%d (mDNS discovery first)",
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
static TaskHandle_t s_task_handle = NULL;
TaskHandle_t wifi_task_get_handle(void) { return s_task_handle; }

void wifi_task_start(QueueHandle_t mic_q, QueueHandle_t imu_q)
{
    s_task_args.mic_q = mic_q;
    s_task_args.imu_q = imu_q;
    xTaskCreatePinnedToCore(wifi_task_fn, "wifi_task", TASK_STACK_WIFI,
                            &s_task_args, TASK_PRIO_WIFI, &s_task_handle, 0);
}
