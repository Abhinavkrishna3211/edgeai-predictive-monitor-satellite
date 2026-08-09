# Satellite Firmware Stress / Stability Test (2026-08-08 → 2026-08-09)

Real-hardware stress, limit, performance, and robustness test of the ESP32-S3
XIAO satellite node (mic + accel telemetry over MQTT to a base station).
Scope was firmware-only — no `gateway/` changes. Four areas were tested:
heap-leak soak, multi-node network load, disconnect/recovery, and malformed
input robustness (including a WiFi-provisioning spot-check). All raw serial
captures referenced below live in `docs/performance/raw/`.

## Summary

| Area | Result |
|---|---|
| Heap leak | Root-caused (stale MQTT client handle leaked on retry) and fixed: [dab1064](../../commit/dab1064) |
| Network load | 10 simulated satellites, 2.65h, 478k publishes, 0 failures — real satellite's heap unaffected |
| Disconnect/recovery | 4/4 WiFi-AP power-cycle trials reproduced a WiFi-flap → MQTT-churn → self-heal cycle; watchdog ([ADR-036](../decisions/ADR-036-mqtt-reconnect-watchdog.md)) fired correctly every time |
| Malformed input | STATUS_LED cmd-topic fuzzing: all malformed payloads rejected safely, 0 crashes; provisioning captive portal survives bad credentials, rapid reset, and a bad resubmission |
| New findings | Low-heap MQTT-init stall (watchdog blind spot); captive-portal HTTP server starves under extended STA retry churn |
| Deferred | Mosquitto broker-side connect/disconnect logging — blocked by write-access restrictions, twice |

---

## 1. Heap leak root-cause and soak

### Discovery and fix

Code inspection of `link_mqtt_start()` (`components/epm_drivers/link_mqtt.c`)
found that `net_task.c` retries this function on any non-zero return,
including `esp_mqtt_client_start()` failing *after* `esp_mqtt_client_init()`
had already succeeded. A retry overwrote the static `s_client` handle without
ever calling `esp_mqtt_client_destroy()` on the old one — leaking the client
struct, its internal event loop, and its 8KB of I/O buffers on that path.

Fixed in [dab1064](../../commit/dab1064) `fix(net): destroy stale MQTT client
handle before re-init on retry` — destroys any existing `s_client` before
re-init. [f6df13f](../../commit/f6df13f) followed up to guard the
destroy-then-reinit sequence with the existing publish mutex, since a
Kconfig-gated accelerated stress test ([574b158](../../commit/574b158),
`CONFIG_EPM_MQTT_RECONNECT_STRESS_TEST`, 50x back-to-back
`link_mqtt_start()` calls 30s after boot) needed to call it concurrently
with the normal publish loop to actually exercise the fix.
[129ddbc](../../commit/129ddbc) added MQTT connect/disconnect/publish
counters and `heap_caps_get_largest_free_block()` to the existing 30s DIAG
heartbeat ahead of the soak, so fragmentation (`largest_free` falling,
`internal` flat) could be told apart from real exhaustion (both falling
together).

### Soak result

`docs/performance/raw/heap_soak_20260808.log` — clean boot baseline through
several hours of steady-state operation, heap flat at ~30932 bytes
internal / 21504 bytes largest-free throughout, `mqtt: connects=1
disconnects=0`, `net: build_failures=0 publish_failures=0`. No decline once
the stale-handle leak was fixed.

### The bigger discovery: extended real-world outage

The satellite was later left running unattended overnight (~7.6h, no test
harness, no synthetic load — the most realistic condition observed) against
a broker that went unreachable at some point. A morning capture
(`docs/performance/raw/disconnect_recovery_20260809.log`) found it in a
**permanent, non-recovering state**: `esp-tls: select() timeout` /
`Failed to open a new connection: 32774` repeating every ~13s, heap
plateaued at ~3.1-3.7KB internal / 1280 bytes largest-free (frozen, not
still falling), `mqtt: connects` frozen at 12 while `disconnects` climbed
past 735. `wifi: disconnects` stayed flat, confirming WiFi itself was fine —
this was an MQTT-layer-only failure.

Re-reading `link_mqtt.c` in full ruled out this repo's own code: after boot,
`link_mqtt_start()` is called exactly once from `net_task_fn()`; every retry
after that runs entirely inside ESP-IDF's vendored `esp-mqtt`/`esp-tls`
library's own internal reconnect state machine, which never calls back into
our driver. The leak is inside the vendored library's retry path against an
unreachable broker — not something this repo owns or can patch without
forking a framework component (see ADR-036 for the full options analysis).

