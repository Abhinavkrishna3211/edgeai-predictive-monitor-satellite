/*
 * led_task.h — Visual LED state machine for the EPM satellite node.
 *
 * ── Single built-in LED (current hardware) ───────────────────────────────────
 *
 * Each state has a RHYTHM, not just a speed — patterns are distinguishable by
 * counting taps and observing pauses, not by estimating frequency.
 *
 *  State            Pattern (each ▌= 100 ms ON, ░ = 100 ms OFF)           Period
 *  ─────────────────────────────────────────────────────────────────────────────
 *  LED_BOOT         ████████████████████████████████  Solid ON             —
 *
 *  LED_WIFI_CONN    ▌░▌░▌░░░░░ ▌░▌░▌░░░░░          3 taps, 0.7 s dark    1.0 s
 *                   "tap-tap-tap … pause"  Scanning for AP
 *
 *  LED_TCP_CONN     █████████░░░░░░░░░░░            Slow 0.5 Hz blink     2.0 s
 *                   "long-on, long-off"   Found AP, connecting to gateway
 *
 *  LED_CALIBRATING  ▌░▌░░░░░░░░░░░░░░░░░░           2 taps, 1.8 s dark   2.0 s
 *                   "tap-tap … long pause"  Learning vibration baseline
 *
 *  LED_OK           ▌░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ Single 100 ms blip   3.0 s
 *                   "blip ……………"  Healthy — mostly dark on purpose
 *
 *  LED_WARN         █████░░░░░ █████░░░░░            Steady 1 Hz blink    1.0 s
 *                   "on-off, on-off"  Elevated vibration
 *
 *  LED_FAULT        ▌░▌░▌░▌░▌░▌░▌░▌░▌░▌░           Continuous 5 Hz      0.2 s
 *                   "strobe — unmistakably urgent"  Bearing fault
 *
 * How to tell them apart at a glance:
 *  • BOOT   → never blinks (solid)
 *  • WIFI   → counts as "1-2-3 … pause … 1-2-3"  (3 quick taps)
 *  • TCP    → slow lazy long blink (0.5 Hz — 1 s on, 1 s off)
 *  • CAL    → counts as "1-2 … long pause" (2 quick taps, then 1.8 s dark)
 *  • OK     → almost always OFF with a rare single blip every 3 s
 *  • WARN   → steady even blink at 1 Hz (like a car turn-signal)
 *  • FAULT  → rapid strobe you cannot count — clearly an alarm
 *
 * ── Upgrade: external RGB LED ────────────────────────────────────────────────
 *
 * When an RGB LED is wired to the XIAO ESP32-S3, set in epm_config.h:
 *   #define EPM_LED_RGB   1
 *   #define LED_PIN_R     3    // GPIO for Red   channel
 *   #define LED_PIN_G     4    // GPIO for Green channel
 *   #define LED_PIN_B     5    // GPIO for Blue  channel
 *
 * Planned RGB colour map (same 7 states, colour replaces pattern counting):
 *   LED_BOOT          White      (255, 255, 255)  — power-on
 *   LED_WIFI_CONN     Blue       (  0,   0, 255)  — scanning for AP
 *   LED_TCP_CONN      Cyan       (  0, 200, 255)  — AP found, seeking gateway
 *   LED_CALIBRATING   Purple     (160,   0, 255)  — learning baseline
 *   LED_OK            Green      (  0, 200,   0)  — healthy, heartbeat pulse
 *   LED_WARN          Yellow     (255, 170,   0)  — attention needed
 *   LED_FAULT         Red        (255,   0,   0)  — bearing fault / alarm
 *
 * led_task.c checks EPM_LED_RGB at compile time; the same led_set_state() API
 * works unchanged regardless of LED type.
 */

#pragma once

#include <stdint.h>

typedef enum {
    LED_BOOT         = 0,  /* solid on — startup / uninitialised            */
    LED_WIFI_CONN    = 1,  /* 3×tap per 1 s — scanning for AP              */
    LED_TCP_CONN     = 2,  /* 0.5 Hz blink — waiting for gateway           */
    LED_CALIBRATING  = 3,  /* 2×tap per 2 s — building vibration baseline  */
    LED_OK           = 4,  /* heartbeat blip every 3 s — all healthy       */
    LED_WARN         = 5,  /* 1 Hz blink — crest / kurtosis elevated       */
    LED_FAULT        = 6,  /* 5 Hz strobe — bearing fault detected         */
} led_state_t;

/* Frames to spend in LED_CALIBRATING after TCP connect — matches Python CAL_FRAMES */
#define LED_CAL_FRAMES  30

/* Init GPIO and start 100 ms periodic timer.  Call once from app_main. */
void led_task_start(void);

/* Thread-safe: may be called from any FreeRTOS task or ISR. */
void led_set_state(led_state_t state);
