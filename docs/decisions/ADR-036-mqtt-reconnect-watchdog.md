---
id: ADR-036
title: Self-heal esp_restart() watchdog for a stuck MQTT reconnect loop
status: accepted
date: 2026-08-09
deciders: Abhinav Krishna N
---

## Context

ADR-024 added a heap-margin guard (`EPM_MQTT_MIN_FREE_HEAP_BYTES`, 32768
bytes) in front of `esp_mqtt_client_init()` and named its own revisit
trigger explicitly: *"the guard's `ESP_LOGE` ever actually fires on real
hardware outside of deliberate low-heap testing."* That trigger has now
fired.

The real satellite was left running unattended overnight (no test harness,
no synthetic load, no deliberate interference — the most realistic
condition this project has observed it under) for approximately 7.6 hours.
A serial capture attached the next morning
(`docs/performance/raw/disconnect_recovery_20260809.log`) found it in a
permanent, non-recovering state:

```
E (27325194) esp-tls: [sock=58] select() timeout
E (27325194) transport_base: Failed to open a new connection: 32774
E (27325194) mqtt_client: Error transport connect
W (27325194) link_mqtt: disconnected
```

repeating roughly every 13 seconds, with DIAG snapshots showing:

```
I (27347724) DIAG: Heap free: internal=3144 largest_free=1280 PSRAM=8209812 IRAM=0
I (27347734) DIAG: mqtt: connects=12 disconnects=735 publishes=86767 publish_failures=12 cmds_received=0
```

`largest_free` stayed frozen at exactly 1280 bytes across the whole capture
window while `internal` free heap plateaued in a ~500-600 byte band around
3.1-3.7 KB — both falling together, not just fragmenting, per the existing
DIAG comment's own distinction. `mqtt: connects` never advanced from 12
while `disconnects` climbed steadily; `wifi: disconnects` stayed frozen at
447, confirming WiFi itself was fine and the failure was MQTT-layer only.

`link_mqtt.c` was re-read in full to rule out our own code: after boot,
`link_mqtt_start()` is called exactly once from `net_task_fn()`; every
reconnect attempt after that runs entirely inside esp-mqtt's own internal
state machine (`network.reconnect_timeout_ms = 2000` in the client config),
which never calls back into our driver. The leak is therefore happening
inside ESP-IDF's vendored `esp-mqtt`/`esp-tls` library during its own
internal retry path against a broker that (for whatever reason — the
laptop-hosted dev broker was not confirmed continuously up overnight) the
board could not reach, not in any driver code this repo owns.

## Measurement

Comparing the stuck capture against
`docs/performance/raw/post_stress_verify_20260809.log` — the same boot
session's healthy baseline, captured ~61 seconds after the same reflash:

| | t=61s (baseline) | t=27347s+ (stuck, ~7.6h later) |
|---|---|---|
| Heap free (internal) | 30928 bytes | ~3100-3700 bytes (plateaued) |
| largest_free | 21504 bytes | 1280 bytes (frozen) |
| mqtt connects | 1 | 12 (frozen, no further growth) |
| mqtt disconnects | 0 | 735 → 749+ (climbing ~2-3/30s) |

This is a single continuous boot session's organic trajectory, not a
synthetic accelerated test — stronger evidence for the leak's real-world
end state than the earlier 50-call accelerated stress test could provide,
because it shows what actually happens when the retry loop is allowed to
run for hours against a genuinely unreachable broker.

## Options considered

### Option A: Fork or patch `esp-mqtt`/`esp-tls` to fix the per-attempt leak
**Design:** Locate and fix the leak at its source inside the vendored
component, following the same shadow-component mechanism ADR-024's Option
A already described for a different esp-mqtt bug.
**Cons:** ADR-024 already rejected taking on permanent maintenance of a
forked framework component for a narrower, better-understood bug in the
same library; the leak observed here is not yet root-caused to a specific
line (no `addr2line`-level diagnosis was performed — the board is a single
physical unit currently stuck in the failure state, not gdb-attached), so
a fork would be a much larger, speculative undertaking. Disproportionate
given Option B fully resolves the *observed* failure mode (permanent
lockout) even though it doesn't stop the underlying per-attempt leak.

