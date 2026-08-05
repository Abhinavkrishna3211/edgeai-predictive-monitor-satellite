---
id: ADR-015
title: wifi_task.c moved whole to src/threads/tcp_task.c; AES-GCM split deferred to Phase 7
status: accepted
date: 2026-08-04
deciders: Abhinav Krishna N
---

## Context

Phase 3 (`docs/MASTER_PLAN.md` Part C.1) moves the firmware source tree into
the target `src/threads/` + `components/epm_*` layout. Every other task file
(`mic_task.c`, `dsp_task.c`, `imu_task.c`, `rgb_led_task.c`) followed the same
shape: task loop stays a thin driver of a HAL contract, with any pure logic
extracted into `components/epm_drivers/` or `components/epm_dsp/` first.

`wifi_task.c` does not fit that shape cleanly. It is one 921-line task
function that owns three things the other files kept separate: the WiFi STA
connection state machine, the raw-TCP send loop (`ADR-010`), and inline
AES-GCM framing (`mbedtls/gcm.h`, `ADR-007`) — all sharing local state
(socket fd, retry counters, the AES context) with no existing seams a HAL
contract could sit behind.

## Decision

Move `wifi_task.c`/`.h` whole to `src/threads/tcp_task.c`/`.h` — a pure
file relocation, no internal split. All public symbols
(`wifi_rf_init`, `wifi_wait_connected`, `wifi_task_start`,
`wifi_task_get_handle`, `g_adapt_overlap_pct`, `g_adapt_spec_avg_n`,
`g_wifi_debug_state`) and the `"wifi_task"` log tag are unchanged; only the
file path, its own-header include, and dependents' include paths
(`src/main.c`, `src/threads/dsp_task.c`, `src/threads/net_task.c`) move to
`threads/tcp_task.h`.

The AES-GCM/TCP internals are not extracted behind a HAL contract in this
phase. That work is deferred to Phase 7, when the raw-TCP transport is
retired in favor of the MQTT path (`ADR-011`) and this code is deleted
outright — inventing a new `epm_drivers`/`epm_hal` home now for logic with a
~4-phase remaining lifespan would add churn and review risk for no lasting
structural benefit; the split is only worth doing for code that survives.

## Consequences

**Positive:**
- Matches the target directory layout with the lowest-risk change: a rename,
  not a refactor — no new seams to get wrong in code that is about to be
  deleted anyway.
- Keeps the connection state machine and its framing logic together, which
  matches how they actually interact (retry-on-send-failure closes the
  socket and re-enters the same function, not a cleanly separable driver
  call).

**Negative / trade-offs:**
- `src/threads/tcp_task.c` is the one file in `src/threads/` that isn't a
  thin wrapper over a HAL contract — an intentional, temporary inconsistency
  with the rest of the tree, resolved when Phase 7 deletes the file.

## Validation

- `pio run -e xiao_esp32s3` — clean build succeeds after the move.
- `ctest --test-dir tests/host/build` — 3/3 still pass (no host test
  references this file).

## Addendum (2026-08-05, Phase 7a)

The inconsistency this ADR called "resolved when Phase 7 deletes the file"
is now resolved: `src/threads/tcp_task.c`/`.h` are deleted outright. WiFi
STA lifecycle and power management — the two of the file's three original
responsibilities with a future — move to a revived
`src/threads/wifi_task.c`/`.h` (`docs/decisions/ADR-022-wifi-task-revived.md`).
The third, the raw-TCP+AES-GCM transport, has no successor; it is retired,
not moved (`docs/decisions/ADR-023-transport-adrs-superseded.md`).
