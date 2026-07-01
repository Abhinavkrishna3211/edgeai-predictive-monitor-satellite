# Known Issues

Deferred weak points from the Phase 8 audit. Items below were NOT fixed in Phase 9
because they require sweep data, hardware, or cross-component changes that exceed the
scope of a single-file patch.

For items that were fixed, see git blame on `mic_tools/recv_verify.py` (WP-01, WP-04,
WP-06 applied 2026-06-28).

---

## WP-02 — HIGH_BAND_MIN threshold has no sweep-derived justification

**Severity**: HIGH
**Effort**: Small (code), Medium (measurement — requires sweep before changing value)
**Status**: Deferred — threshold value needs empirical sweep before change

**Background**: `HIGH_BAND_MIN=0.12` suppresses fault alerts when the high-band energy
ratio is below 12%. For machines in high-ambient low-frequency noise environments,
genuine bearing faults can have hb < 0.12, silently suppressing correct detections.

**Why deferred**: Lowering to 0.06 (the suggested value) without a sweep would change
a safety-critical threshold without measured justification. The sim harness
(`mic_tools/sim_sweep.py`) does not yet include a HIGH_BAND_MIN sweep. Until that
sweep is run and measured FP/FN tradeoffs are documented, the default stays at 0.12.

**Resolution path**: Add `phase9_hb_min_sweep()` to `sim_sweep.py`, sweep
`HIGH_BAND_MIN` in {0.04, 0.06, 0.08, 0.10, 0.12}, measure fp_rate and recall across
factory-noise and low-frequency-ambient scenarios. Pick the value that minimises
FN rate without raising FP rate above 2%. Update `epm_config.h` and `recv_verify.py`.

---

## WP-03 — CAL_FRAMES is frame-count based, not time-based

**Severity**: MEDIUM
**Effort**: Small
**Status**: Deferred — cross-component change (Python + firmware header)

**Background**: `CAL_FRAMES=30` is a raw frame count. At the standard 2.2 fps this is
13.6 s — acceptable. But if `SPEC_AVG_N` changes, the frame rate changes and
calibration duration changes non-obviously. At `SPEC_AVG_N=1` (4.4 fps), calibration
completes in 6.8 s with only 30 Welford samples, which may yield unstable variance.

**Resolution path**: Add a `CAL_MIN_SECONDS = 15.0` constant and compute
`CAL_FRAMES = max(30, int(CAL_MIN_SECONDS * effective_fps))` at startup in
`recv_verify.py`. Add a startup-time warning if resolved `CAL_FRAMES < 20`.
Mirror the constant in `epm_config.h` for firmware alignment.

---

## WP-05 — ADWIN drift-detection delta not derived from actual score variance

**Severity**: MEDIUM
**Effort**: Small
**Status**: Deferred — requires measured HST score distribution from Phase 1

**Background**: `delta=0.002` was taken from library documentation, not derived from
this system's actual HST score variance. With healthy score sigma~0.03 (from Phase 1
baseline), the theoretically appropriate delta for a 1000-frame false-alarm period is
`delta = sigma^2 / period = 0.0009/1000 = 9e-7` — three orders of magnitude smaller.
However, ADWIN's sensitivity also depends on window size and sequence autocorrelation;
the formula above is an approximation.

**Resolution path**: Instrument `OnlineDetector` to log all healthy-phase HST scores.
Collect 10,000 healthy frames across 3 distinct seeds. Compute `score_mean` and
`score_std`. Use `delta_recommended = score_std^2 / desired_false_alarm_frames`
with `desired_false_alarm_frames = 2000`. Document in ADR-004 Section 6. Note: this
may find that `delta=0.002` is already conservative (high false-alarm avoidance) and
no change is needed.

---

## WP-07 — Per-satellite state reads in dashboard handler lack consistent locking

**Severity**: MEDIUM
**Effort**: Small
**Status**: Deferred — thread-safety audit across dashboard/alert logger needed

**Background**: Under CPython's GIL, individual attribute reads are atomic, but compound
reads (`sat.bl_mean` + `sat.bl_std` as a pair) are not protected. The `_sat_models_lock`
guards the model dictionary but not per-satellite field reads in the HTTP handler. A torn
read could display a momentarily inconsistent z-score on the dashboard.

**Impact**: Cosmetic only — dashboard may briefly show a bogus z-score. No alert logic
corruption (alerts are computed in the satellite thread with full context). No SQLite
corruption (writes use WAL and are serialised).

**Resolution path**: Add `sat._display_lock = threading.Lock()` to `SatelliteState`.
Acquire it in the dashboard handler before reading composite fields
(`bl_mean, bl_std, last_alert, rul_result`). Release after reading.
Also acquire during the EMA update in `satellite_thread` for those same fields.
Full audit required to identify all read sites.

---

## WP-08 — Fault-model resonance parameters are not calibrated against real sensor data

**Severity**: MEDIUM (simulation quality)
**Effort**: Large
**Status**: Deferred — requires hardware test rig (6205 bearing + KX134/INMP441 sensors)

**Background**: `fault_models.py` places a broadband resonance bump at 4000 Hz with
sigma=1500 Hz. These values are uncited. Real structural resonances for 6205 bearings
in typical housings span 2-10 kHz depending on geometry; the simulation's 4 kHz centre
may under- or over-represent the high-band energy ratio relative to actual sensors.

**Impact**: If real resonances cluster near 2.5 kHz (below the 4 kHz assumed), the
`HIGH_BAND_MIN=0.12` threshold may be too aggressive on real hardware. If above 5 kHz,
it may be too lenient.

**Resolution path**: Collect controlled recordings: (1) healthy motor at rated load,
(2) bearing with seeded outer-race fault, using KX134 accelerometer and INMP441
microphone on an actual 6205 bearing test rig. Fit a Gaussian to the resonance peak in
averaged fault spectra. Update `fault_models.py` with measured `resonance_hz` and
`resonance_sigma_hz`. Rerun Phase 1-7 sweep suite with calibrated model.

---

## WP-09 — HST clip to [0,1] may suppress extreme-kurtosis fault scores

**Severity**: LOW
**Effort**: Small
**Status**: Deferred — low risk given K_FAULT=12 absolute guard catches extreme cases

**Background**: Welford normalisation clips features to `clip((z+3)/6, 0, 1)`. For
kurtosis values > 15 (z > 7 if sigma_k~2), the clip saturates at 1.0. HST trees
partitioned on [0,1] during healthy training may score extreme-fault features at the
boundary rather than interior, potentially lowering the anomaly score paradoxically.

**Impact**: In practice the absolute kurtosis threshold `K_FAULT=12` fires before
extreme z-scores cause confusion. The HST score is not the sole detection channel.
Estimated FN risk from this issue: <1% in current deployment envelope.

**Resolution path**: Change the normalisation for the kurtosis feature (feature index 0)
from `clip((z+3)/6, 0, 1)` to `clip((z+5)/10, 0, 1)` in `online_detector.py:59`.
This widens the effective range without changing the centre. Validate that healthy
score mean/variance are unchanged (Phase 1 re-run). If cohen_d improves, merge.
