/*
 * dsp_task.h — Public API for the DSP compute task (core 1).
 */

#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/ringbuf.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Returns the QueueHandle for mic_frame_t items (dsp_task → wifi_task).
 * Queue depth is 1 — wifi_task reads; dsp_task posts via xQueueOverwrite.
 * Call AFTER dsp_task_start().
 */
QueueHandle_t dsp_task_get_queue(void);

/**
 * Initialises the Hann window table and launches the DSP FreeRTOS task on
 * core 1.  raw_rb must be the ring buffer returned by mic_task_get_raw_ringbuf().
 * Call AFTER mic_task_start().
 */
void dsp_task_start(RingbufHandle_t raw_rb);

/** Returns the task handle (valid after dsp_task_start()). Used by diagnostics_task. */
TaskHandle_t dsp_task_get_handle(void);

#ifdef __cplusplus
}
#endif
