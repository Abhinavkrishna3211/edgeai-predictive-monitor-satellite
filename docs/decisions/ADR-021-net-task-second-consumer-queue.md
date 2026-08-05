---
id: ADR-021
title: net_task gets its own second queue per producer, not a shared reader
status: accepted
date: 2026-08-05
deciders: Abhinav Krishna N
---

## Context

`docs/PHASE_6C_PROMPT.md` set out to replace `net_task.c`'s synthetic MQTT
frame with real mic/IMU data. Live grep of `src/main.c:157` and
`src/threads/tcp_task.c` found a real blocker not in the phase's original
scope: `dsp_task_get_queue()` and `imu_task_get_queue()` already have exactly
one consumer, `tcp_task.c` (the legacy TCP+AES transport, kept until Phase 7
per `docs/decisions/ADR-015-tcp-task-split-deferred.md`). `tcp_task.c`'s
`recv_mic_and_imu()` (`tcp_task.c:467`) does blocking
`xQueueReceive(mic_q, &s_mic, pdMS_TO_TICKS(2000))` /
`xQueueReceive(imu_q, &s_imu, pdMS_TO_TICKS(4000))` on those handles every
epoch. Both queues are depth-1 (`xQueueCreate(1, ...)` + `xQueueOverwrite` in
`dsp_task.c`/`imu_task.c`) — built for exactly one reader. If `net_task.c`
also called `xQueueReceive` on the same handles, it would race `tcp_task.c`
for the same single buffered item every cycle: whichever task happens to
call first "wins" that epoch's item and the other silently starves. No
fan-out, mutex, event-group, or peek mechanism exists anywhere in this tree
to coordinate two readers of one `xQueueOverwrite` queue.

## Options considered

### Option A: defer real-data wiring to Phase 7
`tcp_task.c` is already slated for full deletion in Phase 7
(ADR-015), which would leave `net_task.c` as the sole consumer and remove
the conflict without any queue changes. Rejected: this project's stated
priority (`docs/MASTER_PLAN.md` Part G) has consistently been "get real data
flowing" over "minimize phase count," and delaying real data on the wire by
a whole phase for a problem that has a small, contained fix now isn't worth
the wait.

### Option B: second parallel queue per producer
`dsp_task.c` and `imu_task.c` each post to a second depth-1 queue
(`dsp_task_get_net_queue()` / `imu_task_get_net_queue()`) purely for
`net_task.c`, in addition to their existing queue which stays wired to
`tcp_task.c` unchanged. Both writes are still `xQueueOverwrite` — same cost
as today, zero coupling between the two consumers, and `tcp_task.c`'s own
queue handle, read pattern, and timeouts are untouched (verified:
`git diff --stat src/threads/tcp_task.c` is empty after this change).

## Decision

**Option B.** `dsp_task_start()`/`imu_task_start()` now create a second
`xQueueCreate(1, sizeof(...))` queue alongside the existing one, and the
producer's post site (`dsp_task.c` after averaging, `imu_task.c` after
`SPEC_AVG_N` blocks) calls `xQueueOverwrite()` on both queues back to back.
`net_task.h`'s `net_task_start()` signature grows two `QueueHandle_t`
parameters (`mic_q`, `imu_q`), populated from `main.c` via
`dsp_task_get_net_queue()`/`imu_task_get_net_queue()`, and passed into the
task via a small `net_task_args_t` struct — the same pattern
`tcp_task.c`'s own `wifi_task_args_t`/`s_task_args` already uses
(`tcp_task.c:695-697, 908-914`) to hand `xTaskCreatePinnedToCore()`'s single
`void *arg` more than one value.

The file stays named `net_task.c` (not renamed to `fuser_task.c`, despite
`docs/MASTER_PLAN.md`'s earlier directory sketch anticipating that name) —
recorded here explicitly so a future phase doesn't rename it out of habit
without a reason.

`net_task.c` reads both net-side queues non-blocking
(`xQueueReceive(q, &dst, 0)`) once per 200 ms publish tick, since it runs on
its own fixed cadence and must not block waiting on either producer.
Per the zero-fill rule (`docs/BASE_STATION_CONTRACT.md` line 24 — present +
real `bin_count` + all-zero values reads as genuine silence to the model,
not "no data yet"), `net_task.c` does not publish until both queues have
delivered at least one real frame; once both have, each tick publishes
whatever is currently cached (freshly received this tick, or carried over
from the last tick if this tick's non-blocking receive found nothing new).
This mirrors `dsp_task.c`'s and `tcp_task.c`'s own existing "cache last
value" pattern rather than inventing a new one. The phase prompt's original
citation, `docs/SENSOR_TELEMETRY_FRAME_PLAN.md`, does not exist in this
repo — it is a reference-repo-only document; `BASE_STATION_CONTRACT.md` is
the correct local source for this rule.

The receive destinations (`s_last_mic`, a `mic_frame_t`, and `s_last_imu`, a
~12.4 KB `imu_frame_t`) are file-scope `static`, never a stack temporary —
`net_task`'s stack (`TASK_STACK_NET` = 4096 bytes) cannot hold either safely,
same reasoning `tcp_task.c` already applies to its own `s_mic`/`s_imu`
receive buffers. They additionally use `EXT_RAM_BSS_ATTR` (PSRAM placement),
matching the precedent already established for this exact size class —
`dsp_task.c:112`'s `s_mag_db` and `imu_task.c:91`'s `s_frame` — because a
plain internal-DRAM static here would regress the tight heap margin
`docs/decisions/ADR-020-bin-count-downsampled-not-buffer-enlarged.md`
documents (~7-10 KB free under load). `xQueueReceive` writing directly into
an `EXT_RAM_BSS_ATTR` destination is safe: there is no DMA constraint on
this path, unlike the KX134 FIFO buffer (`imu_task.c`'s HW-OPT comment,
3b), which must stay in internal RAM specifically because ESP32-S3 DMA
cannot address PSRAM.

## Consequences

- `dsp_task.c`/`imu_task.c` each gain one more `xQueueCreate` + one more
  `xQueueOverwrite` per post — negligible cost, no change to either task's
  timing-critical path.
- `tcp_task.c` is provably unaffected: its queue handles, read pattern, and
  timeouts are byte-identical before and after this change.
- `net_task.c` gains a startup window (until both producers have posted at
  least once) where it publishes nothing — expected, and preferable to
  publishing a zero-filled frame that would be indistinguishable from real
  silence.
- Two depth-1 queues per producer means the net-side and tcp-side consumers
  can legitimately be looking at different frames a few tens of
  milliseconds apart (each producer's `xQueueOverwrite` pair isn't atomic
  across both queues). Acceptable: neither consumer's correctness depends
  on being frame-synchronized with the other.

## Validation

`git diff --stat src/threads/tcp_task.c` empty. `tests/host/` full suite
passes. `pio run -e xiao_esp32s3` clean.
