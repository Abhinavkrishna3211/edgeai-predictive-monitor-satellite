---
id: ADR-035
title: Bench signal-generator uses a laptop speaker, not dedicated shaker hardware
status: accepted
date: 2026-08-08
deciders: Abhinav Krishna N
---

## Context

`tools/bench_signal_gen/` needs a real, physically-generated excitation
source to validate the satellite's on-device DSP pipeline (FFT peak
location, RMS/kurtosis/crest_factor/skewness) against known-good ground
truth — synthetic MQTT bytes alone would only prove `decode_frame()` round-
trips correctly (already covered by `tests/common/test_telemetry_frame.py`
and `tests/ingestion/test_mqtt_subscriber.py`), not that the satellite's
actual mic/accelerometer/DSP chain reproduces a physically real signal
accurately.

This is the first hardware/production test campaign work (Phase 1 of a
3-phase plan: this tool + a DSP-accuracy run now, fault-detection accuracy
extending this same tool next, then a soak/stress campaign). No dedicated
excitation hardware (shaker table, vibration motor, calibrated speaker) is
available for this phase — a laptop and its built-in speaker is what's on
hand.

## Options considered

### Option A: wait for dedicated shaker/excitation hardware
Defer this entire bench-tooling phase until a proper vibration
table/shaker or calibrated reference speaker is procured.

Rejected: the DSP-accuracy question (does the satellite's FFT/scalar
pipeline compute correct values for a signal at a known frequency and
amplitude?) doesn't require calibrated excitation to answer for the mic
channel — a laptop speaker at a fixed measured distance is a real,
physically generated tone, and closed-form sine statistics are exact
regardless of what produced the sine. Blocking the whole phase on
procurement neither this repo nor this session controls would stall real
signal-path validation that's available today.

### Option B: laptop speaker, with the accelerometer channel's scope named honestly (chosen)
Use the laptop's built-in speaker for both channels: air-coupled for mic
(no scope limitation — speakers reproduce audio-band frequencies well), and
contact-coupled (board resting against the speaker cone) for the
accelerometer. Explicitly scope the accelerometer channel to ~20–150 Hz,
matching a bare laptop speaker's limited cone excursion and negligible
sub-20 Hz output, rather than implying full-range accelerometer validation
that the equipment can't actually produce.

## Decision

**Option B.** `tools/bench_signal_gen/` drives a laptop speaker for both
the mic channel (full audio-band tones, no scope limitation) and the
accelerometer channel (contact-coupled, board resting on the cone, scoped
to ~20–150 Hz). This is an equipment-availability constraint, not a
technical preference for speakers over shaker hardware — a cheap vibration
motor or bass-shaker transducer would extend the accelerometer channel's
usable range and remove the air/contact-coupling mismatch, and is called
out in `tools/bench_signal_gen/README.md` as a later improvement, not
pursued now because it isn't available.

**`capture_and_compare.py` applies no hardcoded pass/fail tolerance band on
this first version.** A real capture goes through windowing, quantization,
and the sensor's own noise floor — none of which the closed-form ground-
truth manifest math models. A tolerance chosen before ever running against
real hardware would be a guess in both directions: too tight and it flags
real-world effects that aren't bugs, too loose and it validates nothing.
The tool instead prints actual-vs-expected with `%delta` and defers
tolerance calibration to the first real run's numbers.

## Consequences

**Positive:**
- Unblocks real DSP-accuracy validation immediately, using equipment
  already on hand, rather than waiting on procurement.
- The mic channel gets full, unscoped validation — a laptop speaker has no
  real limitation there.
- Naming the accelerometer channel's ~20–150 Hz scope explicitly (rather
  than silently under-testing and calling it done) means a future reader
  knows exactly what was and wasn't validated, and why.
- Tolerance-band calibration from real data avoids a meaningless guessed
  threshold that either cries wolf on legitimate noise-floor effects or
  silently passes a real regression.

**Negative / trade-offs:**
- Accelerometer-channel validation does not cover the frequency range most
  bearing-fault energy actually occupies (BPFO/BPFI/BSF for the bearings in
  `COMMON_BEARINGS` at typical shaft speeds routinely land well above 150
  Hz) — this phase validates the DSP pipeline's correctness on a real
  captured signal, not fault-relevant frequencies. Phase 2's fault-detection
  accuracy work will need either better excitation hardware or an explicit
  decision to validate fault detection via synthetic-signal injection
  instead of physical excitation.
- Air-coupled (mic) and contact-coupled (accelerometer) excitation are
  physically different mechanisms; results from one channel don't
  generalize to a claim about the other.
- `capture_and_compare.py` prints numbers a human must currently judge by
  eye — no automated pass/fail gate exists yet for CI or a repeatable
  go/no-go bench check. Expected to follow once real numbers establish a
  reasonable tolerance.

## Validation

Exercised locally end-to-end before any hardware run: `generate_and_play.py
tone`/`fault` both synthesize, render (`--no-play --out`), and write a
manifest correctly (verified by inspecting the written WAV and JSON
manifest content directly); `capture_and_compare.py`'s report/diff logic
(`find_peak_bin`, `pct_delta`, `print_report`) was exercised against a
hand-built synthetic `DecodedFrame` (not a live MQTT capture) to confirm
the peak-bin frequency conversion and `%delta` table format are correct,
including the zero-expected-value case (`skewness`) falling back to an
absolute delta instead of a division by zero. `test_bench_signal_gen.py`
covers `ideal_sine_stats()` and `select_defect_frequency()` as pure-logic
unit tests. The actual real-hardware bench run (mic tone sweep +
accelerometer coupling test) is the next step this ADR unblocks, not yet
performed at the time this ADR was written.
