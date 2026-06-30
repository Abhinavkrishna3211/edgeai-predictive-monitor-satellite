#pragma once
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include <stdbool.h>

/* GPIO assignments — confirmed free on XIAO ESP32-S3 */
#define RGB_LED_R_GPIO  1
#define RGB_LED_G_GPIO  5
#define RGB_LED_B_GPIO  6

/* LEDC configuration */
#define RGB_LEDC_TIMER      LEDC_TIMER_0
#define RGB_LEDC_MODE       LEDC_LOW_SPEED_MODE
#define RGB_LEDC_RESOLUTION LEDC_TIMER_13_BIT
#define RGB_LEDC_FREQ_HZ    5000
#define RGB_LEDC_CH_R       LEDC_CHANNEL_0
#define RGB_LEDC_CH_G       LEDC_CHANNEL_1
#define RGB_LEDC_CH_B       LEDC_CHANNEL_2

/* Duty levels (13-bit: 0..8191, common-cathode) */
#define RGB_DUTY_FULL    7000
#define RGB_DUTY_OFF     0

/* Colour macros (R, G, B duty) */
#define RGB_WHITE    RGB_DUTY_FULL, RGB_DUTY_FULL, RGB_DUTY_FULL
#define RGB_BLUE     RGB_DUTY_OFF,  RGB_DUTY_OFF,  RGB_DUTY_FULL
#define RGB_CYAN     RGB_DUTY_OFF,  RGB_DUTY_FULL, RGB_DUTY_FULL
#define RGB_YELLOW   RGB_DUTY_FULL, 5950,          RGB_DUTY_OFF
#define RGB_PURPLE   RGB_DUTY_FULL, RGB_DUTY_OFF,  RGB_DUTY_FULL
#define RGB_GREEN    RGB_DUTY_OFF,  RGB_DUTY_FULL, RGB_DUTY_OFF
#define RGB_AMBER    RGB_DUTY_FULL, 2800,          RGB_DUTY_OFF
#define RGB_RED      RGB_DUTY_FULL, RGB_DUTY_OFF,  RGB_DUTY_OFF
#define RGB_MAGENTA  RGB_DUTY_FULL, RGB_DUTY_OFF,  4900
#define RGB_OFF      RGB_DUTY_OFF,  RGB_DUTY_OFF,  RGB_DUTY_OFF

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
