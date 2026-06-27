/*
 * imu_task.h — Public API for the IMU capture + FFT task.
 */

#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Returns the QueueHandle for imu_frame_t items.
 * Queue depth is 1 — the wifi_task reads via xQueueReceive;
 * the imu_task posts via xQueueOverwrite so it never blocks.
 * Call AFTER imu_task_start().
 */
QueueHandle_t imu_task_get_queue(void);

/**
 * Initialises the IMU (stub or real KX134 driver) and launches the
 * FreeRTOS task.  Must be called once from app_main.
 */
void imu_task_start(void);

#ifdef __cplusplus
}
#endif
