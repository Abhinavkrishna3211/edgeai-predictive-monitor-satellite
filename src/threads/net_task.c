/*
 * net_task.c — MQTT telemetry publish loop (Phase 0.5).
 *
 * Blocks on tcp_task.h's wifi_wait_connected() (WiFi STA bring-up stays
 * owned by tcp_task.c — see docs/decisions/ADR-011-mqtt-transport-added.md),
 * then starts the MQTT link (components/epm_drivers/link_mqtt.c) and
 * publishes one section-list telemetry frame
 * (components/epm_codec/include/frame_codec/spectrum_codec.h) every
 * EPM_NET_PUBLISH_INTERVAL_MS.
 *
 * The frame's bins/scalars are synthetic this phase — a slow-moving,
 * non-zero shape so the base station's dashboard chart is visibly alive.
 * Real mic/accel data replaces them in Phase 6; only the channel layout
 * (mic + accel_x/y/z spectra + one PERF scalar set) needs to stay fixed,
 * since the base station's registry commits sensor_config/input_dim from
 * this node's first frame and silently drops any later frame that doesn't
 * match it exactly.
 */

#include "threads/net_task.h"

#include <errno.h>
#include <math.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "frame_codec/spectrum_codec.h"
#include "frame_codec/telemetry_schema.h"
#include "hal/hal_transport.h"
#include "drivers/link_mqtt.h"

#include "epm_config.h"
#include "threads/tcp_task.h"

static const char *TAG = "net_task";

/* net_task_start()'s two queue args, handed to net_task_fn via
 * xTaskCreatePinnedToCore's arg pointer (mirrors tcp_task.c's own
 * s_task_args pattern) — ADR-021. */
typedef struct {
	QueueHandle_t mic_q;
	QueueHandle_t imu_q;
} net_task_args_t;

static net_task_args_t s_task_args;

/* Fills bins[0..n) with a decaying-ramp shape plus a slow-moving Gaussian
 * peak so consecutive frames visibly differ on the dashboard's spectrum
 * plot without needing real DSP input yet. */
static void fill_synthetic_bins(float *bins, size_t n, uint32_t phase)
{
	float peak_pos = (float)(phase % n);

	for (size_t i = 0; i < n; i++) {
		float decay = expf(-(float)i / ((float)n * 0.25f));
		float dist = (float)i - peak_pos;
		float peak = expf(-(dist * dist) / (2.0f * 4.0f * 4.0f));

		bins[i] = 0.05f * decay + 0.5f * peak;
	}
}

/* One axis' 6 scalars (rms, kurtosis, std, peak, crest_factor, skewness),
 * ids id_base..id_base+5 — TELEM_SCALAR_RMS_X..SKEWNESS_X and its Y/Z/MIC
 * siblings are each 6 consecutive ids in exactly this order
 * (components/epm_codec/include/frame_codec/telemetry_schema.h). */
static void fill_axis_scalars(struct scalar_entry *entries, uint16_t id_base, uint32_t phase)
{
	float wobble = 0.01f * sinf((float)phase * 0.05f);

	entries[0] = (struct scalar_entry){.id = (uint16_t)(id_base + 0), .value = 0.10f + wobble};
	entries[1] = (struct scalar_entry){.id = (uint16_t)(id_base + 1), .value = 3.00f};
	entries[2] = (struct scalar_entry){.id = (uint16_t)(id_base + 2), .value = 0.10f + wobble};
	entries[3] = (struct scalar_entry){.id = (uint16_t)(id_base + 3), .value = 0.40f};
	entries[4] = (struct scalar_entry){.id = (uint16_t)(id_base + 4), .value = 4.00f};
	entries[5] = (struct scalar_entry){.id = (uint16_t)(id_base + 5), .value = 0.00f};
}

/* Builds the 5-section frame (4 SPECTRUM + 1 SCALAR_SET) that commits this
 * node's sensor_config = {mic, accel_x, accel_y, accel_z}, input_dim = 536
 * on the base station's registry. Returns the encoded length, or 0 if
 * out_buf_size is too small (telemetry_build_frame()'s convention). */
static size_t build_synthetic_frame(uint8_t *out_buf, size_t out_buf_size, uint32_t phase)
{
	static float mic_bins[EPM_MODEL_SPECTRUM_BINS];
	static float accel_x_bins[EPM_MODEL_SPECTRUM_BINS];
	static float accel_y_bins[EPM_MODEL_SPECTRUM_BINS];
	static float accel_z_bins[EPM_MODEL_SPECTRUM_BINS];

	fill_synthetic_bins(mic_bins, EPM_MODEL_SPECTRUM_BINS, phase);
	fill_synthetic_bins(accel_x_bins, EPM_MODEL_SPECTRUM_BINS, phase + 7);
	fill_synthetic_bins(accel_y_bins, EPM_MODEL_SPECTRUM_BINS, phase + 13);
	fill_synthetic_bins(accel_z_bins, EPM_MODEL_SPECTRUM_BINS, phase + 19);

	struct spectrum_channel channels[4] = {
		{.channel_id = TELEM_CHANNEL_MIC,
		 .fs = EPM_MIC_FS_HZ,
		 .fft_size = EPM_MIC_FFT_SIZE,
		 .bin_count = EPM_MODEL_SPECTRUM_BINS,
		 .bins = mic_bins},
		{.channel_id = TELEM_CHANNEL_ACCEL_X,
		 .fs = EPM_ACCEL_FS_HZ,
		 .fft_size = EPM_ACCEL_FFT_SIZE,
		 .bin_count = EPM_MODEL_SPECTRUM_BINS,
		 .bins = accel_x_bins},
		{.channel_id = TELEM_CHANNEL_ACCEL_Y,
		 .fs = EPM_ACCEL_FS_HZ,
		 .fft_size = EPM_ACCEL_FFT_SIZE,
		 .bin_count = EPM_MODEL_SPECTRUM_BINS,
		 .bins = accel_y_bins},
		{.channel_id = TELEM_CHANNEL_ACCEL_Z,
		 .fs = EPM_ACCEL_FS_HZ,
		 .fft_size = EPM_ACCEL_FFT_SIZE,
		 .bin_count = EPM_MODEL_SPECTRUM_BINS,
		 .bins = accel_z_bins},
	};

	struct scalar_entry scalars[24];

	fill_axis_scalars(&scalars[0], TELEM_SCALAR_RMS_X, phase);
	fill_axis_scalars(&scalars[6], TELEM_SCALAR_RMS_Y, phase);
	fill_axis_scalars(&scalars[12], TELEM_SCALAR_RMS_Z, phase);
	fill_axis_scalars(&scalars[18], TELEM_SCALAR_RMS_MIC, phase);

	return telemetry_build_frame(channels, 4, scalars, 24, out_buf, out_buf_size);
}

static void net_task_fn(void *arg)
{
	net_task_args_t *args = (net_task_args_t *)arg;
	QueueHandle_t mic_q = args->mic_q;
	QueueHandle_t imu_q = args->imu_q;
	(void)mic_q; /* wired into the publish loop in Task 1 */
	(void)imu_q;

	wifi_wait_connected(portMAX_DELAY);

	int rc = link_mqtt_start();

	if (rc != 0) {
		ESP_LOGE(TAG, "link_mqtt_start failed: %d", rc);
	}

	static uint8_t frame_buf[EPM_NET_FRAME_BUF_BYTES];
	uint32_t phase = 0;

	while (1) {
		size_t len = build_synthetic_frame(frame_buf, sizeof(frame_buf), phase++);

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
