# Phase 0.5 build prompt — paste into your AI coding assistant

Read `docs/MASTER_PLAN.md` Part G "Phase 0.5 — Connectivity" and Part C.1 (target firmware layout) and Part I (naming conventions) first — this prompt is those sections made actionable. Work in small diffs, terse output, one-line rationale per change, no restating unchanged code. Stop at the exit test below — do not start Phase 1 opportunistically.

## Why this phase exists, and why it jumps the queue

The reference Uno Q base station is a finished, physical, running device — it will not be modified, ever. The single highest-risk unknown in this whole project is whether our XIAO ESP32-S3 can actually join the reference WiFi network and MQTT broker and be accepted by the reference unmodified pipeline. Everything else (DSP correctness, gateway rebuild) is secondary until that's proven.

**This phase builds real, permanent code, not a throwaway spike.** Every file created here goes straight into the target layout from `docs/MASTER_PLAN.md` Part C.1 (`components/epm_hal/`, `components/epm_drivers/`, `components/epm_codec/`, `main/threads/`) with the reference naming conventions (Part I: `snake_case`, `module_verb_object()`, `#pragma once`, `int` return with `0`/`-errno`, one `<module>_get_stats()` per module) applied from the first line, not bolted on later. There is no "spike" folder, no planned rewrite. What changes later (Phase 6's fuser task, Phase 9's real KX134 driver, Phase 5's generated schema replacing a hand-copied one) extends this code, it doesn't replace it.

Our current firmware (per `README.md`) speaks a totally different protocol: raw TCP to port 5100, a 48-byte custom header, AES-GCM framing in `wifi_task.c`. None of that is compatible with the reference device. This phase adds the new transport and frame format alongside the old one (old code stays working until Phase 7 formally retires it per the master plan's ADR process) — it does not delete `wifi_task.c` today.

## Facts verified live against the reference repository's `satellite/` reference firmware (2026-07-30 — re-verify if it's been a while since this was read)

- The reference Uno Q runs its own WiFi access point: `WIFI_SSID "EPM-BaseStation"` (default, in `satellite/include/app_config.h`) and its own Mosquitto broker at `MQTT_BROKER_HOST "10.42.0.1"`, port 1883. `10.42.0.x` is the standard Linux NetworkManager hotspot range — the Uno Q itself is the access point, not a separate router.
  **Action: confirm the actual SSID / password / broker IP on Abhi's physical Uno Q before flashing anything — the values above are the reference firmware's compiled-in defaults, not a guarantee of what's configured on the real device sitting on Abhi's bench.**
- `node_id`: last 3 octets of the satellite's own WiFi STA MAC address, lowercase hex, no separators (6 chars total). See `derive_node_id()` in `satellite/src/threads/transport_task.cpp` — port this function's logic, keep the name (`derive_node_id`) and behavior identical.
- Publish topic: `epm/<node_id>/data`, QoS 0. Payload is the raw section-list frame bytes — no envelope, no length prefix (MQTT already frames the message).
- Subscribe topic: `epm/<node_id>/cmd`, QoS 1. Payload is `[TYPE:u8][body]`. Today's only defined type is `MQTT_MSG_TYPE_STATUS_LED`, body = `struct display_rgb_payload { rgb; mode; period_ms; }` (see `satellite/include/frame_codec/wire_protocol.h`). **Correction from Phase 0's build session:** on the reference gateway side this direction (base station → satellite command) is published by `base-station/python/ingestion/mqtt_publisher.py`, not `mqtt_subscriber.py` — verify that file if you need the gateway-side counterpart for testing.
- MQTT client buffer must fit the worst-case frame: 1 (num_sections byte) + 4×(spectrum section overhead + `MODEL_SPECTRUM_BINS` × 4 bytes) + one SCALAR_SET section overhead + up to 24 scalar entries. At `MODEL_SPECTRUM_BINS=128` this is under 3 KB.
- Connection behavior: block indefinitely waiting for WiFi (no fallback — a satellite with no network has no job to do), reconnect MQTT on a 2000 ms backoff loop, one mutex-guarded publish path. The reference implementation uses Arduino + PubSubClient; ours uses ESP-IDF native WiFi STA + the `esp-mqtt` component — same behavior and topic/payload contract, different library, an intentional and fine divergence (ESP-IDF is our chosen framework per Part B of the master plan).
- Frame codec to port (read these three headers + their `.cpp` in full before writing anything): `satellite/include/frame_codec/wire_protocol.h`, `satellite/include/frame_codec/spectrum_codec.h`, `satellite/include/frame_codec/telemetry_schema.h`, and the matching `.cpp` files under `satellite/src/frame_codec/`. Match the reference section/field layout exactly; translate C++ idioms to C (no classes, no references) but keep function names and file organization recognizable against the original for easy comparison.
- Verified from `satellite_node_sim.py` (Phase 0 build session): nominal constants `NOMINAL_MIC_FS_HZ=48000`, `NOMINAL_MIC_FFT_SIZE=2048`, `NOMINAL_ACCEL_FS_HZ=6400`, `NOMINAL_ACCEL_FFT_SIZE=1024`, `--accel-bin-count` default 128. Use these as the target frame's field sizes for this phase's minimal test frame; real DSP-derived values come later.

## Tasks

1. **Confirm real device config with Abhi** — actual SSID, WiFi password, and Uno Q IP/broker address. Do not proceed to hardware testing on assumed defaults. (Code/build steps below can proceed in parallel using the documented defaults as placeholders, clearly marked `TODO: confirm against real device`.)

2. Create `components/epm_codec/` (the real, permanent location from Part C.1 — not a temporary name). Pure C, zero ESP-IDF includes, host-testable. Port in from the reference repository's `satellite/include/frame_codec/` + `satellite/src/frame_codec/`:
   - `wire_protocol.h/.c`
   - `spectrum_codec.h/.c` (this phase only needs the encode path; decode can wait for Phase 8's gateway work)
   - `telemetry_schema.h` (hand-copied for now, tagged with a `// TODO(Phase 5): replace with generated version from schema/telemetry_schema.json` comment — Phase 5 swaps this for the generated one without touching anything else in this component)

3. Create `components/epm_hal/include/hal/hal_transport.h` — pure C contract (connect, publish, subscribe-callback, is-connected), zero ESP-IDF includes, matching Part C.1's HAL layering.

4. Create `components/epm_drivers/link_mqtt.c` implementing `hal_transport.h`:
   - ESP-IDF native WiFi STA join (`esp_wifi_*`), block/retry until connected, matching the "no fallback, just wait" philosophy above.
   - `esp-mqtt` (`esp_mqtt_client`) component, connect to the confirmed broker host:port.
   - `derive_node_id()` ported with that exact name — last 3 octets of our own WiFi MAC, lowercase hex.
   - Subscribe to `epm/<node_id>/cmd` QoS 1, publish to `epm/<node_id>/data` QoS 0.
   - Reconnect backoff ~2000 ms, matching the reference pattern.
   - One `link_mqtt_get_stats()` accessor (connects, disconnects, publish failures) per Part I's per-module counters convention.
   - Handle inbound `STATUS_LED` messages by logging them for now (real LED wiring is a later task, once Phase 3's display work lands) — just prove the cmd path decodes correctly.

5. Add `main/threads/net_task.c` (the real target location, not a spike main) that:
   - Owns the connect/reconnect loop via `epm_drivers/link_mqtt.c`.
   - Publishes one valid frame every few seconds via `epm_codec/spectrum_codec.c`: `num_sections=1`, one SCALAR_SET section with synthetic/zero values at this phase (real mic/accel wiring is separate work — the goal here is a byte-valid frame the reference pipeline accepts, not working DSP).
   - Wire `net_task_start()` into `src/main.c` alongside the existing tasks — additive, do not remove or disable the current `wifi_task.c` TCP path yet; that retirement is Phase 7's job with its own ADR.

6. **Host-side verification before touching hardware:** run `base-station/tests/telemetry_frame_test.py` and `base-station/tests/mqtt_subscriber_test.py` unmodified from the cloned reference repo against the bytes our new encoder produces (write them to a file, or replay via `mosquitto_pub` against Phase 0's WSL devrig broker). These tests are the actual pass/fail bar, not eyeballing the bytes.

7. **Real hardware test:** flash to the real XIAO ESP32-S3, power it up in range of the real Uno Q. Confirm:
   - It joins the WiFi network.
   - It connects to the MQTT broker.
   - It appears in the reference **unmodified**, physically running dashboard's fleet view with a valid frame.

8. One ADR (`docs/decisions/ADR-XXX-mqtt-transport-added.md`): records that MQTT transport was added alongside the existing TCP path, why (interop with an unmodifiable base station), and that the TCP path's formal retirement is deferred to Phase 7.

## Exit test

- Real XIAO satellite hardware appears in the reference real, unmodified Uno Q dashboard over actual WiFi/MQTT, publishing a frame that passes the reference implementation's own `telemetry_frame_test.py`.
- New code lives at `components/epm_codec/`, `components/epm_hal/include/hal/hal_transport.h`, `components/epm_drivers/link_mqtt.c`, `main/threads/net_task.c` — the real target paths from Part C.1, no `_spike`/temporary naming anywhere.
- Existing `src/wifi_task.c` and the TCP path are untouched and still build/work — this phase is additive.
- Naming matches Part I: `snake_case`, `#pragma once`, `int`/`-errno` returns, `link_mqtt_get_stats()` present.
- `git log` shows changes scoped to `components/epm_codec/`, `components/epm_hal/`, `components/epm_drivers/`, `main/threads/net_task.c`, `main/main.c` (additive wiring only), and the ADR.
- Zero AI attribution check passes: `git log --format='%B' | grep -iE 'claude|generated with|co-authored'` returns nothing.

Report back: the real SSID/broker IP/port once confirmed (needed for Phase 4's contract doc), whether ESP-IDF's `esp-mqtt` needed any quirky config to match PubSubClient's buffer/QoS behavior, and anything about the reference `frame_codec` that didn't port cleanly to C.