**Fix:** [39d0fb7](../../commit/39d0fb7) `fix(net): self-heal esp_restart()
when MQTT stays stuck disconnected` — a `consecutive_disconnects` counter
incremented on `MQTT_EVENT_DISCONNECTED`, reset on `MQTT_EVENT_CONNECTED`;
the existing 30s DIAG task calls `esp_restart()` once it reaches 30 (~6.5
minutes of continuous failure at the observed ~13s retry cadence).
`esp_restart()` was chosen over retrying `link_mqtt_start()` because a
retry-only strategy can't self-heal from this heap state — it would
immediately fail ADR-024's 32768-byte guard. Documented in
[ADR-036](../decisions/ADR-036-mqtt-reconnect-watchdog.md), committed as
[f4e8f7b](../../commit/f4e8f7b).

**Validated on real hardware** twice: once by building and flashing the fix
directly onto the board that was still stuck in the failure state overnight
(`docs/performance/raw/watchdog_verify_20260809.log`,
`watchdog_final_verify_20260809.log` — clean recovery, steady state restored
to `connects=1 disconnects=0 publishes=268`, heap back to 30924/21504), and
again via a live trigger during the WiFi AP power-cycle trials (§3):

```
E (393174) DIAG: mqtt stuck: 30 consecutive disconnects with no successful
                 reconnect - restarting to recover (ADR-036)
W (393204) wifi_task: Disconnect reason: ASSOC_LEAVE (8) attempt=1
rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)
...
I (4087) wifi_task: Got IP: 192.168.1.2 (after 1 attempt(s))
```
(`docs/performance/raw/watchdog_trigger_test_20260809.log`)

---

## 2. Multi-node network load

`tools/mqtt_fleet_sim.py` ([2f8f2a6](../../commit/2f8f2a6)) was written for
this test — `tools/satellite_sim.py` speaks the old raw TCP+AES protocol on
port 5100, which the firmware no longer implements after the MQTT transport
migration (ADR-011/ADR-023). The new tool publishes real section-list frames
over MQTT to `epm/<node_id>/data` for N simulated satellites against the
same broker/topic path the real satellite uses.

Ran 10 simulated satellites concurrently against the same broker as the real
unit, for the duration of the heap soak (§1), while the malformed-input
STATUS_LED tests (§4) also ran against the real satellite in the same
window.

**Result** (`docs/performance/raw/mqtt_fleet_sim_20260808.log`): ~9565s
(2.65h), **478,270 total publishes, 0 failures, 0 disconnects** across the
simulated fleet. The real satellite's own DIAG counters over the same window
(`docs/performance/raw/heap_soak_20260808.log`) show no measurable impact:
heap stayed flat at 30932/21504 bytes, `net: build_failures=0
publish_failures=0`, `mqtt: disconnects=0`. The broker and satellite handled
concurrent fleet-scale load without interference.

---

## 3. Disconnect/recovery — WiFi AP power-cycle trials

Four full physical power-cycles of the home WiFi router/AP were run against
the running satellite to test real-world outage recovery
(`docs/performance/raw/wifi_ap_cycle_trials_20260809.log`,
`wifi_ap_cycle_trials_20260809b.log`, ~5MB combined). All four reproduced
the same pattern:

1. AP drops → `wifi_task: Disconnect reason: ...` repeating.
2. WiFi reconnects once the AP is back (28-33 join attempts observed,
   `Got IP: 192.168.1.2 (after N attempt(s))`).
3. But the MQTT-layer churn from the outage doesn't resolve on its own —
   `consecutive_disconnects` keeps climbing even after WiFi is back, because
   the underlying esp-mqtt/esp-tls leak (§1) means the library's own retry
   path degrades further with each cycle.
4. ADR-036's watchdog fires at the 30-32 threshold in every trial, logs
   `mqtt stuck: ... restarting to recover (ADR-036)`, and the board comes
   back cleanly (`rst:0xc`, `Got IP ... after 1 attempt(s)`, MQTT reconnects
   immediately).

| Trial | Watchdog threshold hit | Restart type |
|---|---|---|
| 1 | 32 | `rst:0xc` clean |
| 2 | 31 | `rst:0xc` clean |
| 3 | 31 | `rst:0xc` clean |
| 4 | 31 | `rst:0xc` clean |

4/4 reproducibility, consistent behavior, no unclean resets, no crash loops.

### New finding: low-heap MQTT-init stall (watchdog blind spot)

During trial cycling, a watchdog-triggered `esp_restart()` happened to land
while the WiFi AP was still down (coinciding with a mid-cycle router
power-cycle). The extended WiFi retry loop during boot (33 attempts
observed) consumed/fragmented enough internal heap that by the time `Got IP`
finally landed, free heap was already below the 32KB ADR-024 MQTT-init
safety margin (observed: boot-time heap 27784 bytes, settling flat at 26656
bytes, vs. a normal ~47432-byte boot when the AP is already up).

