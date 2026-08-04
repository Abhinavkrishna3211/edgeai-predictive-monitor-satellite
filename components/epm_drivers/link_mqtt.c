#include "drivers/link_mqtt.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "mqtt_client.h"

#include "frame_codec/wire_protocol.h"
#include "hal/hal_transport.h"

/*
 * MQTT link driver - implements hal/hal_transport.h on top of ESP-IDF's
 * esp-mqtt component. WiFi STA itself is owned by src/wifi_task.c (see
 * docs/decisions/ADR-011-mqtt-transport-added.md); this driver only reads
 * the STA MAC (esp_wifi_get_mac()) to derive the node id and starts the
 * MQTT client once that WiFi link is already up (caller's job - see
 * src/threads/net_task.c, which blocks on wifi_wait_connected() first).
 *
 * Analog of the reference repo's satellite/src/threads/transport_task.cpp,
 * split into this HAL-conforming driver behind hal/hal_transport.h.
 */

static const char *TAG = "link_mqtt";

/* Overridable via platformio.ini build_flags, same pattern as
 * src/epm_config.h. Not folded into that header because these constants
 * are private to this driver - nothing else in the tree needs them, and
 * epm_drivers must not depend on the main component's config (src already
 * depends on epm_drivers to call link_mqtt_start(); the reverse would be
 * a component dependency cycle). TODO: confirm against the real Uno Q. */
#ifndef EPM_MQTT_BROKER_HOST
#define EPM_MQTT_BROKER_HOST "10.42.0.1"
#endif
#ifndef EPM_MQTT_BROKER_PORT
#define EPM_MQTT_BROKER_PORT 1883
#endif

#define NODE_ID_LEN 6 /* last 3 MAC octets, lowercase hex, no separators */
#define TOPIC_BUF_SIZE 32

static char s_node_id[NODE_ID_LEN + 1];
static char s_data_topic[TOPIC_BUF_SIZE];
static char s_cmd_topic[TOPIC_BUF_SIZE];

static esp_mqtt_client_handle_t s_client;
static SemaphoreHandle_t s_mutex;
static volatile bool s_connected;
static transport_cmd_fn s_cmd_handler;

static struct link_mqtt_stats s_stats;

/* Same name/behavior as the reference repo's transport_task.cpp -
 * last 3 octets of the STA MAC, lowercase hex, no separators. */
static void derive_node_id(char *out, size_t out_size)
{
	uint8_t mac[6] = {0};

	esp_wifi_get_mac(WIFI_IF_STA, mac);
	snprintf(out, out_size, "%02x%02x%02x", mac[3], mac[4], mac[5]);
}

static void mqtt_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id,
				void *event_data)
{
	(void)handler_args;
	(void)base;

	esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)event_data;

	switch ((esp_mqtt_event_id_t)event_id) {
	case MQTT_EVENT_CONNECTED:
		s_connected = true;
		s_stats.connects++;
		esp_mqtt_client_subscribe(s_client, s_cmd_topic, 1);
		ESP_LOGI(TAG, "connected, subscribed to %s", s_cmd_topic);
		break;

	case MQTT_EVENT_DISCONNECTED:
		s_connected = false;
		s_stats.disconnects++;
		ESP_LOGW(TAG, "disconnected");
		break;

	case MQTT_EVENT_DATA: {
		/* Ignore fragmented deliveries larger than our buffer would
		 * ever need - every real cmd message fits in one publish. */
		if (event->current_data_offset != 0 || event->data_len != event->total_data_len) {
			ESP_LOGW(TAG, "dropping fragmented cmd message (%d/%d bytes)",
				 event->data_len, event->total_data_len);
			break;
		}

		uint8_t type;
		const uint8_t *payload;
		size_t payload_len;

		if (!mqtt_decode_message((const uint8_t *)event->data, (size_t)event->data_len,
					  &type, &payload, &payload_len)) {
			ESP_LOGW(TAG, "malformed cmd message (%d bytes)", event->data_len);
			break;
		}

		s_stats.cmds_received++;

		if (type == MQTT_MSG_TYPE_STATUS_LED) {
			if (payload_len < sizeof(struct display_rgb_payload)) {
				ESP_LOGW(TAG, "STATUS_LED payload too short (%u < %u)",
					 (unsigned)payload_len,
					 (unsigned)sizeof(struct display_rgb_payload));
				break;
			}

			struct display_rgb_payload rgb;

			memcpy(&rgb, payload, sizeof(rgb));
			ESP_LOGI(TAG, "STATUS_LED rgb=0x%06lx mode=%u period_ms=%u",
				 (unsigned long)rgb.rgb, (unsigned)rgb.mode,
				 (unsigned)rgb.period_ms);
		}

		if (s_cmd_handler != NULL) {
			s_cmd_handler(type, payload, payload_len);
		}
		break;
	}

	default:
		break;
	}
}

