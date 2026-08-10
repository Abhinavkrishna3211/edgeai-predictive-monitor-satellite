---
id: ADR-038
title: Bearing-frequency corroboration for spectral fault classification is additive, out-of-band evidence only
status: accepted
date: 2026-08-10
deciders: Abhinav Krishna N
---

## Context

`_classify_fault_type()` (`gateway/pipeline/alerting.py:98-134`) labels a
frame "Bearing Fault — Early/Advanced" from pure spectral-shape pattern
matching: high-band energy ratio (`hi_r > 0.40`) plus kurtosis crossing
`K_WARN`/`K_FAULT`. It has never been cross-checked against the actual
physics of bearing defects — `bearing_math.py`'s BPFO/BPFI/BSF/FTF formulas
exist in the codebase (used today only for FFT-panel annotation, via
`gateway/main.py`'s `--shaft-hz`/`--shaft-rpm --bearing` flags feeding
`run_plot()`) but were never wired into the live classification path at all.

The Phase B accuracy harness's Part 1 (`tools/accuracy_harness/PHASE_B_REPORT.md`)
found a genuine branch-order priority collision in `_classify_fault_type()`:
"Bearing Fault" (branch 2) unconditionally wins over "Mechanical Imbalance"
and "Shaft Misalignment" whenever a frame's band ratios deep-inside-satisfy
both condition sets simultaneously, because of `if/elif` short-circuit
order. That finding directly shapes this decision: `_classify_fault_type()`'s
branch logic is already fragile to collisions, so adding more responsibility
to it (e.g. a bearing-physics veto/override baked into the branch chain)
would compound an existing risk rather than mitigate it.

Investigation also confirmed no live shaft-speed estimate exists anywhere in
this pipeline. `--shaft-hz`/`--shaft-rpm`/`--bearing` resolve into **local**
variables (`shaft_hz`, `geom`, `bf` in `gateway/main.py`) consumed only by
`run_plot()` for FFT-panel annotation — they never reach
`recv_verify.py`/`_process_satellite_frame()`. Corroboration must therefore
accept shaft speed and geometry as explicit, externally-supplied parameters;
it cannot infer them.

## Decision

Add corroboration as a new, separate module —
`gateway/pipeline/bearing_corroboration.py`'s
`corroborate_bearing_fault(fault_type, mic_fft_db, fs_hz, shaft_hz, geom,
tolerance_hz=None)` — called **after** `_classify_fault_type()` has already
returned a label, never as an input to it. It:

1. No-ops (`return None`) if `fault_type` isn't a "Bearing Fault" label, or
   `shaft_hz`/`geom` are unavailable, or the mic FFT is missing/too short.
2. Otherwise finds the dominant non-DC spectral peak in the mic FFT and
   checks whether it lands within `tolerance_hz` (default: 2 FFT bins) of any
   `BearingFreqs.markers()` entry (BPFO/BPFI/BSF/FTF and their 2nd
   harmonics), returning a result dict with `corroborated`, `matched_marker`,
   `peak_hz`/`peak_db`, and the nearest marker's frequency/delta.

`_classify_fault_type()` and `bearing_math.py` are both untouched — this is
strictly additive annotation, matching ADR-032's precedent for additive,
non-blocking feature additions.

**Wiring** (`mic_tools/recv_verify.py`, right after the existing
`sat.fault_type = fault_type` assignment): if the label starts with "Bearing
Fault" and two new module-level globals, `rv._SHAFT_HZ`/`rv._BEARING_GEOM`,
are set, call `corroborate_bearing_fault()` and store the result on a new
`SatelliteState.bearing_corroboration` field (`None` by default).
`gateway/main.py` sets `rv._SHAFT_HZ`/`rv._BEARING_GEOM` from its existing
`shaft_hz`/`geom` locals immediately after they're resolved from
`--shaft-hz`/`--shaft-rpm --bearing` — this is the one-line gap that
previously kept CLI-supplied shaft speed from ever reaching live
classification, closed as a minimal, explicitly-scoped part of this change
rather than a separate decision.

## Consequences

- No behavior change for any deployment that doesn't pass `--shaft-hz`/
  `--shaft-rpm --bearing` — `_SHAFT_HZ`/`_BEARING_GEOM` default to `None`,
  `corroborate_bearing_fault()` returns `None` immediately, and
  `sat.bearing_corroboration` stays `None`.
- When shaft speed and geometry are supplied, a "Bearing Fault" label now
  carries a same-frame physics cross-check for free — useful for triage
  (dashboard/report consumers can show "corroborated" vs. "pattern-only")
  without any risk to the classifier's own branch logic.
- Does **not** fix the branch-order priority collision found in Part 1 — that
  remains open and is explicitly out of scope for this change; corroboration
  is an annotation, not a correction of the underlying label.
- No live shaft-speed estimator exists yet, so corroboration is opportunistic
  (only active when an operator supplies shaft speed via the CLI at
  startup) rather than always-on.

## Validation

`tests/pipeline/test_bearing_corroboration.py` (8 tests): dominant peak at
BPFO/BPFI both corroborate with the correct marker label; a peak far from
every marker does not corroborate; "Normal" and non-bearing fault-type labels
return `None`; missing `shaft_hz`/`geom`/`mic_fft` each return `None`. All
pass. `gateway/main.py` and `mic_tools/recv_verify.py` both confirmed to
still import cleanly after the wiring change.
