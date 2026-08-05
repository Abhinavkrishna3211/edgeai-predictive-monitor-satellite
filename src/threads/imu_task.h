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
 * Returns the second QueueHandle for imu_frame_t items (imu_task → net_task),
 * a separate depth-1 queue exclusively for the MQTT publisher — imu_task's
 * primary queue above stays wired to wifi_task unchanged (ADR-021).
 * Queue depth is 1 — net_task reads; imu_task posts via xQueueOverwrite.
 * Call AFTER imu_task_start().
 */
QueueHandle_t imu_task_get_net_queue(void);

/**
 * Initialises the IMU (stub or real KX134 driver) and launches the
 * FreeRTOS task.  Must be called once from app_main.
 */
void imu_task_start(void);

/** Returns the task handle (valid after imu_task_start()). Used by diagnostics_task. */
TaskHandle_t imu_task_get_handle(void);

#ifdef __cplusplus
}
#endif
