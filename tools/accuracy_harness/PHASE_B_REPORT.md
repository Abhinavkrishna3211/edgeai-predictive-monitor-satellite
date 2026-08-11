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

## Addendum — 2026-08-10 (part 2): FAULT-heavy mechanism correction, real mic clean-band characterization, real Looseness/Imbalance captures

Follow-on to the addendum above. Three pieces of work, each `method`-labeled per this
report's convention. `bench_signal_gen`'s Windows pagefile blocker noted above is
resolved as of this session (tone/burst synthesis ran directly against the live rig
throughout).

### Task 1 — ADR-039 FAULT-heavy pattern, mechanism correction (method: injected leakage check + real-rig, diagnosis only)

The previous addendum flagged an open question: post-bin0-fix, the Normal-baseline
capture is FAULT-heavy rather than WARN-heavy, and ADR-039 speculated this ran through
`_fault_candidate_scores()`. Real code tracing plus a re-analysis of both the
pre-fix and post-fix CSVs (`epm_sat-5ab004_20260810_fresh_postfix.csv`,
`..._postfix_bin0.csv`) found that speculation wrong: `_fault_candidate_scores()` /
`_classify_fault_type()` never feed `compute_alert()` — fault-type labeling and the
OK/WARN/FAULT alert byte are architecturally separate. The real mechanism, and full
reconstructed numbers (raw non-OK rates, per-channel crossing breakdowns, the ~8x vs.
~4.5x persistence-amplification finding, and the ambient-session-confound hypothesis),
are documented as a dated addendum directly in **ADR-039** rather than duplicated here.
No code changed in this task — diagnosis only, as scoped. A clean isolation of the
ambient-confound hypothesis would need same-code back-to-back captures; not done this
pass, flagged there as a future follow-up.

### Task 2 — real mic clean-frequency-band characterization (method: real-rig)

Nine real single-tone captures via `capture_and_compare.py`'s tone-ground-truth path
(closed-form ideal-sine comparison), each confirmed audible by the operator before
capture: 300, 800, 1500 (x2, see below), 1600, 1700, 1800 (x2), 2000, 2500 Hz.
Cleanliness judged by `crest_factor`/`kurtosis_excess` delta from the ideal-sine
closed form (`crest_factor=1.414`, `kurtosis_excess=-1.5`) — the scale-invariant
discriminator; raw `rms` is not usable for this since it is dominated by
speaker-to-mic acoustic coupling loss (consistently ~99% below the electrical closed
form at every frequency, clean or not) rather than signal cleanliness.

| Freq (Hz) | crest delta | kurtosis delta | verdict |
| --- | --- | --- | --- |
| 300 | clean (small deltas, operator-confirmed audible) | n/a | clean |
| 800 | large positive delta | large positive delta | dirty |
| 1500 (1st) | +608% | +22.4 (excess) | dirty (severe) — retried after operator reported room fan turned on mid-session, flagged as a possible confound |
| 1500 (2nd, fan running) | improved but still elevated (kurtosis excess +3.86) | n/a | dirty |
| 1600 | +31.3% | -9.7% | borderline/degraded, ~15 dB quieter peak |
| 1700 | +18.6% | -2.3% | borderline/degraded, ~10 dB quieter peak, peak bin off by a full bin |
| 1800 (1st) | +6.9% | -0.2% | clean |
| 1800 (2nd, post power-cycle) | bimodal — see caveat below | n/a | mixed |
| 2000 | +5.8% | -0.6% | clean |
| 2500 | clean (prior session) | n/a | clean |

**Confounds, reported honestly rather than smoothed over**: (1) a room fan was turned
on by the operator partway through the 1500 Hz test, coincident with the worst single
result of the sweep — cannot be separated from the frequency itself with this data.
(2) The satellite's WiFi dropped and was power-cycled multiple times during this
session (consistent with the recurring instability documented in the 2026-08-09
stress/stability test); the second 1800 Hz capture, taken immediately after one such
power-cycle, showed a bimodal split between a clean cluster matching the first 1800 Hz
result and a cluster of severe kurtosis spikes (63-215) with `hi_r>0.88` — very likely
handling/setup disturbance from the power-cycle itself, not a property of 1800 Hz.
Neither confound was controlled for; both are flagged rather than fixed, per this
report's standing practice of reporting real numbers as found.

