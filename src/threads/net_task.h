/*
 * net_task.h — Public API for the MQTT telemetry task (Phase 0.5).
 *
 * Additive alongside tcp_task.c's raw-TCP path (see
 * docs/decisions/ADR-011-mqtt-transport-added.md); TCP retirement is
 * Phase 7's job. This task owns nothing about WiFi itself — it blocks on
 * tcp_task.h's wifi_wait_connected(), then drives components/epm_drivers/
 * link_mqtt.c (behind components/epm_hal/include/hal/hal_transport.h) to
 * publish a synthetic section-list telemetry frame
 * (components/epm_codec/include/frame_codec/spectrum_codec.h) every
 * EPM_NET_PUBLISH_INTERVAL_MS. Real mic/accel data replaces the synthetic
 * bins in Phase 6.
 */

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Call after wifi_task_start(). Creates the net task (core 0, priority
 * TASK_PRIO_NET) which blocks on wifi_wait_connected(portMAX_DELAY),
 * starts the MQTT link, and begins the publish loop. Returns 0, or
 * -ENOMEM if task creation fails.
 */
int net_task_start(void);

#ifdef __cplusplus
}
#endif
