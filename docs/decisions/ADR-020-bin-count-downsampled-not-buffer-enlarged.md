---
id: ADR-020
title: Wire spectra downsampled to 128 bins instead of enlarging MQTT buffers
status: accepted
date: 2026-08-05
deciders: Abhinav Krishna N
---

## Context

`docs/PHASE_6B_PROMPT.md` (rescoped 2026-08-05 after reading the reference
`base-station/python/registry/registry.py` live) found that the phase's
original premise — "our bin counts must match the base station's nominal
128" — is wrong: `fs`/`fft_size`/`bin_count` are self-describing per-frame
fields (`components/epm_codec/include/frame_codec/spectrum_codec.h`'s
`struct spectrum_channel`), not enforced by the schema, and `input_dim` is
inferred from whatever the node's actual first frame contains
(`Registry.add()`'s own docstring). Phase 0.5's devrig replay already proved
this by committing `input_dim=536` from our own synthetic frame with zero
complaint. There is no protocol requirement to match the reference's nominal
Fs or bin count.

The real, still-live problem is size. Our full native FFT resolution — mic
512 bins (`FFT_MIC_N/2` = 1024/2) and accel 1024 bins/axis (`FFT_IMU_N/2` =
2048/2), `src/epm_config.h`) — produces a frame of roughly 14.5 KB
(`SPECTRUM_SECTION_OVERHEAD` = 13 B/channel + bin_count×4 B, across 4
channels, plus the scalar section). That exceeds both
`EPM_NET_FRAME_BUF_BYTES` (4096 B, `src/epm_config.h`) and esp-mqtt's
`buffer.size`/`buffer.out_size` (4096 B each, hardcoded in
`components/epm_drivers/link_mqtt.c`). Something has to give: either those
buffers grow to fit native resolution, or the spectra get reduced to fit the
existing budget.

## Options considered

### Option A: enlarge `EPM_NET_FRAME_BUF_BYTES` + esp-mqtt's buffers to ~16 KB
Simplest on paper — no new DSP code, full native spectral resolution reaches
the wire. Rejected on heap-margin evidence specific to this board:

- `heap_soak_log_v2.txt` (captured under the pre-MQTT TCP+AES path) shows
  internal DRAM free hovering at ~7–10 KB under normal load — already far
  below ADR-009's own ">200 KB DRAM free" target.