**Real bin-edge finding, corrected in code (method: real-rig, commit `0cbc21a`)**: the
2000 Hz capture's clean tone landed in FFT bin 10, which `_band_ratios()` assigns to
`hi_r`, not `mid_r` — revealing that `_band_ratios()`'s docstring-stated 500/2000 Hz
band edges are nominal only. At the real 128-bin/48kHz resolution, `int()` truncation
puts the true edges at 375 Hz (lo/mid) and 1875 Hz (mid/hi), 125 Hz below each nominal
value in both cases. `_band_ratios()`'s logic is unchanged (out of scope); only its
docstring was corrected, as an unrelated pre-existing documentation bug surfaced by
this testing, not a Task 3 deliverable.

**Characterized clean band**: reliably clean is ~1800-2500 Hz plus the isolated 300 Hz
point; 800-1700 Hz is a resonant/degraded zone on this specific rig (likely a
speaker/enclosure/mount resonance), worst around 1500 Hz and tapering at both edges.
This clean band sits almost entirely inside the real *hi* band (>1875 Hz), which
directly shaped the Task 3 carrier-placement decisions below.

### Task 3 — real Looseness and Imbalance captures (method: real-rig)

Both real-hardware attempts below produced **real negative results with a
characterized mechanism**, not classifier bugs — reported as such per this report's
instruction that a clean miss is as valuable as a hit. Shaft Misalignment was out of
scope for this pass (no synthesis function exists yet; its gate is IMU-driven, not
mic-driven, and belongs with the separate accelerometer frequency-ceiling work in the
2026-08-09 characterization) — flagged here as a clearly-scoped future task, not built.

**Mechanical Looseness — gate not reached, real hi_r leakage exceeds closed-form
prediction.** Looseness's gate (`gateway/pipeline/alerting.py`) requires
`mic_kurtosis >= K_WARN(6.0) and hi_r < 0.30 and lo_r < 0.55 and mid_r > 0.20`. The
real mid band (375-1875 Hz) overlaps almost entirely with the confirmed-dirty
800-1700 Hz zone, leaving only a narrow ~100-175 Hz clean pocket just under the
1875 Hz hi-band edge. A `looseness` burst-train manifest with carriers at
1650/1750/1850 Hz was chosen for the best closed-form margin available
(closed-form-predicted `hi_r=0.29`, just under the 0.30 gate) and captured live
(21 frames, mic channel, real `_band_ratios()`/`mic_kurtosis` computed from the
actual decoded frames, not the synthetic manifest). Real result: `hi_r` measured
**0.45-0.48** during the burst — nearly 2x the closed-form prediction and well over
the 0.30 gate on every single frame. `mic_kurtosis` did clear `K_WARN` in several
frames (up to 23.1), confirming the burst structure can produce real impulsiveness —
`hi_r` alone blocked the gate every time. Mechanism: real acoustic spectral leakage
near a burst carrier's spectral width (short `tau_ms=4` ringdowns have a broad
Lorentzian linewidth) crosses the nearby 1875 Hz bin edge far more than the
closed-form ideal-signal calculation accounts for — the tighter a carrier sits to
that edge, the worse the real leakage, and this rig's only clean acoustic pocket
sits right against that edge. **Gate: not satisfied on any frame. Remains
injected-data-only.**

