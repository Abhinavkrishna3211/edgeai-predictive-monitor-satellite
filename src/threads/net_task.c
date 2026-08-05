/*
 * net_task.c — MQTT telemetry publish loop (Phase 0.5, real data since 6c).
 *
 * Blocks on wifi_task.h's wifi_wait_connected() (WiFi STA bring-up lives in
 * threads/wifi_task.c — see docs/decisions/ADR-022-wifi-task-revived.md and
 * docs/decisions/ADR-023-transport-adrs-superseded.md), then starts the
 * MQTT link (components/epm_drivers/link_mqtt.c) and
 * publishes one section-list telemetry frame
 * (components/epm_codec/include/frame_codec/spectrum_codec.h) every
 * EPM_NET_PUBLISH_INTERVAL_MS, built from real mic/IMU FFT output delivered
 * via dsp_task_get_queue()/imu_task_get_queue() — this task is each
 * producer's sole consumer (see docs/decisions/ADR-021-net-task-second-consumer-queue.md's
 * addendum: the second-queue split it introduced collapsed back to one
 * queue per producer once tcp_task.c, the other consumer, was deleted).
 *
 * No frame is published until both queues have delivered at least one real
 * frame (docs/BASE_STATION_CONTRACT.md line 24: a present, real-bin_count,
 * all-zero channel reads as genuine silence to the model, not "no data
 * yet" — zero-filling before real data exists would be indistinguishable
 * from that). Once both sides have delivered once, every tick publishes
 * whatever is currently cached, refreshed opportunistically each tick.
 *
 * This task also owns the inbound cmd-topic wiring: it registers the sole
 * transport_set_cmd_handler() caller in the tree (Phase 7c) so a decoded
 * STATUS_LED command reaches the display driver instead of only being
 * logged, and polls transport_is_connected() once per tick to revert the
 * display to a local state on MQTT disconnect (ADR-025).
 */

#include "threads/net_task.h"

#include <errno.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "dsp/spectrum.h"
#include "frame_codec/scalar_map.h"
#include "frame_codec/spectrum_codec.h"
#include "frame_codec/telemetry_schema.h"
#include "frame_codec/wire_protocol.h"
#include "hal/hal_display.h"
#include "hal/hal_transport.h"
#include "drivers/link_mqtt.h"

#include "epm_config.h"
#include "threads/wifi_task.h"

static const char *TAG = "net_task";

/* net_task_start()'s two queue args, handed to net_task_fn via
 * xTaskCreatePinnedToCore's arg pointer — ADR-021. */
typedef struct {
	QueueHandle_t mic_q;
	QueueHandle_t imu_q;
} net_task_args_t;

static net_task_args_t s_task_args;

/* Cached last-received frames. mic_frame_t (~2.1 KB) and imu_frame_t
 * (~12.4 KB) are both too large for TASK_STACK_NET (4096 B), so
 * xQueueReceive() always writes directly into these file-scope statics,
 * never a stack temporary. EXT_RAM_BSS_ATTR places them in PSRAM — the same placement
 * already used for this size class (dsp_task.c's s_mag_db, imu_task.c's
 * s_frame) — to protect the tight internal-DRAM heap margin
 * docs/decisions/ADR-020-bin-count-downsampled-not-buffer-enlarged.md
 * documents. xQueueReceive has no DMA constraint on this path (unlike the
 * KX134 FIFO buffer), so writing into PSRAM here is safe. */
static EXT_RAM_BSS_ATTR mic_frame_t s_last_mic;
static EXT_RAM_BSS_ATTR imu_frame_t s_last_imu;
static bool s_have_mic = false;
static bool s_have_imu = false;

/* Reduced (EPM_MODEL_SPECTRUM_BINS-wide) spectra, rebuilt each publish tick.
 * Small enough to stay plain internal DRAM. */
static float s_mic_bins[EPM_MODEL_SPECTRUM_BINS];
static float s_accel_x_bins[EPM_MODEL_SPECTRUM_BINS];
static float s_accel_y_bins[EPM_MODEL_SPECTRUM_BINS];
static float s_accel_z_bins[EPM_MODEL_SPECTRUM_BINS];

static uint8_t frame_buf[EPM_NET_FRAME_BUF_BYTES];

/* Builds the 5-section frame (4 SPECTRUM + 1 SCALAR_SET) from the currently
 * cached s_last_mic/s_last_imu. Returns the encoded length, or 0 if
 * out_buf_size is too small or a bin-reduction call fails
 * (telemetry_build_frame()'s / epm_dsp_reduce_bins()'s convention). */
