# Phase B accuracy harness — final report

Synthesizes Parts 1-3. Each finding below is labeled with its evaluation method
(`injected` or `real-rig`) per the scoping instruction to never blend results across
method boundaries. Full detail, raw tuples, and generated artifacts live under
`tools/accuracy_harness/out/`; per-part methodology is in each part's own doc
(`classify_confusion.md`, `anomaly_pr_eval.md`, `real_rig_baseline_runbook.md`).

No pre-existing "scoping report" document was found anywhere in the repo (searched
`docs/`, all ADRs, `PROJECT_STATUS.md`) — this report and the approved plan are the
methodology spec of record for this work.

## Part 1 — Classifier confusion matrix (method: injected)

`tools/accuracy_harness/classify_eval.py` + `synth_frames.py`. 57 boundary/zone
tuples scored directly through `_classify_fault_type()`
(`gateway/pipeline/alerting.py:98-134`), no hardware involved.

- All 9 labels score **1.000 precision/recall/F1** on deep-inside tuples (19 tuples,
  the only ones counted toward the accuracy metric — boundary/just-outside tuples are
  diagnostic only, by construction expected to disagree).
- **Real bug found**: 2/2 dual-satisfaction probes confirmed a genuine branch-order
  priority collision — `_classify_fault_type()`'s `if/elif` chain means "Bearing
  Fault" (branch 2) unconditionally wins over "Mechanical Imbalance" and "Shaft
  Misalignment" whenever a frame's band ratios deep-inside-satisfy both condition
  sets simultaneously, even though the intended label was the other one. This is a
  latent bug: a frame with a genuine imbalance *and* an incidental bearing-like
  spectral signature will always be reported as a bearing fault, silently
  suppressing the imbalance label. Not fixed in this pass (`_classify_fault_type()`
  is explicitly out of scope for restructuring per the task's own instructions) —
  flagged here for a future branch-order or priority-scoring redesign.
- Bearing-vs-Looseness dual satisfaction is structurally impossible (`hi_r>0.40` vs.
  `hi_r<0.30` are mutually exclusive), so no third probe was needed there.
- 20 `boundary_artifact` mismatches logged, all expected fallback behavior at exact
  thresholds — not bugs.
- Branches 6/7 ("Severe Anomaly — Inspect", "Elevated Vibration", the kurtosis-only
  fallback branches) confirmed reachable and intentional, not dead code.

## Part 2 — Anomaly-score PR curve (method: injected)

`tools/accuracy_harness/anomaly_pr_eval.py`. Healthy: 300 train + 80 test jittered
Normal-zone tuples. Anomalous: 120 samples (15 severity-jittered variants x 8
non-Normal labels), scored through a synthetic mic-FFT reconstruction that keeps
`hi_r/lo_r/mid_r` consistent with Part 1's tuples so the autoencoder's spectral
bands, HST's spectral centroid, and the classifier's band ratios all see the same
underlying signal.

- **HST (production config, `n_features=7, n_trees=10`): AUC-PR = 1.000.** HST has no
  fixed `t_warn`/`t_fault` in production (feeds Bayesian fusion only) — the threshold
  sweep reported is diagnostic, not a production gate.
- **Autoencoder: AUC-PR = 1.000, but PROXY ONLY** — TensorFlow is not installed in
  this environment, so the real reconstruction-MSE model never ran. The reported
  score is a Mahalanobis-distance proxy over the 41-dim `make_feature_vector()`
  output and must not be quoted as production autoencoder accuracy.
- **Caveat carried into this report**: both AUC-PR figures are near-ceiling by
  construction — severity-jittered anomalies are built 20*delta+ past their
  defining threshold, then scaled up to 1.0-3.0x further. This measures monotonic
  response to synthetic severity, not real-world detection margin on borderline
  faults. Part 3 is the only real-hardware measurement in this report.

## Part 3 — Real-rig Normal baseline (method: real-rig)

`tools/accuracy_harness/rig_baseline_report.py` + `real_rig_baseline_runbook.md`.
135,032 real frames captured over ~9.46 hours via the live satellite -> MQTT ->
`_process_satellite_frame()` pipeline, ambient/no-injected-fault conditions (the
originally planned clean-tone stimulus was blocked by an environment memory
allocation issue in `bench_signal_gen` — documented in the runbook, not fixed, judged
unsafe to fix autonomously).

- **FPR = 0.8568, recall_normal = 0.1432** (n=135,032). Alert breakdown:
  `WARN=88974, FAULT=26715, OK=19343`.
- **Root cause, confirmed real**: median `imu_crest` across the capture is 5.233,
  already above the default `CREST_WARN=5.0` threshold; 72.06% of individual frames
  have `imu_crest >= 5.0` from ambient vibration alone on this physical rig (laptop
  resting on the accelerometer). `mic_crest` stays comfortably under threshold
  throughout (p50=3.09) — the mic channel is not the driver.
- precision/F1 are not computable from this Normal-only baseline (no fault frames
  exist by construction) — reported as `n/a`, not fabricated.
- This adds live-hardware confirmation to the simulation-derived parameter
  recommendations already on record (2026-06-30 sweep: `n_trees 25->10`, `z_mid
  3->2`, `alpha 0.0005->5e-05`) — this is the first real-hardware evidence that
  default IMU crest-factor threshold tuning, or physical decoupling of the
  accelerometer mount from ambient vibration, deserves attention before this rig can
  produce a clean Normal baseline.

## Fault categories that remain injected-data-only

Per the task's explicit scoping (`bench_signal_gen` extension for impulsive/broadband
synthesis was declared out of scope), only "Normal" has ever been measured against
real hardware (Part 3). All 8 fault labels — Bearing Fault (Early/Advanced),
Mechanical Imbalance, Shaft Misalignment, Mechanical Looseness, Severe Anomaly —
Inspect, Elevated Vibration, Anomalous Vibration — have accuracy figures from Part 1
(classifier logic) and Part 2 (anomaly-score separability) only, both `method:
injected`. No real bearing fault, imbalance, misalignment, or looseness signature has
ever been run through this pipeline on real hardware. This is the single biggest gap
in this accuracy harness and should be the top priority for any future real-rig
expansion (e.g. an actual unbalanced/misaligned test rig, or resolving the
bench_signal_gen environment blocker so injected tones can be played instead of only
synthesized as FFT arrays).

