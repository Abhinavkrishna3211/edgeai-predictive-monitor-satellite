/*
 * led_task.h — Public API for the RGB status-LED task wrapper.
 */

#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Initialises the LEDC hardware (hal_display.h) and launches the RGB
 * animation task on core 1, priority 3. Must be called once from app_main.
 */
void led_task_start(void);

/** Returns the task handle (valid after led_task_start()). Used by diagnostics_task. */
TaskHandle_t led_task_get_handle(void);

#ifdef __cplusplus
}
#endif