static size_t build_real_frame(uint8_t *out_buf, size_t out_buf_size)
{
	if (epm_dsp_reduce_bins(s_last_mic.fft_db, FFT_MIC_N / 2, s_mic_bins, EPM_MODEL_SPECTRUM_BINS) != 0 ||
	    epm_dsp_reduce_bins(s_last_imu.fft_x, FFT_IMU_N / 2, s_accel_x_bins, EPM_MODEL_SPECTRUM_BINS) != 0 ||
	    epm_dsp_reduce_bins(s_last_imu.fft_y, FFT_IMU_N / 2, s_accel_y_bins, EPM_MODEL_SPECTRUM_BINS) != 0 ||
	    epm_dsp_reduce_bins(s_last_imu.fft_z, FFT_IMU_N / 2, s_accel_z_bins, EPM_MODEL_SPECTRUM_BINS) != 0) {
		ESP_LOGE(TAG, "epm_dsp_reduce_bins failed");
		return 0;
	}

	struct spectrum_channel channels[4] = {
		{.channel_id = TELEM_CHANNEL_MIC,
		 .fs = (float)MIC_FS_HZ,
		 .fft_size = (uint16_t)FFT_MIC_N,
		 .bin_count = EPM_MODEL_SPECTRUM_BINS,
		 .bins = s_mic_bins},
		{.channel_id = TELEM_CHANNEL_ACCEL_X,
		 .fs = (float)IMU_FS_HZ,
		 .fft_size = (uint16_t)FFT_IMU_N,
		 .bin_count = EPM_MODEL_SPECTRUM_BINS,
		 .bins = s_accel_x_bins},
		{.channel_id = TELEM_CHANNEL_ACCEL_Y,
		 .fs = (float)IMU_FS_HZ,
		 .fft_size = (uint16_t)FFT_IMU_N,
		 .bin_count = EPM_MODEL_SPECTRUM_BINS,
		 .bins = s_accel_y_bins},
		{.channel_id = TELEM_CHANNEL_ACCEL_Z,
		 .fs = (float)IMU_FS_HZ,
		 .fft_size = (uint16_t)FFT_IMU_N,
		 .bin_count = EPM_MODEL_SPECTRUM_BINS,
		 .bins = s_accel_z_bins},
	};

	struct axis_scalars sc_x = {
		.rms = s_last_imu.rms_x, .kurtosis = s_last_imu.kurtosis_x, .std = s_last_imu.std_x,
		.peak = s_last_imu.peak_x, .crest = s_last_imu.crest_x, .skewness = s_last_imu.skewness_x,
	};
	struct axis_scalars sc_y = {
		.rms = s_last_imu.rms_y, .kurtosis = s_last_imu.kurtosis_y, .std = s_last_imu.std_y,
		.peak = s_last_imu.peak_y, .crest = s_last_imu.crest_y, .skewness = s_last_imu.skewness_y,
	};
	struct axis_scalars sc_z = {
		.rms = s_last_imu.rms_z, .kurtosis = s_last_imu.kurtosis_z, .std = s_last_imu.std_z,
		.peak = s_last_imu.peak_z, .crest = s_last_imu.crest_z, .skewness = s_last_imu.skewness_z,
	};
	struct axis_scalars sc_mic = {
		.rms = s_last_mic.rms, .kurtosis = s_last_mic.kurtosis, .std = s_last_mic.std,
		.peak = s_last_mic.peak, .crest = s_last_mic.crest, .skewness = s_last_mic.skewness,
	};

	struct scalar_entry scalars[24];

	scalar_map_build_axis(&sc_x, TELEM_SCALAR_RMS_X, &scalars[0]);
	scalar_map_build_axis(&sc_y, TELEM_SCALAR_RMS_Y, &scalars[6]);
	scalar_map_build_axis(&sc_z, TELEM_SCALAR_RMS_Z, &scalars[12]);
	scalar_map_build_axis(&sc_mic, TELEM_SCALAR_RMS_MIC, &scalars[18]);

	return telemetry_build_frame(channels, 4, scalars, 24, out_buf, out_buf_size);
}

/* transport_set_cmd_handler()'s registered callback (Phase 7c). Decodes a
 * STATUS_LED command and drives the display directly - the only defined
 * cmd type today (frame_codec/wire_protocol.h). */
static void net_task_cmd_handler(uint8_t type, const uint8_t *body, size_t len)
{
	if (type != MQTT_MSG_TYPE_STATUS_LED) {
		return;
	}

	struct display_rgb_payload rgb;

	if (!mqtt_decode_status_led(body, len, &rgb)) {
		ESP_LOGW(TAG, "cmd handler: malformed STATUS_LED payload (%u bytes)",
			 (unsigned)len);
		return;
	}

	rgb_led_set_remote(rgb.rgb, rgb.mode, rgb.period_ms);
}

#if CONFIG_EPM_STATUS_LED_SELFTEST
/* TEMPORARY, test-only (Phase 7c hardware validation, see ADR-025). Fires a
 * manufactured STATUS_LED straight at the cmd handler net_task_fn() just
 * registered, so the decode-dispatch-LED-update path can be confirmed on
 * real hardware without a working MQTT broker connection (ADR-011's known
 * TCP-handshake stall). Blueviolet/BREATHE/500ms - deliberately not any
 * color in display_neopixel.c's k_pattern[] table, so a human watching the
 * board can tell this fired apart from any local state it might already be
 * showing.
 *
 * Runs from its own short-lived task, delayed ~90s past boot, instead of
 * firing inline in net_task_fn() at registration time: ADR-025's
 * last-write-wins single-slot queue means firing immediately at boot loses
 * the race against dsp_task.c's own one-shot rgb_led_set_state(RGB_OK) at
 * HST warm-up (~250 mic frames in, observed ~60-70s after boot), which
 * silently overwrites the remote color before a human has a chance to look
 * (confirmed on hardware: an immediate-fire version showed blueviolet only
 * in the first minute, then reverted to solid green with no further state
 * changes after). Firing after warm-up instead makes the self-test color
 * the last write, so it persists indefinitely for observation. */
