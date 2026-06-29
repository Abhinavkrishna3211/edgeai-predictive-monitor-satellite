/*
 * mic_task.h — Public API for the microphone capture task.
 */

#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Returns the QueueHandle for raw_mic_block_t items (mic_task → dsp_task).
 * Queue depth is 1 — dsp_task reads via xQueueReceive;
 * mic_task posts via xQueueOverwrite so it never blocks.
 * Call AFTER mic_task_start().
 */
QueueHandle_t mic_task_get_raw_queue(void);

/**
 * Initialises I2S and launches the capture FreeRTOS task on core 0.
 * Must be called once from app_main before dsp_task_start().
 */
void mic_task_start(void);

#ifdef __cplusplus
}
#endif