### Option B: `esp_restart()` watchdog on stuck consecutive disconnects (chosen)
**Design:** Add a `consecutive_disconnects` counter to
`struct link_mqtt_stats` (`components/epm_drivers/include/drivers/link_mqtt.h`),
incremented on every `MQTT_EVENT_DISCONNECTED` and reset to 0 on every
`MQTT_EVENT_CONNECTED` (`components/epm_drivers/link_mqtt.c`). The existing
30-second-cadence `diagnostics_task_fn()` (`src/main.c`) checks this counter
after logging the mqtt DIAG line and calls `esp_restart()` once it reaches
30 (~6.5 minutes of continuous failure at the observed ~13s retry cadence).
**Pros:** `esp_restart()` does not depend on heap availability, unlike
re-invoking `link_mqtt_start()` — which would immediately fail ADR-024's
32768-byte guard once heap is this exhausted, so a reconnect-only retry
strategy cannot self-heal from this state. A full restart clears the leaked
heap unconditionally. Minimal, local change — no forked framework
component, reuses the existing DIAG task's cadence and the existing
Part I `_get_stats()` convention.
**Cons:** Doesn't fix the underlying per-attempt leak, only bounds its
consequence — the board will still burn through its heap margin on every
extended outage and pay the cost of a full reboot (losing in-flight FFT
state, provisioning task state, etc.) rather than a clean reconnect. A
two-stage design (retry `link_mqtt_start()` first, escalate to
`esp_restart()` only if that also fails) was considered and rejected as
unsupported complexity: `link_mqtt_start()` would just hit the ADR-024
guard immediately in this state, so it would never do anything a direct
restart doesn't already accomplish, only defer it.

## Decision

**Chosen: Option B — `esp_restart()` watchdog, threshold 30.**

The threshold is calibrated directly from the observed real retry cadence
(~13s per failed attempt, ~2-3 per 30s DIAG cycle): 30 consecutive
disconnects is ~6.5 minutes of continuous, unbroken failure. That's long
enough that an ordinary transient blip — which resets this counter to 0 on
its very next successful reconnect — cannot false-trigger a restart, and
short enough that a field-deployed unit self-heals within minutes instead
of being stuck for hours (as directly observed) until someone notices and
power-cycles it by hand.

Option A remains the correct move if this watchdog is later found to fire
routinely (i.e., broker outages of 6.5+ minutes are common in the target
deployment environment) — at that point the cost of frequent reboots would
justify root-causing and fixing the underlying per-attempt leak instead of
just bounding it.

## Consequences

**Positive:**
- Closes the permanent-lockout failure mode directly observed on real
  hardware — a field unit can no longer get stuck requiring manual
  power-cycle after an extended broker outage.
- No new maintenance surface: one counter field, two one-line updates in
  the existing event handler, one threshold check in the existing DIAG
  task. No forked framework component.
- `consecutive_disconnects` is independent, permanent telemetry (visible
  in every DIAG cycle via a future log line if added) even below the
  restart threshold — useful for spotting a broker flakiness trend before
  it ever reaches 30.

**Negative / trade-offs:**
- The underlying esp-mqtt/esp-tls per-attempt leak is still present and
  unfixed — this bounds the blast radius, it doesn't close the root cause.
- A restart during a genuine extended outage means the board briefly stops
  capturing/publishing entirely (reboot + reconnect time) rather than
  quietly waiting; judged an acceptable trade given the alternative is an
  indefinite, unrecoverable outage.
- Threshold of 30 is calibrated from one real observed retry cadence
  (~13s/attempt); if `network.reconnect_timeout_ms` or the broker's own
  behavior changes that cadence meaningfully, the ~6.5-minute window this
  threshold implies would shift and may need re-tuning.

**Metrics to watch:**
- How often the `"mqtt stuck: ... restarting to recover (ADR-036)"`
  `ESP_LOGE` line fires in the field — frequent firing is the signal to
  revisit Option A.

## Validation

Hardware was available and in exactly the failure state this ADR fixes at
the time of the change (the real board, stuck since the prior night, still
retrying every ~13s with heap frozen at ~3.1-3.7 KB). The fix was built and
flashed via `.\pio.ps1 run --environment xiao_esp32s3 --target upload`
(never plain `pio run`, which silently falls back to the wrong compiled-in
broker default) directly onto that stuck board, and a fresh serial capture
confirmed recovery — see
`docs/performance/SATELLITE_STRESS_STABILITY_TEST.md` for the exact
before/after log excerpt and the post-restart reconnect timeline.