static void net_task_selftest_task(void *arg)
{
	(void)arg;
	vTaskDelay(pdMS_TO_TICKS(90000));

	struct display_rgb_payload selftest_rgb = {
		.rgb = 0x8A2BE2, .mode = 1, .period_ms = 500,
	};
	uint8_t selftest_buf[sizeof(selftest_rgb)];

	memcpy(selftest_buf, &selftest_rgb, sizeof(selftest_rgb));
	ESP_LOGW(TAG, "EPM_STATUS_LED_SELFTEST: firing manufactured STATUS_LED at cmd handler");
	net_task_cmd_handler(MQTT_MSG_TYPE_STATUS_LED, selftest_buf, sizeof(selftest_buf));

	vTaskDelete(NULL);
}
#endif

static void net_task_fn(void *arg)
{
	net_task_args_t *args = (net_task_args_t *)arg;
	QueueHandle_t mic_q = args->mic_q;
	QueueHandle_t imu_q = args->imu_q;
	bool mqtt_was_connected = false;

	wifi_wait_connected(portMAX_DELAY);

	/* Free internal-DRAM heap immediately before esp-mqtt's client init —
	 * the measurement Phase 7b's ADR-024 decision is based on (ADR-017:
	 * esp_mqtt_client_init() null-derefs later if esp_event_loop_create()
	 * fails here under tight margin). */
	ESP_LOGI(TAG, "free heap before link_mqtt_start(): internal=%lu",
		 (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));

	int rc = link_mqtt_start();

	if (rc != 0) {
		ESP_LOGE(TAG, "link_mqtt_start failed: %d", rc);
	}

	/* Registered after link_mqtt_start() (not before): link_mqtt_start()
	 * calls transport_init() internally, which unconditionally resets the
	 * cmd handler to NULL, so registering any earlier would just get
	 * wiped. Unconditional on rc: transport_init() has already run either
	 * way, and there's no retry loop that would re-wipe this later. */
	transport_set_cmd_handler(net_task_cmd_handler);

#if CONFIG_EPM_STATUS_LED_SELFTEST
	/* TEMPORARY, test-only (Phase 7c hardware validation, see ADR-025 and
	 * net_task_selftest_task()'s comment above for why this is deferred
	 * to a delayed background task instead of firing inline here). */
	xTaskCreate(net_task_selftest_task, "led_selftest", 2048, NULL, 3, NULL);
#endif

	while (1) {
		/* MQTT-level disconnect revert (ADR-025): a broker-session drop
		 * doesn't imply WiFi dropped too, so wifi_task.c's own
		 * WIFI_EVENT_STA_DISCONNECTED revert won't fire for this case -
		 * this is the path that covers it. */
		bool mqtt_connected = transport_is_connected();

		if (mqtt_was_connected && !mqtt_connected) {
			ESP_LOGW(TAG, "MQTT disconnected — reverting display to local state");
			rgb_led_set_state(RGB_WIFI_CONN);
		}
		mqtt_was_connected = mqtt_connected;

		if (xQueueReceive(mic_q, &s_last_mic, 0) == pdTRUE) {
			s_have_mic = true;
		}
		if (xQueueReceive(imu_q, &s_last_imu, 0) == pdTRUE) {
			s_have_imu = true;
		}

		if (!s_have_mic || !s_have_imu) {
			vTaskDelay(pdMS_TO_TICKS(EPM_NET_PUBLISH_INTERVAL_MS));
			continue;
		}

		size_t len = build_real_frame(frame_buf, sizeof(frame_buf));

		if (len == 0) {
			ESP_LOGE(TAG, "frame build failed (buffer too small?)");
		} else {
			int pub_rc = transport_publish_spectrum(frame_buf, len);

			if (pub_rc != 0 && pub_rc != -ENOTCONN) {
				ESP_LOGW(TAG, "publish failed: %d", pub_rc);
			}
		}

		vTaskDelay(pdMS_TO_TICKS(EPM_NET_PUBLISH_INTERVAL_MS));
	}
}

int net_task_start(QueueHandle_t mic_q, QueueHandle_t imu_q)
{
	s_task_args.mic_q = mic_q;
	s_task_args.imu_q = imu_q;

	TaskHandle_t handle = NULL;
	BaseType_t ok = xTaskCreatePinnedToCore(net_task_fn, "net", TASK_STACK_NET, &s_task_args,
						 TASK_PRIO_NET, &handle, 0);

	return (ok == pdPASS) ? 0 : -ENOMEM;
}