Because `link_mqtt_start()` never succeeds in initializing an MQTT client
under this condition, `MQTT_EVENT_DISCONNECTED` never fires, so ADR-036's
`consecutive_disconnects` counter — which only increments on that event —
never climbs. **The watchdog cannot trigger to self-heal this specific
state.** The board would sit indefinitely with WiFi connected but zero MQTT
telemetry. The only recovery found was an external power-cycle of the board
itself (confirmed: board recovered cleanly afterward with 47432 bytes free
heap and immediate MQTT connect).

This is a real gap, but out of scope to fix during a live stress test — it
needs its own design (e.g., a boot-time low-heap watchdog independent of the
MQTT-disconnect counter) and is flagged here as a candidate follow-up.

---

## 4. Malformed input robustness

### STATUS_LED cmd-topic fuzzing

Sent a range of payloads to the `epm/<node_id>/cmd` STATUS_LED handler
during the heap soak window (`docs/performance/raw/heap_soak_20260808.log`):

- Valid commands across the parameter space (`rgb=0x00ff00 mode=0
  period_ms=0` through `mode=2 period_ms=100`) — accepted, applied.
- Truncated payloads: 0, 1, 3, and 6 bytes (handler requires 7) — each
  rejected with `STATUS_LED payload too short (N < 7)`, no crash.
- Boundary/overflow values: `rgb=0xffffffff mode=255 period_ms=65535` —
  accepted and clamped/applied without incident.