- `net_task`'s MQTT path runs *concurrently* with that TCP path today
  (Phase 7, which retires TCP+AES, hasn't landed yet) — enlarging MQTT
  buffers adds load on top of an already-tight budget, not in place of
  anything.
- `docs/MASTER_PLAN.md`'s Open Items already documents a latent esp-mqtt bug
  (`esp_mqtt_client_init()` never checks `esp_event_loop_create()`'s return
  value; under tight heap the allocation fails and the next call
  null-derefs) that reproduces specifically under this kind of margin
  pressure — it's the reason `EPM_ACCEL_USE_STUB`'s default hasn't been
  flipped to the real KX134 driver yet.
- Whether esp-mqtt's internal `buffer.size`/`buffer.out_size` allocations
  can even be placed in PSRAM (the precedent ADR-009 used for other
  cold-path buffers, `EXT_RAM_BSS_ATTR`) is unverified — esp-mqtt is a
  vendored component and its buffer allocation path hasn't been audited for
  PSRAM-capable placement.

### Option B: downsample spectra to 128 bins before they reach the wire
Costs real spectral resolution — 512→128 is a 4:1 reduction for mic, 1024→128
is 8:1 for accel — and adds new DSP code with no existing reference to port
(`docs/BASE_STATION_CONTRACT.md` confirms 128 is not a protocol requirement,
just a convenient target). In exchange: zero buffer or Kconfig changes
anywhere, the reduced frame fits the *existing* 4096 B budgets with room to
spare (128 bins × 4 channels ≈ 2251 B total — this is exactly what
`net_task.c`'s synthetic frame already sends today, per its own comment), and
none of the heap-margin risk above is introduced.

## Decision

**Option B.** Given DRAM headroom on this board is already measured close to
its limit under normal load, and the MQTT and TCP transports are running
side by side rather than one replacing the other, growing static/heap
footprint is the higher-risk path right now. A `epm_dsp_reduce_bins()` helper
is added to `components/epm_dsp/spectrum.c`, alongside the existing
`epm_dsp_accumulate_power`/`epm_dsp_power_to_db` (which already established
this component's linear-power ⇄ dB conversion convention). It reduces an
`in_n`-bin dB spectrum to `out_n` bins by converting each output bin's source
band back to linear power, averaging, and reconverting to dB — power-domain
averaging, not a dB-domain mean, to stay energy-correct.

This phase adds the function in isolation only, host-tested against a known
spectral feature (`tests/host/test_spectrum.c`). It is **not** wired into
`dsp_task.c`, `imu_task.c`, or `net_task.c` here — `fuser_task.c` (Phase 6c)
is what actually assembles the real wire frame from DSP output, and is the
natural place to call this function per-channel before encoding.

## Consequences

- No `EPM_NET_FRAME_BUF_BYTES` or esp-mqtt buffer-size change is needed;
  frame size at 128 bins stays within the existing budget with headroom.
- Spectral resolution on the wire is coarser than our native FFT output
  (4:1 mic, 8:1 accel) — acceptable since the base station imposes no
  bin-count requirement and 128 bins is what its own reference satellite
  publishes.
- New, currently-unreferenced code exists in `epm_dsp` until Phase 6c wires
  it in — expected and documented, matching the phase prompt's explicit
  scope ("don't wire it into dsp_task.c/imu_task.c yet").
- If a future phase finds DRAM headroom has genuinely improved (e.g. after
  Phase 7 retires the TCP+AES path entirely), Option A becomes worth
  revisiting — this ADR records the trade-off, not a permanent ban on larger
  buffers.

## Validation

`components/epm_dsp/spectrum.c` — `epm_dsp_reduce_bins()`.
`tests/host/test_spectrum.c` — divisibility guard, 512→128 and 1024→128
reduction with a known single strong bin (asserts the reduced spectrum's max
lands in the expected output band, band position preserved, value close to
the analytically expected band-averaged power), flat-spectrum sanity case.

## Addendum (2026-08-08): wire fft_size wasn't updated to match the pooling

This decision reduces `bin_count` to 128 but never revisited what `fft_size`
should say once pooling is in play — `net_task.c`'s channel builder kept
sending the *native* `FFT_MIC_N`/`FFT_IMU_N` (1024/2048) alongside the
pooled `bin_count`. `gateway/common/telemetry_frame.py`'s own docstring
already documents the correct convention ("fft_size is NOT the sender's
native FFT length whenever it pools bins down before sending" — this file
was ported from the reference base station and got it right from the
start), but the firmware violated its own wire contract: any consumer
computing bin width the standard way (`fs / fft_size`) recovered the
native, pre-pooling bin width instead of the true pooled one, off by
exactly `epm_dsp_reduce_bins()`'s pooling factor (4× for mic, 8× for
accel).

Found during real-hardware bench validation
(`docs/performance/BENCH_SIGNAL_GEN_HARDWARE_RUN.md`'s 2026-08-08 addendum):
the mic channel's apparent wire spectrum capped at ~1992 Hz despite a
16 kHz sample rate that should allow up to 8 kHz Nyquist, and accelerometer
peak-frequency readings landed on suspiciously clean multiples of the
*native* 12.5 Hz bin width rather than the true 100 Hz pooled width.

Fix: `src/epm_config.h` adds `EPM_MIC_WIRE_FFT_SIZE`/`EPM_IMU_WIRE_FFT_SIZE`
(native fft_size divided by `epm_dsp_reduce_bins()`'s own `band` — 256 for
both channels, since `band` differs but `native_fft_size / band` doesn't),
plus compile-time `#error` guards asserting the `FFT_MIC_N/FFT_IMU_N`
divisibility `epm_dsp_reduce_bins()` already requires at runtime.
`net_task.c`'s channel builder now sends these instead of the raw
`FFT_MIC_N`/`FFT_IMU_N` for the 4 pooled channels (mic, accel x/y/z). `fs`
and `bin_count` are unchanged. The 3 envelope channels (Phase 11a /
ADR-032) are unaffected — they were never pooled, since
`IMU_ENVELOPE_HALF` is already build-time-asserted equal to
`EPM_MODEL_SPECTRUM_BINS`.

No gateway-side change was needed: `telemetry_frame.py` already decodes
`fft_size` opaquely per its documented pooled-aware convention, and no
other consumer (`autoencoder.py`, `mqtt_subscriber.py`, `dashboard.py`,
`led_control.py`) reads `fft_size` off a decoded frame at all. (Separately,
`gateway/api/live_plot.py`'s raw mic/imu spectrum panels build their
frequency axis from CLI-hardcoded native sizes rather than the decoded
frame, and already silently never render since their axis length doesn't
match the wire `bin_count` — a pre-existing, unrelated bug this addendum
does not touch.)

`tests/host/test_spectrum.c` gained `test_wire_fft_size_true_bin_width()`,
asserting `MIC_FS_HZ / EPM_MIC_WIRE_FFT_SIZE == 62.5` and
`IMU_FS_HZ / EPM_IMU_WIRE_FFT_SIZE == 100.0` so this can't silently
regress.
