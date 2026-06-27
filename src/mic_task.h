/*
 * mic_task.h — Public API for the microphone capture + FFT task.
 */

#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Returns the QueueHandle for mic_frame_t items.
 * Queue depth is 1 — the wifi_task reads via xQueueReceive;
 * the mic_task posts via xQueueOverwrite so it never blocks.
 * Call AFTER mic_task_start().
 */
QueueHandle_t mic_task_get_queue(void);

/**
 * Initialises I2S, DSP library, and launches the mic FreeRTOS task.
 * Must be called once from app_main before wifi_task_start().
 */
void mic_task_start(void);

#ifdef __cplusplus
}
#endif