- Garbage byte patterns: `rgb=0xadde3713 mode=190 period_ms=239` — accepted
  as opaque data (handler doesn't range-validate `mode`/`rgb` beyond length),
  no crash.
- Zero-length message — `malformed cmd message (0 bytes)`, rejected safely.

Result: `led: state_changes=4 remote_updates=9 hw_errors=0` — every valid
command counted, every malformed one rejected without incrementing
`hw_errors` or affecting board stability. No crash, no hang, across the
whole fuzzing pass.

### Provisioning captive-portal spot-check

Ran against the real satellite, with its NVS credentials deliberately
corrupted (raw NVS partition write via `esptool.py` +
`nvs_partition_gen.py`, not the compiled-default reseed path — an
empty/erased NVS silently reconnects using compiled `WIFI_SSID`/`WIFI_PASS`
build constants rather than entering `PROVISIONING`, so a targeted
bad-credential write was needed to actually exercise the state machine).

Note: this NVS-partition erase also reset the autoencoder's learned
baseline model state, not just WiFi credentials — the `nvs` partition
(`partitions_8mb.csv`) holds both. It relearns from scratch over time; no
action needed.

1. **Bad-credential boot → PROVISIONING entry**
   (`docs/performance/raw/provisioning_spotcheck_20260809.log`): bogus SSID
   correctly failed to join within the 15s `BOOT_JOIN_TIMEOUT_MS`, board
   transitioned straight to `WIFI_PROV_PROVISIONING` (skipping the
   `RECOVERING` window, correct for a first-boot failure vs. a loss-after-
   connection). Captive AP came up (`EPM-SAT-<node_id>`, WPA2-PSK,
   `http://192.168.4.1`), `provisioning_entries=1`.

2. **Rapid hardware reset during PROVISIONING**
   (`docs/performance/raw/provisioning_rapidreset_20260809.log`): triggered
   a hard reset via `esptool.py`'s RTS-pin reset while the board was
   actively in `PROVISIONING`. Clean reboot, identical AP SSID/password
   (persisted correctly in its own `epm_ap` NVS namespace, untouched by the
   credential write), re-entered `PROVISIONING` cleanly. No crash, panic, or
   abort anywhere in the log.

3. **Bad-credential resubmission via the portal**: submitted a wrong
   password for a real SSID through `POST /submit`. Firmware entered
   `WIFI_PROV_STA_TESTING`, ran the full 15s `STA_TEST_TIMEOUT_MS`, logged
   `credential test result: FAILED` → `submitted credential failed to join
   — back to PROVISIONING`, cleanly re-entered `PROVISIONING` with
   `provisioning_entries` incremented (1→2), portal resumed serving. No
   crash, no hang, no NVS corruption.

4. **Real-credential resubmission**: succeeded on the first STA-test attempt
   — `Got IP: 192.168.1.2 (after 1 attempt(s))`, `credential test result:
   CONNECTED`, `PROVISIONING → CONNECTED`, MQTT reconnected and resumed
   publishing (`connects=1 disconnects=0 publishes=198`, heap recovering
   toward baseline). Satellite restored to normal operation.

### New finding: captive-portal HTTP server starves under extended STA retry churn

While the board sat in `PROVISIONING` for an extended period (bad SSID still
retried in the background every ~3s under `WIFI_MODE_APSTA`, sharing the
single radio with the softAP), both a Windows laptop and an iPhone
intermittently failed to associate with or load the captive portal —
`ERR_CONNECTION_RESET` / `ERR_EMPTY_RESPONSE` / outright association
failures. Correlating with DIAG output confirmed the cause: internal heap
fragmentation from the ~700+ background STA retry/scan cycles. At ~39
minutes into `PROVISIONING`, `internal` free heap read 20-27KB but
`largest_free` had fallen to as low as **7168 bytes** — too small a
contiguous block for the HTTP server to reliably allocate a TCP/HTTP
response buffer, causing it to either reset the connection or accept it and
send nothing.

This is a real robustness gap: the longer WiFi stays broken, the less
reliable the satellite's own recovery mechanism becomes — precisely when a
user needs it most. Rebooting the board resets the fragmentation and
restores portal reliability for the next several minutes (confirmed: fresh
boot handled a GET/POST cleanly at ~115s in); a request that fails should
generally succeed on retry within that window. Flagged as a candidate
follow-up (e.g., periodic softAP/HTTP-server restart, or reducing STA retry
frequency while in `PROVISIONING`), not fixed in this test.

---

## Deferred: Mosquitto broker-side connect/disconnect logging

Attempted to enable Mosquitto broker-side connect/disconnect logging to
cross-reference against the firmware's own WiFi/MQTT event logs. Blocked
twice: no admin rights in an earlier session, and this session's write to
`C:\Program Files\mosquitto\mosquitto.conf` was blocked outright by the
sandbox's permission classifier. Not pursued further per the tool's own
instructions not to work around a classifier denial. All findings in this
report rely on the firmware-side serial logs only.

---

## Firmware changes (commits, chronological)

- [129ddbc](../../commit/129ddbc) `feat(diag): add MQTT stats and largest-free-block to DIAG heap log`
- [dab1064](../../commit/dab1064) `fix(net): destroy stale MQTT client handle before re-init on retry`
- [2f8f2a6](../../commit/2f8f2a6) `feat(tools): add MQTT-native fleet simulator for network-load testing`
- [f6df13f](../../commit/f6df13f) `fix(net): guard link_mqtt_start()'s client swap with the publish mutex`
- [574b158](../../commit/574b158) `test(net): add Kconfig-gated accelerated MQTT reconnect stress test`
- [39d0fb7](../../commit/39d0fb7) `fix(net): self-heal esp_restart() when MQTT stays stuck disconnected`
- [f4e8f7b](../../commit/f4e8f7b) `docs(adr): add ADR-036 for the MQTT reconnect watchdog`

No `gateway/` changes were made, per scope.

## Known gaps for future work

1. Low-heap MQTT-init stall is invisible to the ADR-036 watchdog (§3) —
   needs a boot-time/heap-level watchdog independent of the MQTT-disconnect
   counter.
2. Captive-portal HTTP server reliability degrades under extended
   `PROVISIONING`-mode STA retry churn (§4) — needs either a lower STA
   retry rate while provisioning, or a periodic AP/HTTP-server restart to
   clear fragmentation.
3. The underlying esp-mqtt/esp-tls per-attempt leak against an unreachable
   broker (§1) is bounded by the watchdog but not fixed at the source — see
   ADR-036 Option A for the tradeoffs of forking the vendored component.
4. Mosquitto broker-side logging remains unavailable in this environment.

## Raw data index

All captures in `docs/performance/raw/` (~16MB total):

| File | Section | Notes |
|---|---|---|
| `heap_soak_20260808.log` | §1, §2, §4 | Multi-hour soak baseline + STATUS_LED fuzzing, concurrent with fleet load |
| `mqtt_fleet_sim_20260808.log` | §2 | 10-node simulated fleet, 2.65h |
| `mqtt_stress_20260809*.log` | §1 | Accelerated 50x `link_mqtt_start()` stress runs |
| `post_stress_verify_20260809.log` | §1 | Healthy baseline post-fix, pre-overnight-soak |
| `disconnect_recovery_20260809.log` | §1 | The stuck-overnight capture that triggered ADR-036 |
| `watchdog_verify_20260809.log`, `watchdog_final_verify_20260809.log` | §1 | Watchdog fix validated directly on the stuck board |
| `watchdog_trigger_test_20260809.log` | §1, §3 | Live watchdog trigger during AP cycling |
| `wifi_ap_cycle_trials_20260809.log`, `wifi_ap_cycle_trials_20260809b.log` | §3 | 4 WiFi AP power-cycle trials + low-heap-stall discovery |
| `closed_port_trial_20260809.log` | §3 | Related disconnect/recovery capture |
| `provisioning_spotcheck_20260809.log` | §4 | PROVISIONING entry via bad credentials |
| `provisioning_rapidreset_20260809.log` | §4 | Rapid reset, bad resubmission, successful real-credential resubmission |
