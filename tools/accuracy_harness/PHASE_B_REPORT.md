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
