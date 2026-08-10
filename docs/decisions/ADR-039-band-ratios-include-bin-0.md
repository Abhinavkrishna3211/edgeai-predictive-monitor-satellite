---
id: ADR-039
title: "_band_ratios() includes bin 0 -- accepted tradeoff: more mains-hum false positives for real low-shaft-speed-fault recall"
status: accepted
date: 2026-08-10
deciders: Abhinav Krishna N
---

## Context

`_band_ratios()` (`gateway/pipeline/alerting.py`) has excluded wire bin 0 from
every band-ratio computation (`total`, `lo_r`) since the very first commit
(`f30011c`), as generic "skip DC" hygiene. At that commit the mic FFT array
had 512 bins (15.625 Hz/bin) -- excluding bin 0 discarded a negligible sliver
of spectrum. `EPM_MODEL_SPECTRUM_BINS` was later coarsened to 128
(`ADR-020`), and `epm_dsp_reduce_bins()` pools 4 native 512-bin-resolution
bins into each wire bin (`EPM_MIC_SPECTRUM_BAND = 512/128 = 4`,
`src/epm_config.h`), making wire bin 0 span 0-187.5 Hz at `MIC_FS_HZ=48000`.
The exclusion was never revisited when the resolution changed -- no ADR, no
comment, no test ever discussed it -- and by the time of Phase B's accuracy
harness it silently discarded the entire 0-187.5 Hz range from `hi_r`/`lo_r`/
`mid_r`, `_classify_fault_type()`, `_fault_candidate_scores()`, HST's
spectral-centroid feature, and `bearing_corroboration.py`'s peak search. This
range is exactly the low-shaft-speed band this device's bench rig targets
(default bearing/imbalance manifests use shaft_hz=25.0, i.e. defect
frequencies in the tens-to-low-hundreds of Hz).

**Was bin 0 truly dead, or genuinely blind?** Firmware detail
(`src/epm_config.h:242`) hardcodes native `fft_db[0]` (the true DC bin) to
-120 dBFS after DC removal -- but wire bin 0 pools 4 native bins, so only 1
of the 4 is this fixed floor; the other 3 carry real spectral content. An
80 Hz-tone multi-frame averaged leakage check (ambient vs. tone-playing,
several dozen frames per condition via `FrameCapture`) found wire-bin-1
deltas inconsistent across repeated runs (+7.7 dB single-frame, +1.64 dB
first multi-frame average, ~+1 dB and not statistically significant under a
Welch's t-test in a third run, with the ambient baseline itself drifting
~6 dB between captures 15 seconds apart) -- this rig's fan noise dominates
at 80 Hz strongly enough that a clean leakage measurement was never
obtained. The measurement was inconclusive on its own. The decision to fix
rests on the independent, non-noisy architectural argument: bin 0 pools real
(non-floored) content at the current resolution regardless of what any one
noisy bench measurement shows, and the safety-relevant question is not "is
there leakage today" but "can a real low-shaft-speed fault's energy ever
land in this bin" -- which it demonstrably can.

## Decision

