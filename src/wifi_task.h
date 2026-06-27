/*
 * wifi_task.h — Public API for the WiFi connection + TCP send task.
 */

#pragma once

#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Phase 1 — call BEFORE any I2S/DMA tasks are started.
 * Initialises WiFi hardware, registers event handlers, and calls
 * esp_wifi_start() so the RF scan begins with no I2S interrupt load.
 */
void wifi_rf_init(void);

/**
 * Phase 2 — call after wifi_rf_init(), still before I2S tasks.
 * Blocks until WIFI_CONNECTED_BIT is set or ticks_to_wait expires.
 * Returns true if connected, false on timeout.
 */
bool wifi_wait_connected(TickType_t ticks_to_wait);

/**
 * Phase 3 — call after mic_task_start() / imu_task_start().
 * Creates the TCP sender task that drains the DSP queues.
 *
 * @param mic_q  Queue returned by mic_task_get_queue()
 * @param imu_q  Queue returned by imu_task_get_queue()
 */
void wifi_task_start(QueueHandle_t mic_q, QueueHandle_t imu_q);

#ifdef __cplusplus
}
#endif
