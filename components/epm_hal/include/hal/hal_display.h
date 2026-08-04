#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/*
 * Status-LED contract for the RGB indicator (matches the reference repo's
 * satellite/include/hal/hal_display_rgb.h role: one status enum, driven by
 * the DSP/WiFi tasks). components/epm_drivers/display_ledc.c is the only
 * implementation, using the LEDC hardware-fade engine (see its own header
 * comment); a future NeoPixel swap (due before Phase 9 closes) implements
 * this same contract.
 *
 * Function/enum names keep their original rgb_led_* spelling rather than
 * being renamed to hal_display_* — this is a pure move of the existing
 * public API into its HAL home, not a rename, to avoid rippling call-site
 * changes through dsp_task.c/wifi_task.c for no behavioural benefit.
 */

typedef enum {
    RGB_BOOT = 0,
    RGB_WIFI_CONN,
    RGB_TCP_CONN,
    RGB_CALIBRATING,
    RGB_LEARNING,
    RGB_OK,
    RGB_WARN,
    RGB_FAULT,
    RGB_TRIPPED,
    RGB_STATE_MAX,
} rgb_led_state_t;

/* Call once from app_main before task creation. */
void rgb_led_task_init(void);

/** Returns the task handle (set when task starts). Used by diagnostics_task. */
TaskHandle_t rgb_led_task_get_handle(void);

/* Set LED state. Safe from any task context. Non-blocking. */
void rgb_led_set_state(rgb_led_state_t state);

/* Task function — pin to core 1, priority 3, stack 3072. */
void rgb_led_task(void *arg);
