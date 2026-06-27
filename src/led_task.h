/*
 * led_task.h — Visual LED indicator for the EPM satellite node.
 *
 * ── Single built-in LED (current hardware) ────────────────────────────────────
 *
 * Five states with distinct, easy-to-read patterns.  No counting required —
 * each state reads differently at a glance:
 *
 *  State            LED behaviour                  Meaning
 *  ──────────────────────────────────────────────────────────────────────────────
 *  LED_BOOT         Solid ON                       Startup  (<2 s, then blinks)
 *  LED_CONNECTING   0.5 Hz blink (1 s ON / 1 s OFF) WiFi / TCP / calibrating
 *  LED_OK           Single blip every 3 s          Machine healthy
 *  LED_WARN         1 Hz blink (0.5 s / 0.5 s)    Elevated vibration — check soon
 *  LED_FAULT        Solid ON                       Bearing fault — inspect NOW
 *
 * At a glance:
 *  • Nearly dark (rare blip) → healthy, data flowing normally
 *  • Slow blink              → connecting or calibrating, wait
 *  • Medium blink            → vibration elevated, attention recommended
 *  • Solid ON after startup  → FAULT — inspect bearing immediately
 *
 * BOOT vs FAULT: both solid, but BOOT lasts < 2 s before transitioning to blink.
 * If the LED turns solid again after being in a blinking or heartbeat state: FAULT.
 *
 * ── RGB LED upgrade ───────────────────────────────────────────────────────────
 *
 * Connect a common-cathode RGB LED and set in epm_config.h:
 *   #define EPM_LED_RGB   1
 *   #define LED_PIN_R     3    // GPIO for Red   channel
 *   #define LED_PIN_G     4    // GPIO for Green channel
 *   #define LED_PIN_B     5    // GPIO for Blue  channel
 *
 * Colour map (5 states):
 *   LED_BOOT          White   (255, 255, 255)
 *   LED_CONNECTING    Blue    (  0,   0, 255)
 *   LED_OK            Green   (  0, 200,   0)
 *   LED_WARN          Yellow  (255, 170,   0)
 *   LED_FAULT         Red     (255,   0,   0)
 */

#pragma once

#include <stdint.h>

typedef enum {
    LED_BOOT       = 0,  /* solid ON — startup, brief                       */
    LED_CONNECTING = 1,  /* 0.5 Hz blink — WiFi / TCP / calibrating        */
    LED_OK         = 2,  /* heartbeat blip every 3 s — machine healthy     */
    LED_WARN       = 3,  /* 1 Hz blink — elevated vibration, check soon    */
    LED_FAULT      = 4,  /* solid ON — bearing fault, inspect now          */
} led_state_t;

/* Frames after TCP connect before alert-driven LED kicks in.
 * Matches Python CAL_FRAMES — first 30 frames build the vibration baseline. */
#define LED_CAL_FRAMES  30

/* Init GPIO and start 100 ms periodic timer. Call once from app_main. */
void led_task_start(void);

/* Thread-safe: may be called from any FreeRTOS task or ISR. */
void led_set_state(led_state_t state);