## Known environment limitations affecting this report

- TensorFlow/tflite_runtime not installed — autoencoder path in Part 2 is proxy-only.
- `bench_signal_gen` blocked by a Windows pagefile/commit-limit `MemoryError` on even
  small numpy allocations — not fixed (out of scope for autonomous system-level
  changes); Part 3 substituted ambient capture instead of a synthetic tone.
- scipy/scikit-learn import flakiness noted in the approved plan as a transient
  environment issue — did not recur during this session's Parts 1-3 work.

## Addendum — 2026-08-10: fault-classifier scoring fix, IMU/mic crest split, bin-0 inclusion, first real-hardware fault captures

This addendum covers four pieces of follow-on work, each `method`-labeled per this
report's convention.

### Fault-classifier priority-collision fix (method: injected)

The branch-order priority collision flagged in Part 1 above (`_classify_fault_type()`'s
`if/elif` chain always resolving dual-satisfaction frames to "Bearing Fault") is fixed.
`_classify_fault_type()` now scores every category whose gate is satisfied by average
relative margin past its threshold via `_fault_candidate_scores()`, and the strongest
match wins, rather than the first branch tested. Commit `9759663`.

### IMU/mic crest-factor threshold split (method: real-rig)

Part 3's root cause (median `imu_crest=5.233`, already above the shared
`CREST_WARN=5.0` threshold, from ordinary laptop-on-accelerometer ambient vibration —
see Part 3 above) is fixed by giving the IMU channel its own thresholds
(`IMU_CREST_WARN=9.0`/`IMU_CREST_FAULT=18.0`) separate from the mic channel's
(`CREST_WARN=5.0`/`CREST_FAULT=10.0`). Commits `84ee922`, `9759663`. Confirmed on a
fresh real-rig capture: **FPR 0.8568 → 0.2001** (commit `702d9bd`). This is the
FPR baseline the bin-0 fix below regresses from.

### Task 1 — bin-0 spectral exclusion (method: injected leakage check + real-rig)

`_band_ratios()` and its three duplicates excluded wire bin 0 from every band-ratio
computation as stale "skip DC" hygiene inherited from a 512-bin resolution; at the
current 128-bin resolution bin 0 spans 0-187.5 Hz and pools 3 real (non-floored)
native bins alongside the true DC bin. An 80 Hz-tone multi-frame-averaged leakage
check was inconclusive on its own (this rig's fan noise dominates at 80 Hz enough
that no run produced a statistically clean before/after delta), so the fix rests
on the independent architectural argument instead: a classifier blind to 0-187.5 Hz
can never see a genuine low-shaft-speed fault whose signature lives entirely in that
band. **Fixed**, with explicit sign-off, across `alerting.py`, `bearing_corroboration.py`,
`capture_and_compare.py`, and `generate_and_play.py` (`mic_tools/sim_sweep.py`'s
independent, stale-resolution copy left untouched as out of scope). Full rationale,
the leakage-check numbers, and the measured consequences below are in **ADR-039**.

