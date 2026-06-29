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
 * @param mic_q  Queue returned by dsp_task_get_queue()
 * @param imu_q  Queue returned by imu_task_get_queue()
 */
void wifi_task_start(QueueHandle_t mic_q, QueueHandle_t imu_q);

/*
 * Adaptive-sensing parameters — written by wifi_task when a v2 reply arrives,
 * read by dsp_task at the start of each averaging cycle.  uint8_t writes
 * are atomic on Xtensa so no mutex is needed for these two single-byte values.
 *
 *   g_adapt_overlap_pct : 0, 25, 50, or 75 — % of FFT window to overlap
 *   g_adapt_spec_avg_n  : 1..16 — FFT frames to average per output frame
 *
 * dsp_task latches these at the START of each averaging cycle so a change
 * never corrupts a partially-accumulated power spectrum.
 */
extern volatile uint8_t g_adapt_overlap_pct;
extern volatile uint8_t g_adapt_spec_avg_n;

#ifdef __cplusplus
}
#endif
