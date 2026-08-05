/*
 * net_task.c — MQTT telemetry publish loop (Phase 0.5, real data since 6c).
 *
 * Blocks on wifi_task.h's wifi_wait_connected() (WiFi STA bring-up lives in
 * threads/wifi_task.c — see docs/decisions/ADR-022-wifi-task-revived.md),
 * then starts the MQTT link (components/epm_drivers/link_mqtt.c) and
 * publishes one section-list telemetry frame
 * (components/epm_codec/include/frame_codec/spectrum_codec.h) every
 * EPM_NET_PUBLISH_INTERVAL_MS, built from real mic/IMU FFT output delivered
 * via the net-side queues (dsp_task_get_net_queue()/imu_task_get_net_queue(),
 * ADR-021) — a second consumer of each producer's output, parallel to
 * tcp_task.c's own queues, added specifically so this task never races
 * tcp_task.c for the same buffered item.
 *
 * No frame is published until both queues have delivered at least one real
 * frame (docs/BASE_STATION_CONTRACT.md line 24: a present, real-bin_count,
 * all-zero channel reads as genuine silence to the model, not "no data
 * yet" — zero-filling before real data exists would be indistinguishable
 * from that). Once both sides have delivered once, every tick publishes
 * whatever is currently cached, refreshed opportunistically each tick.
 */

#include "threads/net_task.h"

#include <errno.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "dsp/spectrum.h"
#include "frame_codec/scalar_map.h"
#include "frame_codec/spectrum_codec.h"
#include "frame_codec/telemetry_schema.h"
#include "hal/hal_transport.h"
#include "drivers/link_mqtt.h"

#include "epm_config.h"
#include "threads/wifi_task.h"

static const char *TAG = "net_task";

/* net_task_start()'s two queue args, handed to net_task_fn via
 * xTaskCreatePinnedToCore's arg pointer (mirrors tcp_task.c's own
 * s_task_args pattern) — ADR-021. */
typedef struct {
	QueueHandle_t mic_q;
	QueueHandle_t imu_q;
} net_task_args_t;

static net_task_args_t s_task_args;

/* Cached last-received frames. mic_frame_t (~2.1 KB) and imu_frame_t
 * (~12.4 KB) are both too large for TASK_STACK_NET (4096 B), so
 * xQueueReceive() always writes directly into these file-scope statics,
 * never a stack temporary (mirrors tcp_task.c's own s_mic/s_imu receive
 * buffers). EXT_RAM_BSS_ATTR places them in PSRAM — the same placement
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

static void net_task_fn(void *arg)
{
	net_task_args_t *args = (net_task_args_t *)arg;
	QueueHandle_t mic_q = args->mic_q;
	QueueHandle_t imu_q = args->imu_q;

	wifi_wait_connected(portMAX_DELAY);

	int rc = link_mqtt_start();

	if (rc != 0) {
		ESP_LOGE(TAG, "link_mqtt_start failed: %d", rc);
	}

	while (1) {
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
