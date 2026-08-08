# tools/bench_signal_gen

Bench signal-generator + capture-compare tool for validating the satellite's
on-device DSP pipeline (FFT peak location, RMS/kurtosis/crest_factor/
skewness) against known-good closed-form or bearing-math ground truth. See
`docs/decisions/ADR-035-bench-signal-gen-laptop-speaker.md` for why a laptop
speaker is the excitation source and what that limits.

This is bench tooling — it runs on a technician's laptop, not on the
gateway or the satellite itself, and is not installed by CI (see
`requirements.txt`).

Phase 1 (this drop): `tone` mode + `capture_and_compare.py`, for mic-channel
DSP-accuracy validation. `fault` mode's synthesis/manifest plumbing is built
now (it shares everything with `tone`) but its actual bench use — comparing
against a satellite fault-classifier's output — is Phase 2 scope.

## Two tools

- **`generate_and_play.py`** — synthesizes and plays a signal through the
  laptop's speaker, and writes a ground-truth JSON manifest
  (`manifests/<mode>_..._<timestamp>.json`, gitignored) of the values a
  correct DSP pipeline should produce.
- **`capture_and_compare.py`** — subscribes to the satellite's live MQTT
  data topic (`epm/<node_id>/data`), captures frames for a window, and
  prints the satellite's actual computed values against the manifest's
  expected values with `%delta`. No pass/fail tolerance is applied yet —
  see "Why no tolerance band" below.

## Bench procedure — mic channel

1. Have the satellite running and connected to the same MQTT broker your
   laptop can reach.
2. Position a speaker (built-in laptop speaker is fine) at a fixed, measured
   distance from the satellite's mic — write the distance and speaker
   volume down, since neither is captured by the tool and both affect the
   scalar magnitudes (though not the FFT peak frequency).
3. Play a tone and let it finish:
   ```
   python generate_and_play.py tone --freq 1000 --duration 5 --amplitude 0.8
   ```
   This writes a manifest to `manifests/`.
4. Immediately after, capture and compare against that manifest:
   ```
   python capture_and_compare.py --manifest manifests/tone_1000hz_<timestamp>.json --channel mic --host <broker-ip> --window-s 8
   ```
5. Repeat at a few frequencies landing in different FFT bins (e.g. 200 Hz,
   1 kHz, 4 kHz) to check peak-bin accuracy isn't a coincidence of one
   lucky bin alignment.

## Bench procedure — accelerometer channel

A bare laptop speaker has limited cone excursion and negligible output
below ~20 Hz, so this channel is scoped honestly to **roughly 20–150 Hz** —
well below where most bearing-fault energy actually lives, but enough to
validate the DSP pipeline's peak-detection and scalar math on a real
captured signal rather than only synthetic bytes.

1. Rest the satellite board's enclosure directly against the speaker cone
   (or a subwoofer, if available — subwoofers extend the usable low end).
   Physical contact matters here, not just proximity — accelerometers
   couple through contact vibration, not air pressure the way a mic does.
2. Play a low-frequency tone within the ~20–150 Hz range:
   ```
   python generate_and_play.py tone --freq 60 --duration 5 --amplitude 1.0
   ```
3. Capture against the accel axis facing the speaker (commonly `accel_z`
   if the board sits flat on the cone):
   ```
   python capture_and_compare.py --manifest manifests/tone_60hz_<timestamp>.json --channel accel_z --host <broker-ip> --window-s 8
   ```

A cheap vibration motor or bass-shaker transducer (bolted or clamped
directly to the enclosure) would remove the ~20–150 Hz ceiling and the
air-coupling-vs-contact-coupling mismatch entirely — worth adding later if
accelerometer-channel bench validation becomes a recurring need, but out of
scope for this first pass (see ADR-035).

## `fault` mode (Phase 2 plumbing, built now)

```
python generate_and_play.py fault --bearing 6205 --shaft-hz 25 --defect bpfo --mod-depth 0.5 --duration 5
```

Synthesizes a shaft-rate carrier AM-modulated by the selected bearing
defect frequency (computed via `gateway.pipeline.bearing_math`) — the
standard bearing-fault vibration model. The manifest records the
bearing_math-computed target frequency plus the actual synthesized
carrier/sideband frequencies. There's no closed-form RMS/kurtosis/
crest_factor/skewness ground truth for an AM signal the way there is for a
pure sine, so `capture_and_compare.py` only diffs peak-bin frequency in
this mode.

## Why no tolerance band

`capture_and_compare.py` prints actual-vs-expected and `%delta` without
judging pass/fail. A real capture goes through windowing, quantization, and
the mic/accelerometer's own noise floor — none of which the closed-form
manifest math models — so a tolerance guessed before ever running against
real hardware would be meaningless either way: too tight and everything
"fails" on real-world effects that aren't bugs, too loose and it catches
nothing. The first real bench run's numbers inform what a reasonable
tolerance actually is.

## Options reference

`generate_and_play.py tone`: `--freq` (Hz), `--duration` (s), `--amplitude`
(0-1, default 0.8), `--sample-rate` (default 48000), `--out` (optional WAV
save path), `--no-play` (render/save only).

`generate_and_play.py fault`: `--bearing` (key into
`gateway.pipeline.bearing_math.COMMON_BEARINGS`, e.g. `6205`, or
`n,D,d[,alpha]`), `--shaft-hz`, `--defect {bpfo,bpfi,bsf,ftf}`,
`--mod-depth` (0-1, default 0.5), `--duration`, `--amplitude` (default 0.8),
plus the same `--sample-rate`/`--out`/`--no-play` as `tone`.

`capture_and_compare.py`: `--manifest` (required), `--channel {mic,accel_x,
accel_y,accel_z}` (required), `--host` (default `localhost`), `--port`
(default `1883`), `--node-id` (default: first sender seen), `--window-s`
(default `8`).

## Tests

`test_bench_signal_gen.py` covers the two pure-logic pieces
(`ideal_sine_stats()` and `select_defect_frequency()`) with no audio/
hardware/MQTT dependency. Not wired into the repo's root `tests/` pytest
suite / CI — run it manually:
```
python -m pytest tools/bench_signal_gen/test_bench_signal_gen.py -v
```
