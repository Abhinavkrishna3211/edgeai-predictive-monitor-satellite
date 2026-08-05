/*
 * net_task.h — Public API for the MQTT telemetry task.
 *
 * The satellite's sole transport (see
 * docs/decisions/ADR-023-transport-adrs-superseded.md). This task owns
 * nothing about WiFi itself — it blocks on wifi_task.h's
 * wifi_wait_connected(), then drives components/epm_drivers/
 * link_mqtt.c (behind components/epm_hal/include/hal/hal_transport.h) to
 * publish a section-list telemetry frame
 * (components/epm_codec/include/frame_codec/spectrum_codec.h) every
 * EPM_NET_PUBLISH_INTERVAL_MS, built from real mic/accel data delivered via
 * dsp_task_get_queue()/imu_task_get_queue() (ADR-021).
 */

#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Call after wifi_rf_init(), dsp_task_start(), and imu_task_start().
 * Creates the net task (core 0, priority TASK_PRIO_NET) which blocks on
 * wifi_wait_connected(portMAX_DELAY), starts the MQTT link, and begins the
 * publish loop, reading mic_q/imu_q each tick. Returns 0, or -ENOMEM if
 * task creation fails.
 */
int net_task_start(QueueHandle_t mic_q, QueueHandle_t imu_q);

#ifdef __cplusplus
}
#endif