Include bin 0 in `_band_ratios()`'s `total` and `lo_r` sums, matching every
other bin. Applied consistently across all independent duplicates of this
logic (`gateway/pipeline/alerting.py::_band_ratios`,
`gateway/pipeline/bearing_corroboration.py`'s dominant-peak search,
`tools/bench_signal_gen/capture_and_compare.py::band_ratios_from_wire_bins`,
`tools/bench_signal_gen/generate_and_play.py::band_ratios_from_samples`).
`mic_tools/sim_sweep.py` carries its own, fifth, independent copy of this
logic but at a stale 512-bin/15.625 Hz-per-bin resolution unrelated to the
current 128-bin wire format -- left untouched as out of scope; it is a
standalone simulation tool (kept separate from the live pipeline per
`ADR-030`'s precedent), not exercised by real captures or the accuracy
harness this decision is scoped to.

This is an explicit, accepted tradeoff, not an oversight: **more mains-hum
and ambient-low-frequency-noise-driven false positives, in exchange for
recall on real low-shaft-speed mechanical faults**, per the project's
standing "never miss a real fault" priority. A classifier that cannot see
0-187.5 Hz can never flag a genuine low-RPM imbalance or looseness fault
whose signature lives entirely in that band, no matter how well-tuned its
other thresholds are -- that failure mode is worse than an elevated false
positive rate that a human or a persistence/hysteresis layer can absorb.

## Consequences (measured, real-rig)

**Part 1 (classifier confusion matrix, method: injected) -- unaffected.**
`classify_eval.py` scores `_classify_fault_type()` directly against
synthetic `hi_r`/`lo_r`/`mid_r` tuples; it never calls `_band_ratios()`, so
this fix cannot and does not change its output (`out/classify_confusion.json`
byte-identical before/after). This is expected, not a validation gap in
this specific evaluation -- Part 1 tests the classifier's branch/scoring
logic in isolation from the FFT-to-band-ratio conversion this ADR changes.

**Part 3 (real-rig Normal baseline, method: real-rig) -- FPR regresses
sharply, exactly as predicted.** A fresh ~0.17-hour (2,977-frame) ambient
capture taken immediately after this fix landed (gateway restarted to load
the new code; capture window ends before any Task 3 tone playback began, so
it is uncontaminated) measured:

- **FPR = 0.8310** (up from **0.2001** on the equivalent fresh capture taken
  right after the IMU_CREST_WARN split, pre-bin-0-fix -- see commit
  `702d9bd`). Alert breakdown: `FAULT=2259 (75.9%), WARN=215 (7.2%),
  OK=503 (16.9%)`.
- **Not driven by IMU or mic crest thresholds directly**: 0.0% of frames
  exceed `IMU_CREST_WARN=9.0` (median `imu_crest=4.584`); `mic_crest`
  median is 3.120, comfortably under `CREST_WARN=5.0` (though its p95=16.47
  and max=31.9 show a real heavy tail from ordinary room transients).
  `mic_kurtosis` has an even heavier tail (p95=104.3, max=1009.6). The
  mechanism is bin 0 now injecting real ambient low-frequency content (mains
  hum, HVAC, room rumble) into `lo_r` on effectively every frame, which
  combines with these already-present crest/kurtosis transients to satisfy
  the Mechanical Imbalance/Looseness gates in `_fault_candidate_scores()`
  far more often than when that content was invisible.
- The breakdown is FAULT-heavy rather than WARN-heavy, unlike the original
  135k-frame imu_crest-driven FPR=0.8568 baseline (which was WARN-dominant,
  `WARN=88974, FAULT=26715`). This suggests a sustained per-frame shift
  (consistent ambient low-frequency content) rather than intermittent
  spikes, escalating through `WARN_PERSIST`/`FAULT_CLEAR_PERSIST` hysteresis
  to FAULT on most frames. Not fully traced further in this pass -- flagged
  here as a concrete follow-up (persistence/hysteresis tuning, or a
  dedicated mains-hum notch/floor for `lo_r`) rather than re-litigated
  under this decision's scope.

**Real-rig fault-type captures (Task 3, method: real-rig) -- mixed, rig-
limited results, not classifier failures.** Four low-frequency (resonance/
carrier ~40-85 Hz, deliberately placed inside the previously-blind band)
bearing/imbalance-safe/imbalance-threshold/looseness probes were played
through the bench rig's laptop speaker and captured via mic. All four
showed real captured `rms` 99.0-99.6% below their closed-form expected
value and a captured spectral peak at 656.25 Hz regardless of the
signal actually played (80 Hz bearing/imbalance carriers, or 40/60/85 Hz
looseness carriers) -- this rig's speaker/mic acoustic path cannot
reproduce content in the 40-150 Hz range at usable amplitude (consistent
with the fan-noise SNR problem already found during the leakage check).
None of the four landed on their eponymous label post-fix (`Anomalous
Vibration`, `Mechanical Looseness` x2, and `Normal` for the by-design-safe
preset) -- an honest limitation of this specific rig's acoustic frequency
response, not evidence against the bin-0 fix or the classifier logic.

A **canonical bearing signal at default resonance_hz=3500 Hz**, by
contrast, was captured faithfully on the **mic** channel: peak_bin_freq_hz
within 4.5% of expected (3656 Hz vs. 3500 Hz), `hi_r` within 2.25% of
ground truth (0.974 vs. 0.997), and correctly classified as **"Bearing
Fault -- Early"** -- the first real-hardware match of a bearing-fault label
in this project's history (severity read "Early" rather than the
ground-truth "Advanced": real captured kurtosis is damped relative to the
idealized synthetic burst, plausible given microphone/speaker dynamics).
The same signal captured on the **accel_z** channel showed a peak at
150 Hz, not 3500 Hz -- the accelerometer's mechanical coupling path does
not transmit this rig's 3500 Hz acoustic content at all, corroborating the
2026-08-09 accel characterization finding of a real-rig mechanical ceiling
around 90 Hz (resonance, not sensor limit) from an independent angle: the
mic/acoustic path and the accelerometer/mechanical path have different and
non-overlapping frequency ceilings on this specific bench rig (mic fails
low, ~40-150 Hz; accel fails high, above ~150 Hz).

## Not addressed by this decision

- The FAULT-heavy persistence/hysteresis escalation pattern noted above.
- Tuning `lo_r`'s 0.45/0.55 gate thresholds to compensate for the new
  mains-hum contribution -- left as measured, not re-tuned, so the
  real-rig numbers above reflect the fix in isolation.
- This bench rig's acoustic (<150 Hz) and mechanical (>150 Hz) frequency
  ceilings -- physical rig limitations, not software.
