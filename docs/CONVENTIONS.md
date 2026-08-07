# Conventions

The naming, structure, and process rules this repo follows — firmware and
gateway alike. This is the one file to read before touching code here;
the project's internal planning/build-log doc is a separate,
non-style reference. Everything below has been enforced in practice across the whole
interop effort (Phases 0–10c), not just proposed — each rule cites a real
file where you can see it applied.

## Naming

- **`snake_case` throughout** — files, functions, variables, struct fields,
  both C and Python. No camelCase, no PascalCase outside Python class names
  (`GPUInferenceEngine`, `MqttIngestor`), which follow ordinary Python
  convention rather than this repo's own rule.
- **`module_verb_object()` function names** — e.g. `hal_accel_read_block()`,
  `link_mqtt_start()`, `rgb_led_set_state()`, `dsp_task_get_stats()`. The
  module prefix makes call sites greppable and unambiguous even when two
  modules expose a similarly-named verb (`rgb_led_get_stats()` in both
  `display_ledc.c` and `display_neopixel.c` — see "Swappable drivers"
  below).
- **Doc filenames**: `docs/<TOPIC>_PLAN.md` / `docs/<TOPIC>.md` style design
  and reference docs (`docs/BASE_STATION_CONTRACT.md`,
  `docs/GPU_SETUP.md`), `docs/Appendix_*.md` for appendix-style reference
  material, `docs/decisions/ADR-NNN-*.md` for decision records (see ADRs,
  below). Screaming-snake-case for the filename itself, lowercase-kebab
  only inside an ADR's descriptive suffix (`ADR-017-kx134-spi-accelerometer-driver.md`).
  `docs/gpu_setup.md` was the one holdout against this until Phase 10c's
  sweep renamed it to `docs/GPU_SETUP.md` and fixed its two referencing
  files (`README.md`, `gateway/pipeline/inference_gpu.py`).

## Headers

- **`#pragma once`** on every header in the tree, with one deliberate
  exception: `components/epm_codec/include/frame_codec/telemetry_schema.h`
  uses `#ifndef`/`#define`/`#endif` guards instead. That file is generated
  by `schema/gen_schema.py`, itself a direct port of the reference repo's
  `base-station/python/tools/gen_telemetry_schema.py` — same validation and
  rendering logic, on purpose, so a diff against the reference generator's
  output stays meaningful. Changing the guard style would be a cosmetic
  firmware-side change for zero benefit, at the cost of that parity. Not a
  gap to "fix" — if `schema/gen_schema.py`'s `render_header()` ever changes
  guard style, it should be because the reference generator changed, not
  because this file looked inconsistent in a grep.
- Every other header — checked by grepping for the pragma's presence
  anywhere in the file, not just line 1 (headers with a comment block
  above the pragma will false-positive on a naive `head -1` check) — has
  it. Confirmed repo-wide as part of Phase 10c's sweep.

## Task / module starters

- Return `int` (`0` on success, `-errno` on failure) where the caller can
  meaningfully react to failure at startup — e.g. `net_task_start()`
  (`src/threads/net_task.c`), which propagates `link_mqtt_start()`'s
  return so `main.c` can log a failed MQTT bring-up without crashing the
  whole node.
- **Established exception, not a violation**: `led_task_start()`,
  `mic_task_start()`, `dsp_task_start()`, and `imu_task_start()` all return
  `void`. Each does its fallible one-time setup (`xQueueCreate()`,
  `xRingbufferCreateStatic()`) via `configASSERT()`/`ESP_ERROR_CHECK()`
  instead — a deliberate, different philosophy: these are resources the
  task cannot run without at all, so the correct response to failure is
  "crash immediately and loudly during boot," not "return an error code
  the caller might not check." This is a second, equally-legitimate
  starter idiom that coexists with the return-`int`-and-log one; converting
  these four to `int` would be a real (and out-of-scope) behavior change,
  not a naming fix.
- Fallible driver calls made *inside* a starter or task loop, where the
  task should keep running degraded rather than assert, follow the
  return-and-log pattern instead — see `imu_task_start()`
  (`src/threads/imu_task.c`), which checks `hal_accel_init()`'s and
  `hal_accel_start()`'s return values and logs on failure (`ESP_LOGE`)
  without aborting, matching `net_task_start()`'s handling of
  `link_mqtt_start()`. Fixed in Phase 10c's sweep — both calls were
  previously discarding a real `-errno`/`0` return from
  `components/epm_drivers/accel_kx134_spi.c`.

## `<module>_get_stats()` accessors

One per module, filling a module-owned `struct <module>_stats` with
free-running counters — never resetting state, never allocating. Twelve
real implementations exist across the tree today (Phase 10a): every task
(`wifi_task_get_stats()`, `mic_task_get_stats()`, `dsp_task_get_stats()`,
`imu_task_get_stats()`, `net_task_get_stats()`, `led_task_get_stats()`) and
every swappable driver pair (`hal_accel_get_stats()` in both
`accel_kx134_spi.c` and `accel_stub.c`; `rgb_led_get_stats()` in both
`display_ledc.c` and `display_neopixel.c`), plus `link_mqtt_get_stats()`
and `mic_inmp441_i2s_get_stats()`. `link_mqtt_get_stats()`
(`components/epm_drivers/link_mqtt.c`) is the reference shape to copy for
a new module. All twelve are wired into `main.c`'s 30-second diagnostics
logger, confirmed by direct inspection of the wiring code in Phase 10a,
not just a grep count.