int transport_init(void)
{
	if (s_mutex == NULL) {
		s_mutex = xSemaphoreCreateMutex();
		if (s_mutex == NULL) {
			return -ENOMEM;
		}
	}

	memset(&s_stats, 0, sizeof(s_stats));
	s_connected = false;
	s_cmd_handler = NULL;

	return 0;
}

const char *transport_node_id(void)
{
	return s_node_id;
}

int transport_publish_spectrum(const uint8_t *message, size_t len)
{
	if (message == NULL || len == 0) {
		return -EINVAL;
	}

	if (!s_connected || s_client == NULL) {
		return -ENOTCONN;
	}

	xSemaphoreTake(s_mutex, portMAX_DELAY);
	int msg_id = esp_mqtt_client_publish(s_client, s_data_topic, (const char *)message,
					      (int)len, 0, 0);
	xSemaphoreGive(s_mutex);

	if (msg_id < 0) {
		s_stats.publish_failures++;
		return -EIO;
	}

	s_stats.publishes++;
	return 0;
}

bool transport_is_connected(void)
{
	return s_connected;
}

int transport_set_cmd_handler(transport_cmd_fn fn)
{
	if (fn == NULL && s_cmd_handler == NULL) {
		return 0;
	}

	s_cmd_handler = fn;
	return 0;
}

int link_mqtt_start(void)
{
	int rc = transport_init();

	if (rc != 0) {
		return rc;
	}

	derive_node_id(s_node_id, sizeof(s_node_id));
	snprintf(s_data_topic, sizeof(s_data_topic), "epm/%s/data", s_node_id);
	snprintf(s_cmd_topic, sizeof(s_cmd_topic), "epm/%s/cmd", s_node_id);

	esp_mqtt_client_config_t cfg = {
		.broker.address.hostname = EPM_MQTT_BROKER_HOST,
		.broker.address.port = EPM_MQTT_BROKER_PORT,
		.broker.address.transport = MQTT_TRANSPORT_OVER_TCP,
		.credentials.client_id = s_node_id,
		.network.reconnect_timeout_ms = 2000,
		.buffer.size = 4096,
		.buffer.out_size = 4096,
		.task.stack_size = 6144,
	};

	s_client = esp_mqtt_client_init(&cfg);
	if (s_client == NULL) {
		return -ENOMEM;
	}

	esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);

	esp_err_t err = esp_mqtt_client_start(s_client);

	if (err != ESP_OK) {
		ESP_LOGE(TAG, "esp_mqtt_client_start failed: 0x%x", err);
		return -EIO;
	}

	ESP_LOGI(TAG, "node_id=%s broker=%s:%d data_topic=%s", s_node_id, EPM_MQTT_BROKER_HOST,
		 EPM_MQTT_BROKER_PORT, s_data_topic);

	return 0;
}

void link_mqtt_get_stats(struct link_mqtt_stats *out)
{
	if (out == NULL) {
		return;
	}

	*out = s_stats;
}
