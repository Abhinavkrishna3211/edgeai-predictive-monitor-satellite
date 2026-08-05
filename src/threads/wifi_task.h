/*
 * wifi_task.h — Public API for WiFi STA lifecycle + power management.
 *
 * Despite the "task" name (kept for continuity with the log tag and the
 * priority/stack naming convention elsewhere in this tree) there is no
 * FreeRTOS task here: WiFi STA bring-up is entirely event-driven
 * (ESP-IDF's own event loop task drives it), and the two calls below are
 * meant to be made once, in sequence, from app_main() before any other
 * task starts. See docs/decisions/ADR-022-wifi-task-revived.md.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Phase 1 — call BEFORE any I2S/DMA tasks are started.
 * Initialises WiFi hardware, registers event handlers, calls
 * esp_wifi_start() so the RF scan begins with no I2S interrupt load, caps
 * TX power, and configures dynamic CPU frequency scaling.
 */
void wifi_rf_init(void);

/**
 * Phase 2 — call after wifi_rf_init(), still before I2S tasks.
 * Blocks until WIFI_CONNECTED_BIT is set or ticks_to_wait expires.
 * Returns true if connected, false on timeout.
 */
bool wifi_wait_connected(TickType_t ticks_to_wait);

/* JTAG-readable WiFi state machine step: 0=init 1=rf_init_done 2=sta_start
 * 3=connecting 4=got_ip. */
extern volatile uint32_t g_wifi_debug_state;

#ifdef __cplusplus
}
#endif
