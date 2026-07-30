# MASTER PLAN — EPM Satellite + Gateway: Full Interop & Architecture Rebuild

(Full text as provided by Abhi, 2026-07-30 — held here as the cross-phase source of truth. the planning tool tracks phase completion against this document; VS Code the AI coding assistant executes one phase at a time from a scoped prompt derived from it. See docs/PHASE_0_PROMPT.md for the current phase.)

## Mission
Two repositories exist for the same physical system (bearing-fault monitoring via a mic + accelerometer sensor node reporting to a central gateway). They were built independently and now need to converge:
- **This repo** (`edgeai-predictive-monitor-satellite`) — ESP32-S3/XIAO firmware **and** a self-built Python gateway (`mic_tools/`) written because the physical base-station hardware wasn't available yet.
- **Reference repo** (`edgeai-predictive-monitor`, the reference-repo maintainer's) — a working Arduino Uno Q base station (STM32U585 MCU + Qualcomm QRB2210 MPU/Linux) with its own satellite firmware and Python gateway, built with professional embedded discipline (the author has production firmware experience, ex-Qualcomm).

Repo links:
- This repo (to be changed): `https://github.com/Abhinavkrishna3211/edgeai-predictive-monitor-satellite`
- Reference repo (ground truth for wire format and module conventions): `https://github.com/rahuljeyaraj/edgeai-predictive-monitor`

The goal is not "pick a winner." It is: our satellite firmware must speak the reference-repo maintainer's wire protocol so it can plug into his (unmodified) base station as a node, AND our own gateway (`mic_tools/`) gets rebuilt to mirror his module architecture and conventions, while keeping every piece of analytical capability we've already built that his gateway doesn't have. The end state is two professional, structurally-identical codebases, each strong where the other is weak, that can interoperate at the wire level.

## Non-negotiable constraint
We can modify our own repo (firmware + gateway) freely. We do NOT modify `edgeai-predictive-monitor` (the reference-repo maintainer's repo) at all — clone it read-only, purely as a reference to read from and test against. Any change we think he should make gets written up as a suggestion, never committed to his tree.

---

## Part A — What each side already has (read this before planning anything)

Before any code is written, both repos must be read fresh — do not work from a cached summary, work from the actual current source, since both repos may have moved on since anyone last looked.

### Our firmware (`src/`, `components/`)
ESP-IDF, ~2,000 lines: `main.c`, `mic_task.c`, `dsp_task.c`, `imu_task.c` (stub accelerometer), `wifi_task.c` (796 lines — owns WiFi, raw TCP, AES-GCM framing, mDNS, the adaptive-sensing reply protocol, LED policy), `components/mic_capture/` (a real reusable driver). Strengths: hardware-level correctness (core pinning, IRAM-safe ISRs, PSRAM placement, power management, AES-GCM encryption), an adaptive DSP loop (gateway can request more/less averaging and overlap). Weaknesses: flat structure, no HAL, no host-testable DSP, an unauthenticated control channel, a stubbed accelerometer, and at least two DSP correctness bugs to verify (window normalisation, Welch overlap hop sizing).

### Our gateway (`mic_tools/`)
Python, ~10,000 lines across 30 flat files, dominated by one file: `recv_verify.py` at ~4,276 lines. This is NOT a scratch folder — it's a real, working analytics stack:
- TCP server + AES-GCM decryption, multi-satellite registry with per-satellite persisted state
- `bearing_math.py` — BPFO/BPFI/BSF/FTF bearing-defect-frequency computation with harmonic overlays (the reference-repo maintainer's gateway has no equivalent of this — it's pure anomaly detection with no fault-type physics)
- `rul_estimator.py` — exponential + Kalman remaining-useful-life estimation
- ADWIN drift detection, Hoeffding-tree (HST) online learning
- 4-channel Bayesian sensor fusion (`z_kurtosis`, `z_rms`, `z_hst`, `z_ae`)
- ONNX autoencoder inference on the Adreno GPU path (`inference_gpu.py`), MLP autoencoder training (`train_autoencoder.py`)
- The adaptive-sensing control loop server-side (`_adaptive_overlap`, `_adaptive_avg_n`) matching the firmware's `g_adapt_*` reply protocol
- Uno Q sysfs RGB LED control (`_write_led`, `led_set_status`) — already written assuming eventual deployment on the actual Uno Q
- An HTTP dashboard, HTML report generation, webhook + email alerting, maintenance logging
- `sim_sweep.py` (1,669 lines) — parameter sweep tooling
- 7 tests total

### His base station (`base-station/`)
Two halves:
1. **`sketch/`** (Arduino, 4,358 lines) running on the Uno Q's STM32U585 MCU: its own onboard mic (`mic_sampler.cpp`, 96 kHz, 2048-pt FFT) and KX134 accelerometer (`accel_sampler.cpp`, real working driver), fused every 64 ms (`fuser.cpp`) into the same section-list frame format, sent to the MPU over a dedicated SPI+DMA link (`spi_link.cpp`) — NOT the shared Bridge UART, which was tried and failed (documented in `docs/progress2.md`).
2. **`python/`** running on the Uno Q's Linux MPU (QRB2210): the gateway. Verified live layout (2026-07-30): `ingestion/` (`mqtt_subscriber.py`, `mqtt_publisher.py`, `spi_reader.py`, `sensor_frame.py`), `pipeline/` (`manager.py`, `capture.py`, `commissioning.py`, `classifier.py`, `inference.py`, `autoencoder.py`, `features.py`, `gate.py`, `ei_client.py`, `ei_scaling.py`, `ei_projects.py`), `registry/` (`registry.py`, `matrix_status.py`, `status_color.py`), `common/` (`telemetry_frame.py`, `telemetry_schema.py`, `wire_protocol.py`, `raw_features.py`, `bridge_lock.py`), `api/` (`app.py`, `commissioning_controller.py`, `capture_controller.py`, `ei_controller.py`, `connection_manager.py`), `alerts/` (`alert_store.py`, `telegram_alerts.py`), `history/` (`store.py`, `retention.py`), `monitoring/` (`gpu_perf.py`), `network/` (`wifi.py`), `tools/` (`satellite_node_sim.py`, `gen_telemetry_schema.py`, `gen_synthetic_captures.py`, `raw_capture.py`, `raw_capture_server.py`, `offline_experiment.py`, EI upload/prep tooling). 28 tests under `base-station/tests/`. Edge Impulse integration for training. No bearing-fault physics, no RUL, no ADWIN/HST, no Bayesian fusion — his gateway is architecturally disciplined but analytically thinner than ours.
   - A local dev rig already exists: `base-station/start_desktop_dashboard.sh` runs `python/main.py` + N `tools/satellite_node_sim.py` processes against a local Mosquitto, no hardware required. Sim nodes are driven by `--captures-dir` of `.npz` files (or synthetic captures via `gen_synthetic_captures.py`) and an HTTP control API (`/config`, `/online`, `/state`) plus a browser UI.

### His satellite (`satellite/`)
ESP32-S3, Arduino framework, ~2,000 lines, MQTT transport, HAL-layered (`include/hal/hal_audio.h`, `hal_accel.h`, `hal_display.h`, `hal_transport.h`), drivers behind the HAL (`mic_i2s.cpp`, `kx134.cpp` — a real working accelerometer driver we don't have), one shared `scalar_stats.h`, generated `telemetry_schema.h` (under `satellite/include/frame_codec/`). No encryption, no power management, no diagnostics, arduinoFFT (GPL-3.0 — a licensing concern if this becomes a product), never run on hardware per his own comments.

---

## Part B — The merge strategy, decided (not to be re-litigated by the build sessions)

| Layer | Decision | Reasoning |
|---|---|---|
| Wire protocol | Adopt his exactly — MQTT, section-list frame, generated schema | It's the only format his unmodifiable base station accepts |
| Satellite firmware structure | Adopt his HAL/driver/task layering, keep our ESP-IDF choice | ESP-IDF is the right call for our feature set (SIMD, hardware AES, core pinning); his file shape is worth copying regardless of framework |
| Satellite DSP | Ours, bugs fixed, hardened | Averaged spectra, adaptive overlap, power management, IRAM safety — all real advantages, verify and fix the two known bug candidates |
| Satellite accelerometer driver | Port his KX134 driver as the reference, reimplement behind our `hal_accel.h` — retire `accel_task.c`'s stub as the default (kept only as a Kconfig dev fallback) | His is real and tested against hardware; ours is a stub |
| Satellite display driver | Adopt his NeoPixel (addressable RGB) driver + status-color convention in place of our plain-LEDC RGB LED task | Matches his hardware and his `registry/status_color.py` / `matrix_status.py` semantics so status meaning is identical on both sides |
| Sensor parameters (sample rates, FFT sizes, bin counts) | Verify both sides' actual live constants (ours: mic sample rate variants seen in `sdkconfig.mic_char_*`, his: 96 kHz / 2048-pt FFT) and adopt whichever is better-justified per-parameter, with an ADR recording the choice and why | Numbers were set independently on each side under different constraints (ours for adaptive multi-rate characterization, his for a fixed onboard mic) — neither is automatically right for the merged system |
| Gateway module structure | Adopt his layout: `ingestion/ pipeline/ registry/ api/ common/` | Ours is one 4,276-line file; his is disciplined and independently testable |
| Gateway analytics | Ours, ported into his structure | bearing_math, RUL, ADWIN, HST, Bayesian fusion, autoencoder — all stay, nothing is thrown away, just relocated into proper modules |
| Gateway frame decoding | Adopt his `telemetry_frame.py` / generated schema directly rather than maintaining a second decoder | one decoder, one source of truth, both our satellite and his can be decoded by the same code |
| Adaptive control loop | Ours, ported into his `pipeline/` structure, and propose he adds the matching cmd-topic message type on his firmware side (a suggestion to him, not a change we make in his repo) | it's a real capability neither base station currently offers his own satellites |
| Security | Retire our custom TCP+AES, since MQTT is the only thing his base station accepts; propose broker TLS as the confidentiality answer going forward | a bespoke protocol nobody else speaks is a dead end once interop is the goal |
| Testing discipline | His — host-testable pure-C DSP/codec, host-testable pure-Python pipeline modules, golden reference vectors, one test per extracted module | he has 28 tests, we have 7, on ten times the gateway code |
| Naming/conventions | His, applied uniformly on both sides | so he can read and maintain this without translation |

Nothing analytical gets deleted. Every capability in `recv_verify.py` gets a new home in the restructured tree; none of it is discarded in the name of matching his architecture.

---

## Part C — Target architecture, both halves

### C.1 — Satellite firmware
```
components/
  epm_hal/include/hal/       hal_audio.h  hal_accel.h  hal_display.h  hal_transport.h
                              pure C contracts, ZERO ESP-IDF includes
  epm_drivers/                mic_inmp441_i2s.c   (from components/mic_capture)
                               accel_kx134_spi.c   (ported from his satellite/src/drivers/kx134.cpp,
                                                     reimplemented behind hal_accel.h)
                               accel_stub.c        (KEPT, selectable via Kconfig, for dev without hardware)
                               display_neopixel.c   (REPLACES plain-LEDC RGB driver logic from
                                                     src/rgb_led_task.c — port his
                                                     satellite/src/drivers/rgb_ws2812.cpp behind
                                                     our hal_display.h, verified live 2026-07-30
                                                     as the correct analog; his repo also has a
                                                     separate base-station/sketch/rgb_display.cpp
                                                     and matrix_display.cpp for the Uno Q itself —
                                                     do NOT port those, they're a different board's
                                                     display, not the satellite's)
                               link_mqtt.c          (new — replaces TCP+AES transport)
  epm_dsp/                    scalar_stats.c/.h  window.c/.h  spectrum.c/.h  envelope.c/.h
                              pure C, ZERO ESP-IDF includes, HOST-TESTABLE
  epm_codec/                  telemetry_schema.h  (GENERATED — copy his schema.json, port his generator)
                               frame_encode.c/.h
                              pure C, HOST-TESTABLE
main/
  main.c                      wiring only
  epm_config.h                every constant, one-line rationale each
  board_pins.h                every GPIO, justified, pin-budget-checked against the XIAO's 11 usable pins
  threads/                    mic_task.c  dsp_task.c  imu_task.c  fuser_task.c
                              net_task.c  led_task.c  diag_task.c
schema/
  telemetry_schema.json        copied from his repo (extend only with his sign-off, since it's shared)
  gen_schema.py
docs/decisions/                ADR-NNN-*.md, numbered, append-only
tests/host/                    native gcc, no hardware
tests/golden/                  captured reference frames from his satellite_node_sim.py AND from ours
```

### C.2 — Gateway (`mic_tools/` → restructured, mirroring his `base-station/python/`)
```
gateway/
  ingestion/
    mqtt_subscriber.py         adapted from his module — subscribes to the confirmed data/cmd topics
    frame_decode.py            thin wrapper around the SHARED decoder in common/
  pipeline/
    manager.py                 commissioning, per-node sensor_config + input_dim inference
                                (port his validation rules: complete-frame requirement,
                                first-frame-commits rule, zero-fill convention)
    autoencoder.py              (from mic_tools/autoencoder.py + inference_gpu.py)
    rul.py                      (from mic_tools/rul_estimator.py)
    drift.py                    (ADWIN, extracted from recv_verify.py)
    online_learning.py          (Hoeffding tree, extracted from recv_verify.py)
    fusion.py                   (Bayesian fusion, extracted from recv_verify.py)
    adaptive_control.py         (_adaptive_overlap / _adaptive_avg_n, extracted)
    bearing_math.py             (moved as-is from mic_tools/ — already well-structured)
  registry/
    satellite_state.py          (SatelliteState class + persistence, extracted from recv_verify.py)
    baselines.py                (adaptive baseline load/save, extracted)
  api/
    dashboard.py                 (HTTP dashboard, extracted from recv_verify.py's _DashHandler)
    reports.py                   (_generate_report_html, extracted)
    notifications.py             (webhook + email, extracted)
    led_control.py               (Uno Q sysfs LED — kept, becomes a clean module instead of
                                   free functions in a 4000-line file)
  common/
    telemetry_schema.py          GENERATED — reuse his gen_telemetry_schema.py output directly
    telemetry_frame.py           frame decoder — adopt his implementation directly if license/
                                  structure allows; otherwise port it line-for-line
  main.py                        entrypoint, argument parsing, wiring only
tests/
  ingestion/  pipeline/  registry/  api/   one test module per extracted module, minimum
  golden/                                   reuse the firmware's golden captures as gateway
                                              decode-correctness fixtures
```
`sim_sweep.py` (1,669 lines) gets its own audit pass in a later phase — same treatment: split by responsibility, not left as a second monolith.

---

## Part D — Wire contract (VERIFY against his live source before implementing each phase — do not trust this table blindly; it is a draft until Phase 4 confirms it)

| | |
|---|---|
| Transport | MQTT to Mosquitto |
| Publish topic | `epm/<node_id>/data` (confirm exact string against `ingestion/mqtt_subscriber.py`) |
| Subscribe topic | `epm/<node_id>/cmd` (confirm exact string and payload format) |
| `node_id` | last N hex digits of the WiFi MAC — confirm exact digit count from his code |
| Payload | raw bytes, no length prefix, no envelope (MQTT provides framing) |
| Frame | `[num_sections u8]` then per section `[source_id u8][channel_id u8][data_kind u8][section_len u16][body]` |
| `source_id` | 1 for satellite (0 reserved for his base station's own onboard sensors) — confirm |
| SPECTRUM body | `[fs f32][fft_size u16][bin_count u16][bins f32...]` |
| SCALAR_SET body | `[count u8][ids u16...][values f32...]` |
| Endianness | little-endian |
| Kurtosis | excess (Gaussian ≈ 0) — confirm against current `scalar_stats.h`/`raw_features.py` |
| Peak | confirm current convention — his satellite historically used signed max, known failure mode on negative-going impacts; decide match-or-diverge with a documented ADR either way |
| Complete-frame rule | every frame carries the node's full committed channel set at once |
| First-frame-commits rule | `_infer_sensor_config_and_dim` locks channel set + input_dim from frame 1 — mismatch after that is silent, permanent frame-dropping, not an error |
| Zero-fill rule | a structurally-absent channel is still a present SPECTRUM section, real bin_count, all-zero values; bin_count=0 means "not part of this node" |
| Schema | generated from `base-station/telemetry_schema.json` via `base-station/python/tools/gen_telemetry_schema.py` |

Verification sources, in this order: `base-station/python/ingestion/mqtt_subscriber.py`, `base-station/python/common/telemetry_frame.py`, `base-station/python/common/wire_protocol.py`, `base-station/python/registry/registry.py`, `base-station/telemetry_schema.json`, `base-station/python/tools/satellite_node_sim.py` (best acceptance target — its published bytes are ground truth), `base-station/tests/telemetry_frame_test.py` + `mqtt_subscriber_test.py` + `satellite_node_sim_test.py` (run these against our output as the actual pass/fail bar), `docs/SENSOR_TELEMETRY_FRAME_PLAN.md`, `docs/Appendix_B_Wire_Protocol_Specification.md`.

**Correction, verified live 2026-07-30 (Phase 0 build session):** `mqtt_subscriber.py` is data-ingestion only — it subscribes `DATA_TOPIC_FILTER = "epm/+/data"` at QoS 0, derives `node_id = topic.split("/")[1]`, and does **no `source_id`/`num_sections` validation**; a malformed frame is silently counted into `self._dropped`, not rejected/nacked. It does **not** publish commands. Command publishing (the `cmd` topic direction, base station → satellite) lives in the separate `base-station/python/ingestion/mqtt_publisher.py` — verify that file, not `mqtt_subscriber.py`, for cmd-topic payload shape and QoS. Also confirmed from `satellite_node_sim.py`: nominal constants `NOMINAL_MIC_FS_HZ=48000`, `NOMINAL_MIC_FFT_SIZE=2048`, `NOMINAL_ACCEL_FS_HZ=6400`, `NOMINAL_ACCEL_FFT_SIZE=1024`, `--accel-bin-count` default 128 — these feed Part D.1's FFT-size row below. Note the sim's nominal accel rate (6400 Hz) does not match the real KX134 driver's ODR verified earlier in this doc (12800 Hz) — a sim-vs-real-hardware mismatch on his own side, flagged for Phase 9, not something to resolve on our end.

### Part D.1 — Sensor + display parameters to reconcile (added by Abhi 2026-07-30 — verify both sides live, table below is a draft of what to check, not the answer)

Neither side's numbers are automatically right for the merged system — each was tuned under different constraints. Every row needs a live read of both repos before deciding, and the decision gets an ADR either way (adopt his, keep ours, or a new merged value).

**Verified live 2026-07-30 (cheap check, done in this session — still confirm against your own repo's exact current source before implementing, this is a snapshot):**

| Parameter | Ours (verify against current source) | His — verified | Decision owner / phase |
|---|---|---|---|
| Mic sample rate | Multiple Kconfig variants present (`sdkconfig.mic_char_16k/22050/32k/48k` in our tree) — confirm which is the shipping default in `sdkconfig.defaults` vs. which are just characterization sweeps | Two different values on his side depending on board: his standalone **`satellite/`** ESP32 (`satellite/src/drivers/mic_i2s.cpp`) uses **48000 Hz** (`AUDIO_SAMPLE_RATE_HZ`); his **`base-station/sketch/`** Uno-Q onboard MCU mic (`mic_sampler.cpp`) uses **96000 Hz** (`MIC_SAMPLER_SAMPLE_RATE_HZ`), chosen so the I2S bit clock divides cleanly from the STM32's 76.8 MHz kernel clock — that clean-division reasoning is board-specific to the Uno Q, not a constraint we share. Since our satellite is the ESP32 competing with **his** ESP32 satellite (not his Uno-Q onboard mic), 48 kHz from `satellite/mic_i2s.cpp` is the directly comparable number | Phase 4 (contract) + Phase 2/6 (firmware) |
| Mic FFT size / bin count | Confirm current `dsp_task.c` FFT size and whether it's fixed or adaptive | Verified via `satellite_node_sim.py`'s nominal constants (2026-07-30): `NOMINAL_MIC_FS_HZ=48000`, `NOMINAL_MIC_FFT_SIZE=2048` — matches the satellite's 48 kHz mic rate confirmed earlier. Accel side: `NOMINAL_ACCEL_FS_HZ=6400`, `NOMINAL_ACCEL_FFT_SIZE=1024`, `--accel-bin-count` default 128 — note this 6400 Hz nominal does NOT match the real KX134 driver's 12800 Hz ODR verified elsewhere in this table; still unclear whether that's the sim using a lower-fidelity default or the accel section publishing at half the true ODR. Confirm `satellite/src/threads/mic_sampler_task.cpp`'s actual FFT size directly (not yet read) before treating the sim's nominal as ground truth for the real satellite firmware | Phase 4 |
| Accel ODR | N/A currently (stub) — this is set fresh in Phase 9 | Both of his boards converged on the **same value, 12800 Hz**, after real A/B hardware testing: `satellite/src/drivers/kx134.cpp` (`KX134_ODR_HZ 12800`, comment cites Nyquist/vibration-frequency reasoning) and `base-station/sketch/accel_sampler.cpp` (comment: tried 1600 Hz first, then 25600 Hz caused an issue traced specifically to ODR, settled on 12800 Hz on 2026-07-21 after live A/B testing) — also matches `satellite_node_sim.py`'s `DEFAULT_SAMPLE_RATE_HZ` per his own code comment. This is a strong, hardware-validated number to adopt as-is rather than re-litigate | Phase 9 |
| Fuse/epoch interval | Confirm our current task cadence in `dsp_task.c`/`imu_task.c` | Confirmed: `base-station/sketch/fuser.cpp`'s `FUSER_EPOCH_MS` ≈ 64 ms (~15.6 frames/s) in normal mode, with a separate slower `FUSER_RAW_EPOCH_MS` for raw-capture mode — matches the plan's original draft, no correction needed | Phase 6 |
| Display driver + status semantics | Confirm current `src/rgb_led_task.c` — plain LEDC RGB, no status-color table found yet | Confirmed NeoPixel/WS2812 on his side, and it's a real HAL-layered driver on his **satellite** specifically (not just the base station): `satellite/include/hal/hal_display_rgb.h`, `satellite/src/drivers/rgb_ws2812.cpp`, `satellite/src/threads/rgb_display_task.cpp`. Separately, his `base-station/sketch/` also has `rgb_display.cpp` (single RGB) AND `matrix_display.cpp` (LED matrix) — three distinct display concepts across his repo, don't conflate them: **port `satellite/`'s `rgb_ws2812.cpp` + `hal_display_rgb.h`** as the direct analog for our satellite, not the base-station matrix/rgb files | Phase 3 (move) + finalize before Phase 9 closes |

---

## Part E — Local test rig (no physical Uno Q available)

His Python gateway runs standalone off-hardware via `base-station/start_desktop_dashboard.sh` (only his SPI ingestion path needs the real board):
```
laptop:
  Mosquitto (local, anonymous)
  base-station/python        ← his code, run UNMODIFIED, as the acceptance target
  tools/satellite_node_sim.py ← his reference publisher, ground truth for frame bytes
  our ESP32-S3 firmware       ← WiFi to the same broker
  our restructured gateway    ← second subscriber, same broker, proves our decode path too
```

Project-level acceptance test: our firmware's frames pass his unmodified `telemetry_frame_test.py`/`mqtt_subscriber_test.py`, our node appears correctly on his unmodified dashboard, AND our own restructured gateway independently decodes and processes the same frames correctly (bearing overlays, RUL, fusion all producing sane output against live data).

---

## Part F — Execution model

Plan with Opus (the planning tool), build with Sonnet (VS Code the AI coding assistant), checkpoint every phase, fresh session per saturated context, the planning tool owns cross-phase state, small diffs and low token-per-turn output on the build side.

1. Planning pass (Opus/the planning tool, no code): at the start of each phase, re-read the relevant live source in both repos (never trust an earlier session's memory of it), confirm or correct Part D's contract table for anything that phase touches, produce a concrete file-level task list with an explicit exit test, saved as `docs/PHASE_N_PROMPT.md`.
2. Build pass (Sonnet, VS Code the AI coding assistant, terse, low-token-per-turn): hand it `docs/PHASE_N_PROMPT.md`. Work in small diffs, not restate unchanged code, skip prose explanations in favor of a one-line rationale per change, stop at the phase's stated exit test rather than opportunistically doing more.
3. Checkpoint (fresh context): a separate pass reads the diff cold against `docs/CONVENTIONS.md` and the phase goal before advancing.
4. New session per phase, not per file. Re-verify current repo state at the start of each phase rather than trusting prior session memory.
5. the planning tool's specific job: hold this document as the single source of truth across the whole effort, track phase completion, produce the correctly-scoped prompt for that phase only, run the checkpoint review, and re-verify against both live repos every phase rather than assuming nothing changed.

---

## Part G — Phased plan

Firmware and gateway phases are interleaved where they depend on each other; do not skip ahead.

### Phase 0 — Safety net + local rig
See `docs/PHASE_0_PROMPT.md`.
**Exit:** his sim node shows on his unmodified dashboard, driven from our devrig; golden frame captured.

### Phase 0.5 — Connectivity (added 2026-07-30, jumps ahead of Phase 1-3/5-6 deliberately; revised 2026-07-30 to build directly into the real target structure, no throwaway spike)

**Context change from Abhi:** the reference-repo maintainer's Uno Q base station is not just code anymore — it's a finished, physical, running device, and it will not be modified. Our job is now concretely "make our XIAO satellite join his real base station," with WiFi/MQTT connectivity as the immediate, highest-priority unknown — everything else (DSP correctness, gateway rebuild) is worth less until we know the satellite can actually join his network and broker.

**Revision:** Abhi asked explicitly that the satellite code follow his HAL/driver/task structure and naming discipline from the start, not as a later cleanup pass. So this phase does not write disposable "spike" code — it creates the real `components/epm_hal/`, `components/epm_drivers/`, `components/epm_codec/`, `main/threads/` locations from Part C.1 directly, with Part I's naming conventions applied immediately, additive alongside the existing TCP path (which Phase 7 formally retires later with its own ADR). What Phase 5/6/9 add later extends this code; nothing here gets thrown away and redone.

Verified live from his `satellite/` reference firmware (2026-07-30, read cold from source, not assumed):
- His Uno Q runs its own WiFi access point (`WIFI_SSID "EPM-BaseStation"`) and its own Mosquitto broker on `10.42.0.1:1883` (`satellite/include/app_config.h`) — `10.42.0.x` is the standard Linux NetworkManager hotspot range, confirming the Uno Q itself is the AP, not a separate router. **Our satellite must join that same SSID and reach 10.42.0.1:1883 directly — confirm the actual SSID/password/IP on the physical Uno Q Abhi has, since `EPM-BaseStation`/`10.42.0.1` are his firmware's defaults, not guaranteed to be what's actually configured on the real device.**
- `node_id` = last 3 octets of the WiFi MAC, lowercase hex, no separators (`derive_node_id()` in `satellite/src/threads/transport_task.cpp`).
- Publish topic `epm/<node_id>/data`, QoS 0, raw section-list frame bytes, no envelope, must fit a buffer sized for the worst case (~3 KB at `MODEL_SPECTRUM_BINS=128`, 4 channels + one SCALAR_SET section).
- Subscribe topic `epm/<node_id>/cmd`, QoS 1, payload `[TYPE:1B][body]`; today's only defined type is `MQTT_MSG_TYPE_STATUS_LED` with body `struct display_rgb_payload {rgb, mode, period_ms}` (`satellite/include/frame_codec/wire_protocol.h`).
- Connection pattern: block indefinitely on WiFi (no fallback — a satellite with no network has no job), reconnect MQTT on a 2000 ms backoff, PubSubClient-equivalent semantics (his is Arduino/PubSubClient; ours will be ESP-IDF native WiFi STA + `esp-mqtt`, same behavior, different library).
- Frame codec to port as-is for this spike: `satellite/include/frame_codec/{wire_protocol.h,spectrum_codec.h,telemetry_schema.h}` + their `.cpp` — this is Phase 5/6's real work pulled forward in minimal form, not re-invented.

**Tasks:**
1. Confirm the physical Uno Q's actual SSID, password, and broker IP with Abhi (or read them off the device) — do not assume the firmware defaults are what's deployed.
2. Port `wire_protocol.h/.cpp`, `spectrum_codec.h/.cpp`, `telemetry_schema.h` into a new `components/epm_codec_spike/` in our repo (temporary location, explicitly named `_spike` so Phase 3/5 knows to replace it, not merge with it).
3. Add ESP-IDF native `components/epm_drivers_spike/link_mqtt.c`: WiFi STA join, `esp-mqtt` client, `derive_node_id()` port, publish/subscribe exactly per the topics and payload shapes above.
4. Wire a minimal `main/spike_main.c` that publishes one valid heartbeat/SCALAR_SET frame every few seconds (real mic/accel data can come later — a zero-filled or synthetic-scalar frame that decodes correctly is the goal here, not full DSP).
5. Test against `base-station/tests/telemetry_frame_test.py` and `mqtt_subscriber_test.py` (run unmodified from the cloned reference repo) before ever touching real hardware.
6. Flash to the real XIAO, power on near the real Uno Q, confirm it appears in his **unmodified** dashboard's fleet view with a valid (even if minimal) frame.
7. ADR recording this as a deliberate spike, what it proves, and that Phase 3/5/6 supersede it.

**Exit:** our physical XIAO satellite appears in the reference-repo maintainer's unmodified, physically running Uno Q dashboard over real WiFi/MQTT, publishing a wire-valid frame. This unblocks confident execution of Phase 1-3 afterward with the hardest unknown (does WiFi/MQTT to his real device even work) already answered.

### Phase 1 — Host test harnesses, both sides, no behavior changes yet
Firmware: `tests/host/`, native CMake/gcc build. Anchor tests for `scalar_stats` (sine → crest 1.414; square → crest 1.0; Gaussian → kurtosis at reference value per whatever convention Phase 4 confirms) and Hann window coherent gain. These may currently FAIL against `src/dsp_task.c` — expected, that's the point.
Gateway: pytest harness for `mic_tools/` as it currently is, covering `bearing_math.py`, `rul_estimator.py`, `bayesian_fusion.py`, `adaptive_baseline.py` at minimum, BEFORE any restructuring — these become the regression net for Part C.2's extraction.
**Exit:** both harnesses run and report pass/fail with detail; gateway tests currently pass against unmodified `mic_tools/`.

### Phase 2 — Firmware DSP correctness fixes
Verify (don't assume) `src/dsp_task.c`'s window normalisation and Welch-overlap hop-sizing behavior against current source. Fix any confirmed defects: window normalisation should derive coherent gain from the actual window array rather than a hardcoded constant; Welch overlap should actually change FFT hop size with `overlap_pct` rather than always producing one FFT per incoming block. One commit per fix, one ADR per fix under `docs/decisions/`, including corrected variance-reduction math for overlapping segments if relevant.
**Exit:** Phase 1 firmware host tests pass; flashed spectra differ from Phase 0 baseline by a known, explained offset only, nothing else changes.

### Phase 3 — Firmware restructure to target layout (pure move, zero behavior change)
Move code into `components/epm_hal`, `epm_drivers`, `epm_dsp`, `epm_codec`, `main/threads` per Part C.1. Move-and-rename only, no logic changes. Split `src/wifi_task.c`: connection state machine → `main/threads/net_task.c`, framing/encoding → `components/epm_codec` (transport implementation itself comes in Phase 7).
**Exit:** firmware builds; flashed output byte-identical to Phase 2; `epm_dsp` and `epm_codec` confirmed to build with zero ESP-IDF includes.

### Phase 4 — Contract verification pass (Opus/the planning tool planning only, no code)
Read live in the reference-repo maintainer's repo: `mqtt_subscriber.py`, `common/telemetry_frame.py`, `common/wire_protocol.py`, `registry/registry.py` (`_infer_sensor_config_and_dim`), `telemetry_schema.json`, `tools/satellite_node_sim.py`, `tests/telemetry_frame_test.py`, `tests/mqtt_subscriber_test.py`, `docs/SENSOR_TELEMETRY_FRAME_PLAN.md`, `docs/Appendix_B_Wire_Protocol_Specification.md`. Output an updated, corrected version of Part D as `docs/BASE_STATION_CONTRACT.md`. Flag every place it differs from this plan's draft. No firmware or gateway code changes in this phase.
**Exit:** `docs/BASE_STATION_CONTRACT.md` exists, is current, and is reviewed by Abhi before Phase 5.

### Phase 5 — Schema generation, shared by firmware and gateway
Copy the reference-repo maintainer's `telemetry_schema.json` into `schema/`. Port his `gen_telemetry_schema.py` to emit BOTH `components/epm_codec/telemetry_schema.h` (firmware) AND `gateway/common/telemetry_schema.py` (our gateway), from the one JSON, with his exact "GENERATED ... DO NOT EDIT BY HAND" banner on both outputs.
**Exit:** changing a channel definition in the JSON and regenerating updates both firmware and gateway headers correctly with zero hand edits.

### Phase 6 — Firmware: fuser task + section-list encoder
Add `main/threads/fuser_task.c` per the confirmed contract in `docs/BASE_STATION_CONTRACT.md`: fixed epoch, drains mic+IMU queues, one frame per epoch with the full committed channel set, `source_id=1`, zero-fill rule honored. Implement `components/epm_codec/frame_encode.c` for the section-list format. `tests/host/test_frame_encode.c` must produce bytes that pass the reference-repo maintainer's own `telemetry_frame_test.py` directly.
**Exit:** his unmodified `telemetry_frame_test.py` accepts our encoded frames.

### Phase 7 — Firmware: MQTT transport
Implement `components/epm_drivers/link_mqtt.c` behind `hal_transport.h` (esp-mqtt). `node_id`, topics, QoS exactly per `docs/BASE_STATION_CONTRACT.md`. Retire the TCP+AES path; add an ADR marking the earlier transport ADRs as superseded (not deleted) with the reasoning: fleet interoperability outweighs a bespoke protocol, confidentiality moves to broker TLS as a later hardening item. Preserve power management (DFS, light sleep, TX cap) through this change — verify and note in the ADR that it survived.
**Exit:** our ESP32 publishes to the Phase 0 local Mosquitto rig and appears on the reference-repo maintainer's unmodified dashboard with a correct live spectrum. **This is the core project deliverable.**

### Phase 8 — Gateway: extract and restructure `mic_tools/`
Using Part C.2's target layout, extract `recv_verify.py`'s ~15 responsibilities into `gateway/ingestion, pipeline, registry, api, common`. Move every existing capability without loss: bearing_math, RUL, ADWIN, HST, Bayesian fusion, autoencoder inference, adaptive control loop, per-satellite persistence, alerting (webhook/email), dashboard, report generation, Uno Q LED control. Reuse the reference-repo maintainer's `common/telemetry_frame.py` decoder (ported in Phase 5) as the single frame-decode path rather than writing a second decoder. Add or migrate a pytest module per extracted package. The Phase 1 gateway tests must all still pass at the end of this phase, now against the restructured code.
**Exit:** Phase 1 gateway tests pass unmodified against the new structure; our gateway and the reference-repo maintainer's unmodified gateway can both run as separate subscribers on the same broker and both correctly process frames from either satellite (his and ours).

### Phase 9 — Gateway: real KX134 driver on our firmware
Port the reference-repo maintainer's `satellite/src/drivers/kx134.cpp` as the reference implementation, reimplemented behind our `components/epm_hal/include/hal/hal_accel.h` contract as `components/epm_drivers/accel_kx134_spi.c`. Keep `accel_stub.c` selectable via Kconfig for hardware-free development. Follow the register sequence and bring-up order exactly as documented in his `docs/KX134_Interface_Appendix.md` and `docs/SATELLITE_BRINGUP_GUIDE.md` — do not skip bring-up steps.
**Exit:** WHO_AM_I reads correctly, at-rest axis readings match expected g-values, FIFO+interrupt path delivers data with zero dropped frames over a sustained run, IMU channel appears correctly in published frames.

### Phase 10 — Hardening + full convention compliance pass
Firmware: match his error-handling pattern (drivers report, tasks own recovery with backoff, no silent infinite retry). Add per-module timing/health counters mirroring his fuser bench-stats shape.
Gateway: same discipline applied to every extracted module — explicit error handling, no bare excepts, logging over silent failure.
Both: full pass against `docs/CONVENTIONS.md` across the entire repo — naming, comment style, ADR completeness, no dead code, no debug prints.
**Exit:** unplug a sensor mid-run — firmware degrades visibly and recovers without reboot; gateway handles a malformed/late frame without crashing.

### Phase 11 (optional, differentiator) — Envelope analysis
Firmware: `components/epm_dsp/envelope.c` (band-pass → rectify → low-pass → decimate → FFT), exposed as a new schema channel. Coordinate the schema addition with the reference-repo maintainer before implementing, since it touches the shared JSON.
Gateway: wire the envelope channel into `bearing_math.py`'s overlay logic so BPFO/BPFI/BSF/FTF markers are drawn against the envelope spectrum, where the defect frequencies are actually visible, rather than the raw spectrum.
Test: a carrier amplitude-modulated at a known low frequency produces a clear peak with harmonics in the envelope spectrum and no such peak in the raw spectrum.

---

## Part H — Git / commit standards (every phase, no exceptions)
- One logical change per commit; never mix a move with a behavior change.
- Conventional commits: `type(scope): imperative summary`. Types: `feat fix refactor perf docs test chore build`.
- Squash "wip"/"fix typo" commits before merging.
- Zero AI/tool attribution anywhere — no co-author trailers, no "generated with," no mention in code, comments, commit messages, or docs. Before every push: `git log --format='%B' | grep -iE 'claude|generated with|co-authored'` must return nothing.
- PR description per phase: what changed, why, what was verified against the reference repo, link to the phase's ADR(s).
- A human reads every phase's diff before merge, in addition to the automated checkpoint pass.

## Part I — Naming and conventions to match exactly
`snake_case` throughout, `module_verb_object()` function names, `#pragma once`, task/module starters return `int` (`0`/`-errno`), one `<module>_get_stats()` accessor per module with counters, comments explain constraint + cite the failure they prevent + accepted trade-offs, generated files carry the `GENERATED ... DO NOT EDIT BY HAND` banner, `docs/decisions/ADR-NNN-*.md` numbered and append-only (superseded, never deleted), doc filenames like `docs/<TOPIC>_PLAN.md` for design and `docs/Appendix_*.md` for reference material.

## Part J — What to send the reference-repo maintainer, and when
Do not contact him until Phase 4 surfaces a real, specific question his source code didn't answer — never ask something answerable by reading his repo. When you do, lead with the concrete question, and close with something useful found along the way (e.g., his fuser discards ~89% of the spectra it computes to sample-and-hold — accumulating instead would give him a ~3× cleaner noise floor for free; or an I2S DMA overflow counter his mic path lacks). Also worth raising once Phase 7 lands: the adaptive-sensing cmd-topic message type our gateway supports and his firmware doesn't yet — offered as an addition to the shared schema, not a fork of it.

---

## Phase tracker (the planning tool maintains this)

**2026-07-30 addendum from Abhi:** display driver target changed from plain-LEDC RGB to NeoPixel (matching his hardware), IMU stub is confirmed to be fully replaced by the real KX134 driver (not kept as default), and every sample-rate/FFT-size/epoch-interval number on both sides must be verified live and the better one adopted per-parameter (see Part D.1) rather than assumed from either side's docs. Folded into Part C.1, Part B's decision table, and Part D.1 above.

Live verification done in this session (cheap read of the already-cloned reference repo, not a full phase pass): confirmed his `satellite/` ESP32 has its own real NeoPixel/WS2812 HAL driver (`hal_display_rgb.h`, `rgb_ws2812.cpp`, `rgb_display_task.cpp`) — separate from his Uno-Q `base-station/sketch/rgb_display.cpp` + `matrix_display.cpp`, which are a different board's display and should NOT be ported. Confirmed accel ODR of 12800 Hz is hardware-validated and consistent across both of his boards — adopt as-is. Confirmed his two boards use *different* mic sample rates (satellite 48 kHz vs. Uno-Q onboard 96 kHz) — our satellite should compare against his `satellite/` 48 kHz, not the Uno-Q's 96 kHz. Confirmed fuser epoch (~64 ms) matches the plan's original draft. FFT size/bin count still unverified — flagged for Phase 4. None of this is a substitute for each phase's own live re-verification; it just seeds Part D.1 with real numbers instead of placeholders.

| Phase | Status | Notes |
|---|---|---|
| 0 | in progress | Branch + tag created (`feat/base-station-interop`, `baseline-working`). Reference repo verified read-only against live source (2026-07-30): module path is `base-station/python/` not `mpu/`; his own `docs/Running_Dashboard_And_Satellite_Sim.md` is stale re: paths; `start_desktop_dashboard.sh` is a ready-made devrig using `--captures-dir` (.npz) not `--data-dir` (CSV) as originally assumed. `docs/PHASE_0_PROMPT.md` handed to VS Code the AI coding assistant for execution (clone ref repo locally, install Mosquitto, run the rig, capture golden frame, build `tools/devrig/` wrappers). |
| 1 | not started | |
| 2 | not started | |
| 3 | not started | |
| 4 | not started | |
| 5 | not started | |
| 6 | not started | |
| 7 | not started | |
| 8 | not started | |
| 9 | not started | |
| 10 | not started | |
| 11 | not started | (optional) |