### Swappable drivers

Where a module has both a real hardware driver and a Kconfig-selectable
stub (`accel_kx134_spi.c` / `accel_stub.c` behind `EPM_ACCEL_USE_STUB`;
`display_neopixel.c` / `display_ledc.c` behind `EPM_DISPLAY_USE_LEDC`),
both sides implement the exact same `hal_*`/`<module>_verb_object()`
function names against the shared `hal/hal_*.h` contract, including
`_get_stats()`. `default n` on both Kconfig options means the real
hardware driver is what a plain build gets; the stub exists for
development/CI without the physical part wired up — see
`docs/decisions/ADR-016-neopixel-display-driver.md` and
`docs/decisions/ADR-017-kx134-spi-accelerometer-driver.md`.

## Error handling

Drivers report; tasks own recovery. A driver-level function
(`hal_accel_read_block()`, `kx134_write_reg()`, an `esp_err_t`-returning
LEDC/RMT call) returns its real status and does not retry, escalate, or
loop internally. The task that called it decides what "recovery" means:
log once per failure streak (not every tick — `display_ledc.c`/
`display_neopixel.c`, Phase 10a), or count consecutive failures and
escalate past a threshold (`imu_task.c`'s per-axis `fail_cnt`/
`IMU_FAIL_MAX` pattern, mirrored from `mic_task.c`'s pre-existing one,
Phase 10a). No silent infinite retry, and no silently discarded return
value — both were real bugs found and fixed, not hypothetical risks:
Phase 10a found `imu_task.c` discarding `hal_accel_read_block()`'s return
entirely (a disconnected KX134 would go unnoticed while stale data kept
publishing) and two display drivers writing to hardware without checking
`esp_err_t`; Phase 10b found 6 of 9 `except Exception:` sites in the
gateway silently swallowing errors (`notifications.py`, `ml_scoring.py`,
`baselines.py`, `satellite_state.py`) and fixed each with real logging;
Phase 10c found `imu_task_start()` itself discarding `hal_accel_init()`/
`hal_accel_start()`'s returns. Where a call truly can't fail
meaningfully or a gap is a real feature absence rather than a
missing error check, that's said outright rather than patched over —
`display_ledc.c` has no `rgb_led_set_remote()` implementation despite the
HAL contract declaring it, correctly logged as a scope gap in Phase 10a's
tracker row, not silently stubbed in.

## Comments

Explain the constraint, cite the failure it prevents, and note any
accepted trade-off — not what the code already says by being well-named.
Example: `components/epm_drivers/link_mqtt.c`'s heap-guard comment names
the exact ESP-IDF bug (`esp_mqtt_client_init()` never checking
`esp_event_loop_create()`'s return), the exact crash it causes
(`LoadProhibited` in `esp_mqtt_client_register_event()`), and links
`docs/decisions/ADR-024-esp-mqtt-heap-guard.md` for the full trace. A
comment that only restates the next line's logic is worth deleting rather
than keeping.

## Generated files

Any file emitted by a script, not hand-maintained, carries a
`GENERATED ... DO NOT EDIT BY HAND` banner at the top — currently
`components/epm_codec/include/frame_codec/telemetry_schema.h` and
`gateway/common/telemetry_schema.py`, both produced by
`schema/gen_schema.py` from `schema/telemetry_schema.json`. Changing a
channel definition means editing the JSON and re-running the generator,
never hand-editing either output.

## Architecture Decision Records

`docs/decisions/ADR-NNN-*.md`, numbered sequentially starting at `001`,
append-only: a decision that's later reversed gets a new, higher-numbered
ADR that says so and references the one it supersedes (e.g. `ADR-023`
marks the earlier transport ADRs superseded rather than deleting them;
`ADR-018` reverses `ADR-014`'s kurtosis-convention call). Never edit or
delete a past ADR to make it retroactively "correct." 30 ADRs exist as of
Phase 10c (`ADR-001` through `ADR-030`), sequential, no gaps.

## Git / commit standards

- One logical change per commit — never mix a file move with a behavior
  change, or a bug fix with a rename.
- Conventional commit format: `type(scope): imperative summary`. Types:
  `feat fix refactor perf docs test chore build`.
- Squash `wip`/`fix typo` commits before merging.
- **Zero AI/tool attribution, anywhere** — no co-author trailers naming an
  AI tool, no "generated with," no mention in code, comments, commit
  messages, or docs. Verify before every push (this pattern intentionally
  matches the literal strings this policy forbids, so it can detect any of
  them appearing anywhere):
  `git log --format='%B' | grep -iE 'claude|generated with|co-authored'`
  must return nothing. (Pre-existing AI-co-author trailer lines from
  before 2026-07-30 predate this rule and are left alone — rewriting them
  needs a force-push and explicit sign-off, tracked as a known open item,
  not a Part H violation.)
- A human reads every phase's diff before merge, in addition to whatever
  automated checks ran.