- **Part 1 re-run: no change.** `classify_eval.py` scores `_classify_fault_type()`
  directly against synthetic `hi_r`/`lo_r`/`mid_r` tuples and never calls
  `_band_ratios()`, so this fix is architecturally insulated from Part 1's confusion
  matrix (`out/classify_confusion.json` byte-identical before/after). Expected, not a
  gap in this specific evaluation.
- **Part 3 re-run: FPR regresses 0.2001 → 0.8310** on a fresh, uncontaminated
  2,977-frame ambient capture taken right after the fix landed. Not driven by
  `imu_crest` (0.0% of frames exceed `IMU_CREST_WARN=9.0`, median 4.584) or by
  `mic_crest`'s median (3.120, under `CREST_WARN=5.0`) — the mechanism is bin 0 now
  carrying real ambient low-frequency content (mains hum, HVAC, room rumble) into
  `lo_r` on effectively every frame, combined with `mic_crest`/`mic_kurtosis`'s
  already-present heavy tails (p95=16.47/104.3). This is the explicit, accepted
  tradeoff instructed for this fix: more mains-hum-driven false positives in
  exchange for recall on real low-shaft-speed faults, now measured rather than
  hypothesized. See ADR-039 for the full breakdown, including the FAULT-heavy (not
  WARN-heavy) alert-persistence pattern flagged there as an open follow-up.

### Task 2 — manifest ground-truth sanity check (method: injected)

All 4 staged fault manifests (bearing/imbalance-safe/imbalance-threshold/looseness)
were regenerated post-bin0-fix via `generate_and_play.py --no-play` and their
`expected` blocks compared against the closed-form docstring claims in
`band_ratios_from_samples()` and the manifest generator functions; all matched. This
regeneration also surfaced that the bin-0 fix moves every manifest's `lo_r` sharply
upward (e.g. ~0.43-0.55 → ~0.80-0.97), which is what motivated the dual LF/canonical
real-rig test design in Task 3 below.

### Task 3 — first real-hardware fault captures (method: real-rig)

Two manifest generations were captured against the live satellite → MQTT →
`_process_satellite_frame()` pipeline: four **LF-80Hz** probes (resonance/carrier
frequencies deliberately placed in 40-85 Hz, inside the range bin-0 exclusion used to
hide) on the mic channel, and one **canonical** (default resonance_hz=3500) bearing
signal on both the mic and accel_z channels separately, to distinguish an acoustic-path
limitation from a mechanical-path one.

- **LF-80Hz probes (mic): rig-limited, not classifier bugs.** All four showed captured
  `rms` 99.0-99.6% below the closed-form expected value and a captured spectral peak
  locked at 656.25 Hz regardless of which signal was actually played — this rig's
  speaker/mic acoustic path cannot reproduce usable amplitude in the 40-150 Hz range,
  consistent with the fan-noise SNR problem found during Task 1's leakage check. None
  of the four landed on their eponymous label; the genuine post-fix labels are reported
  as-is (not reframed as failures) per instruction: `Anomalous Vibration`, `Mechanical
  Looseness` (x2), and `Normal` for the by-design-safe imbalance preset. This is honest
  evidence that this specific rig's acoustic test method needs a different signal
  design for sub-150 Hz probes — not evidence against the bin-0 fix or the classifier.
- **Canonical bearing signal (mic): first real-hardware fault-label match in this
  project's history.** peak_bin_freq_hz landed within 4.5% of expected (3656 Hz vs.
  3500 Hz), `hi_r` within 2.25% of ground truth (0.974 vs. 0.997), and the frame
  classified as **"Bearing Fault — Early"** (ground truth: "Advanced" — real captured
  kurtosis is damped relative to the idealized synthetic burst, plausible given
  microphone/speaker dynamics).
- **Canonical bearing signal (accel_z): does not couple.** The same signal captured on
  the accelerometer channel peaked at 150 Hz, not 3500 Hz — this rig's mechanical
  coupling path does not transmit 3500 Hz acoustic content at all. Combined with the
  mic result above, this is a stronger and more useful finding than either channel
  alone: the acoustic and mechanical paths on this bench rig have distinct,
  non-overlapping frequency ceilings (mic fails below ~150 Hz, accel fails above
  ~150 Hz, consistent with the 2026-08-09 accel characterization's ~90 Hz mechanical
  resonance ceiling) rather than the whole rig being limited in one direction.

This changes the "Fault categories that remain injected-data-only" gap noted earlier
in this report: **Bearing Fault is no longer injected-data-only** — it has one
genuine real-hardware confirmation (mic, canonical frequency). The other three fault
labels (Mechanical Imbalance, Mechanical Looseness, Shaft Misalignment) remain
injected-data-only; this rig's demonstrated acoustic ceiling (~150 Hz) means a future
real-hardware attempt at those labels needs either a higher-frequency signal design
or a non-acoustic stimulus (direct rig excitation), not just a re-run of the same
LF-tuned probes.