**Mechanical Imbalance — gate not reached, confirmed structurally unreachable via mic
on this rig.** Imbalance's gate requires `mic_crest >= CREST_WARN(5.0) and
mic_kurtosis < K_WARN*1.4(8.4) and lo_r > 0.45`. The real lo band is 0-375 Hz; this
rig's mic acoustic path has no confirmed-clean response below ~1800 Hz (300 Hz being
the one isolated clean exception, sitting inside the real lo band). One real capture
was taken at `--resonance-hz 300` with the `threshold` preset (chosen to target
`mic_crest~5.1`/`mic_kurtosis~8.0`, narrowly inside the gate by design) to get a real
number rather than only the architectural argument. Real result (21 frames, live
decode): `lo_r` stayed at **0.02-0.14** throughout the burst (one pre-burst ambient
frame at 0.56 is excluded as not representative of the signal), while `mid_r` was
**dominant at 0.73-0.85** — the opposite of what the gate needs. `mic_crest` (up to
9.07) and `mic_kurtosis` did clear their respective thresholds in multiple frames, so
the blocker is specifically `lo_r`, consistently and by a wide margin. This is
stronger evidence than the architectural band-mismatch argument alone predicted: even
a carrier whose fundamental sits inside the real lo band gets acoustically reshaped by
this rig's speaker/mic path into mid-band-dominant energy (plausibly via the same
800-1700 Hz resonance found in Task 2), not merely attenuated. **Gate: not satisfied
on any frame. Very likely untestable via mic on this specific rig regardless of
carrier choice — a rig/acoustic-path property, not a tuning failure. Remains
injected-data-only.**

**Follow-up note — 2026-08-10 (same day, post-Task-3): `safe`-preset disambiguation
(real-rig).** The paragraph above rests entirely on the `threshold` preset
(`tau_ms=5.9`, not `4` — `4.0` is `looseness`'s default tau, a different mode/
paragraph; confirmed against `_IMBALANCE_PRESETS` in `generate_and_play.py` before
running anything). A short-tau burst and the `safe` preset's much longer, gentler
decay (`tau_ms=18.0`) put different mechanical demands on the rig's speaker, so this
was checked directly: same `--resonance-hz 300 --shaft-hz 25`, `safe` preset,
6s duration, real-rig capture. `capture_and_compare.py compare` only reports the
last matching frame, so a small scratch script reused its `FrameCapture`/
`band_ratios_from_wire_bins` to record every mic-channel frame in a wider window
(106 frames total). The ~27 frames coinciding with audible playback (identified by
the same signature used to spot the burst in the `threshold` capture: `mid_r`
overtaking `lo_r`) gave real result: `lo_r` **0.1064-0.3570**, `mid_r`
**0.4357-0.6665**, `hi_r` **0.1154-0.2864**, `mic_crest` up to 10.09, `mic_kurtosis`
up to 22.03 (frames outside that window are ambient: `lo_r` 0.30-0.70, `mid_r`
0.15-0.38, consistent with pre/post-burst quiet, and excluded from the range above).

Compared against the `threshold` capture (`lo_r` 0.02-0.14, `mid_r` 0.73-0.85):
`safe`'s longer decay measurably recovers low-band energy — `lo_r`'s ceiling is
~2.5x higher (0.36 vs 0.14) and `mid_r` is correspondingly less dominant (0.44-0.67
vs 0.73-0.85) — while still falling well short of the gate's `lo_r > 0.45` and still
`mid_r`-dominant throughout. This is real evidence that burst-transient response is
a genuine contributing factor alongside the acoustic path itself, not purely an
intrinsic property of any low-frequency signal on this rig. **The "confirmed
structurally unreachable via mic on this rig" framing above overstates it — revised:
unreachable via mic on this rig at both presets tested (short- and long-tau bursts
alike), with burst decay time a real, measurable contributing factor rather than the
acoustic path being the sole explanation.** Gate: not satisfied on any frame at
either preset. Remains injected-data-only.

### Updated fault-category status

Mechanical Imbalance and Mechanical Looseness do **not** graduate to real-hardware-
confirmed status from this addendum — both now carry a genuine, characterized
real-rig negative result instead of being simply untested. Status after this session:
**Bearing Fault** — real-hardware confirmed (prior addendum). **Normal** —
real-hardware confirmed (Part 3). **Mechanical Imbalance, Mechanical Looseness** —
real-hardware attempted, real negative result with mechanism identified, gate not yet
satisfied on this rig. **Shaft Misalignment** and the remaining classifier-only
labels (Severe Anomaly — Inspect, Elevated Vibration, Anomalous Vibration) — still
fully injected-data-only, untouched by this session's real-rig work.

## Addendum — 2026-08-11: Looseness re-test at 256-bin wire resolution (ADR-040)

`docs/decisions/ADR-040-wire-resolution-raised-to-256-bins.md` raised
`EPM_MODEL_SPECTRUM_BINS` 128 -> 256 specifically to attack the leakage mechanism
identified in Task 3 above (a short-tau burst's Lorentzian ringdown linewidth
spreading across coarse 375 Hz bins near the mid/hi edge). This addendum re-runs
that exact test against the new firmware to report the real before/after number,
per this report's standing practice of reporting real numbers as found rather than
assuming a fix worked.

### Task 3 — Looseness re-test at 256 bins (method: real-rig)

Same manifest parameters as the original 2026-08-10 test, no changes: carriers
1650/1750/1850 Hz, `tau_ms=4`, `burst_ms=20`, `period_hz=30`, `amplitude=0.8`,
`sample_rate=48000`, 6 s duration
(`tools/bench_signal_gen/manifests/looseness_20260810_230258.json` was the
original; `looseness_20260811_020612.json` and `looseness_20260811_071513.json`
are this addendum's two repeat captures). At 256 bins the wire mid band shifts to
468.8-1968.8 Hz (`hz_per=93.75` Hz, was 375-1875 Hz at 128 bins) — a real confound
from finer resolution quantizing band edges differently, noted but not separated
from the leakage-reduction effect below.

Two independent live captures, 97 total mic frames. 73 frames coincide with the
actual burst (identified the same way prior addenda did — `mid_r` overtaking
`lo_r` marks a ~2.5 s post-playback tail, excluded from burst statistics as a
likely click/silence artifact rather than the injected fault signal: tail frames
show `lo_r` collapsing to 0.005-0.017 and `mid_r` jumping to 0.80-0.84, unlike
anything in the actual burst).

Real result: `hi_r` during the burst now measures **mean 0.3010, std 0.0284,
range 0.2377-0.3802** across the 73 burst frames — down from the original
firmware's consistent 0.45-0.48 (roughly a 35-40% reduction), and the mean now
sits almost exactly on the 0.30 gate instead of nearly 2x over it. Real,
reproducible improvement in the identified mechanism. It still does not reliably
clear the gate: only 33/73 burst frames (45%) individually satisfy `hi_r < 0.30`,
and the full gate (`mic_kurtosis >= 6.0 and hi_r < 0.30 and lo_r < 0.55 and
mid_r > 0.20`) fires on 0 of the 73 burst frames — `mic_kurtosis` stays under 1 in
magnitude throughout the real burst, nowhere near the 6.0 threshold this gate also
requires. The gate does fire on 9 of the 24 excluded tail frames (kurtosis
6.0-32.98, `mid_r` 0.80-0.84, `lo_r` 0.005-0.017), matching the tail signature
above and excluded on the same basis — a likely playback-stop click, not the
synthesized Looseness signal.

**Gate: still not satisfied on any burst frame. Mechanical Looseness remains
injected-data-only.** Unlike the two 2026-08-10 real-rig negative results above,
this is not a static negative — ADR-040's bin-resolution change produced a real,
measured, roughly 35-40% reduction in the specific mechanism that was blocking the
gate, moving `hi_r`'s mean from clearly-over to straddling the 0.30 threshold. The
category does not graduate today, but this is now a partially-closed gap with a
characterized remaining margin (mean 0.001 over gate, std 0.028 — the frame-to-frame
variance is larger than the remaining gap itself), not an open one.

### Updated fault-category status (supersedes the table above for Looseness only)

**Mechanical Looseness** — real-hardware attempted twice (2026-08-10 at 128 bins,
2026-08-11 at 256 bins); real, measured leakage reduction from the bin-resolution
change, gate still not satisfied on either attempt. **Mechanical Imbalance** —
unchanged from the table above, not re-tested this addendum (its blocker, `lo_r`
dominated by `mid_r`, is a rig acoustic-path property unrelated to bin resolution
— ADR-040 was not expected to and did not address it). All other categories
unchanged from the table above.
