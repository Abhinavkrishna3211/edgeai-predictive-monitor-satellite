# Weak Points Audit

Minimum 8 documented weak points across the EPM codebase, ordered by severity.

---

## WP-01 — AdaptiveBaseline bootstrapping on pre-damaged machines
**Location**: adaptive_baseline.py:37-88; recv_verify.py:950-957
**Severity**: HIGH
**Description**: AdaptiveBaseline only updates on frames where `is_healthy=True`. During
warm-up (first WARMUP_N=30 frames), ALL frames advance the Welford estimator regardless
of true machine health — there is no gate at this stage. If a satellite connects with an
already-damaged bearing (kurtosis already elevated to e.g. 8.0), the first 30 frames seed
the EMA at the damaged level. All subsequent kurtosis values near 8 are then classified as
"normal", making the machine permanently invisible to the adaptive threshold. The rule-based
K_WARN=6.0 absolute threshold still fires, but the adaptive path (Z_WARN_SIGMA=4σ) is
permanently miscalibrated.
**Impact**: Silently high false-negative rate for pre-damaged machines. Adaptive path
provides no additional detection over raw K_WARN.
**Proposed fix**: Add a pre-warm-up kurtosis health check: if the median of the first
WARMUP_N frames exceeds K_WARN, reset and flag the satellite as "pre-degraded — absolute
thresholds only, adaptive baseline deferred until below-threshold period observed".
**Effort**: Small

---

## WP-02 — HIGH_BAND_MIN filter suppresses faults on high-ambient-noise machines
**Location**: recv_verify.py:764-769
**Severity**: HIGH
**Description**: `if raw != EPM_OK and hb < HIGH_BAND_MIN: raw = EPM_OK` suppresses any
alert if the high-band energy ratio is below 0.12 (12%). For machines in high-ambient
low-frequency noise environments (e.g. large industrial fans where 0-500 Hz dominates),
the high-band fraction may remain below 0.12 even during a genuine bearing fault if the
fault amplitude is lower than the background noise floor. This is a known false-negative
path even for real faults.
**Impact**: Genuine bearing faults suppressed in noisy low-frequency environments. The
adaptive bypass (`Z_HB_SIGMA`) is gated on the adaptive baseline being warmed up (30
frames), meaning it also fails for pre-damaged machines (WP-01 compound failure).
**Proposed fix**: Lower HIGH_BAND_MIN from 0.12 to 0.06, or make it configurable
per-machine via the `--high-band-min` CLI argument (already exists but defaults to 0.12
with no sweep-derived justification). Phase 5 sweep data suggests 0.06 maintains
factory-noise rejection while reducing false-negative suppression.
**Effort**: Small

---

## WP-03 — CAL_FRAMES=30 is frame-count based, not time-based
**Location**: recv_verify.py:176; epm_config.h:80
**Severity**: MEDIUM
**Description**: CAL_FRAMES=30 is a raw frame count, not a time duration. At the default
2.2 fps, 30 frames = 13.6 seconds. If SPEC_AVG_N is changed (e.g. increased to 8 for
cleaner spectra), the frame rate drops to ~1.1 fps and 30 frames takes 27 seconds —
acceptable. But if SPEC_AVG_N is reduced to 1 for faster response, frame rate doubles
to ~4.4 fps and calibration completes in 6.8 seconds, which may be insufficient for the
Welford variance estimator to converge reliably. The same applies to AdaptiveBaseline
WARMUP_N=30.
**Impact**: Unstable calibration baseline at non-default frame rates -> inflated z-scores
-> early false alerts OR missed faults depending on noise direction.
**Proposed fix**: Express CAL_FRAMES as a minimum duration (e.g. 20 seconds) and compute
the frame count at runtime from SPEC_AVG_N and MIC_FS_HZ/FFT_MIC_N. Add a soft assertion
logging a warning if CAL_FRAMES resolves to <15 frames.
**Effort**: Small

---

## WP-04 — HST score normalization assumes healthy score ~0.3 (hardcoded)
**Location**: recv_verify.py:750; online_detector.py:76-80
**Severity**: MEDIUM
**Description**: The Bayesian fusion HST channel maps HST score to z-scale as
`z_hst = (hst_score - 0.3) / 0.05`. The constant 0.3 is the assumed "typical healthy
score" and 0.05 is the assumed "scale". These are hardcoded. In practice, the
`_score_ema` (EMA of healthy scores) is tracked inside OnlineDetector but is not exposed
to recv_verify.py. Instead, recv_verify.py uses the fixed offset 0.3 regardless of what
the actual healthy score EMA has converged to. If the actual healthy score settles at 0.4
(possible with different bearing types or noise profiles), the HST z-channel will be
systematically wrong, inflating Bayesian fusion p_fault even during healthy operation.
**Impact**: Persistent low-level p_fusion elevation during healthy operation -> higher
false-alert rate, calibration curve shift.
**Proposed fix**: Expose `detector._score_ema` and use it as the offset:
`z_hst = (hst_score - detector._score_ema) / 0.05`. This is a 1-line fix.
**Effort**: Small

---

## WP-05 — ADWIN delta=0.002 not validated against actual HST score variance
**Location**: online_detector.py:31, 45; ADR-004
**Severity**: MEDIUM
**Description**: ADWIN drift detection fires when the running mean of HST scores
changes significantly. The delta=0.002 threshold was taken from the river library
example, not derived from this system's actual score distribution. Phase 1 shows the
healthy HST score mean is approximately 0.5 with variance sigma≈0.02-0.05. The ADWIN
false-alarm rate under zero-drift is O(1/delta) in expectation. With delta=0.002 and
healthy score std≈0.03, the effective false drift-detection rate needs empirical
validation. If healthy score variance is higher than assumed, ADWIN will fire spuriously,
triggering unnecessary baseline refreshes that reset the HST model and lose accumulated
knowledge.
**Impact**: Spurious drift detections -> unnecessary HST resets -> warm-up period restarted
-> temporary detection gap of ~250 frames (~2 minutes).
**Proposed fix**: After Phase 1 establishes the actual healthy HST score mean and std,
derive delta from: `delta = (sigma_score)^2 / desired_false_alarm_period_frames`. For
sigma=0.03 and 1000-frame false-alarm period: delta = 0.0009. Document this derivation
in ADR-004.
**Effort**: Small

---

## WP-06 — ExponentialRUL K0 initial prior assumes Gaussian kurtosis = 3.0
**Location**: rul_estimator.py:53; ADR-002
**Severity**: MEDIUM
**Description**: The Kalman filter initialises with x[0]=ln(3.0), assuming the machine
starts at Gaussian kurtosis=3.0. In practice, many healthy industrial machines have
kurtosis in the range 3-5 (slightly non-Gaussian vibration from normal imperfection,
gear mesh, etc.). With a loose prior P[0,0]=1.0, the filter adapts quickly. But if the
true healthy kurtosis is 4.5 and the filter initialises at 3.0, the first 30+ frames
will show apparent positive λ (increasing kurtosis) even though the machine is perfectly
healthy. This will cause RUL to be reported too early (potentially infinite->finite
transition within the first few minutes), confusing operators.
**Impact**: Spurious early RUL estimates during warm-up -> low confidence score mitigates
this somewhat (n_updates < 30 guard), but the Kalman state is nonetheless biased.
**Proposed fix**: After the AdaptiveBaseline or calibration phase, update the RUL initial
K0 estimate to `x[0] = log(max(bl_kurt_mean, 3.0))` once CAL_FRAMES are collected.
**Effort**: Small

---

## WP-07 — Per-satellite state dicts accessed from multiple threads without consistent locking
**Location**: recv_verify.py (satellite_thread, dashboard handler, alert logger)
**Severity**: MEDIUM
**Description**: Each satellite's state (`sat.hst_detector`, `sat.ab_kurtosis`, etc.) is
mutated in `satellite_thread` and read by the HTTP dashboard handler and the alert logger,
all in separate threads. While `_sat_models_lock` guards the model dict, the per-satellite
`SatelliteState` object fields are read without locks in the dashboard handler. Under CPython
GIL, individual attribute reads are atomic, but compound reads (e.g. reading `sat.bl_mean`
and `sat.bl_std` as a pair) are not — a torn read could return an updated mean with a
stale std, producing a momentarily bogus z-score visible on the dashboard.
**Impact**: Cosmetic dashboard glitches under high frame rate. No data corruption in
SQLite (writes are under the GIL and WAL is atomic). Risk is low under CPython but
non-zero.
**Proposed fix**: Acquire a per-satellite `threading.Lock` before reading compound state
in the dashboard handler. This is already partially done (`_sat_models_lock`) but
inconsistently applied.
**Effort**: Small

---

## WP-08 — fault_models.py resonance centre (4 kHz) and sigma (1.5 kHz) are arbitrary
**Location**: fault_models.py:230; ADR-001 consequences
**Severity**: MEDIUM (simulation quality, not production code)
**Description**: The broadband resonance bump added to faulty spectra uses a Gaussian
centred at 4000 Hz with σ=1500 Hz. These values are not cited from any bearing test-rig
dataset or ISO standard. For a 6205 bearing at 25 Hz shaft speed, the BPFO is approximately
91 Hz and the expected structural resonance frequency depends on the housing geometry,
typically 2-10 kHz for small bearings. The simulator's resonance bump correctly covers this
range, but the centre at 4 kHz and σ=1.5 kHz were chosen without calibration against real
KX134/INMP441 sensor data from an actual 6205 bearing.
**Impact**: Simulation results may over- or under-estimate the HIGH_BAND_MIN filter's
effectiveness. If real resonance peaks cluster near 2.5-3 kHz (lower than simulated 4 kHz),
the HIGH_BAND_MIN threshold may need adjustment on real hardware.
**Proposed fix**: Collect 10-minute baseline + fault recordings from a real KX134+INMP441
on an actual bearing test rig. Fit Gaussian to the observed resonance. Update fault_models.py
with measured values. Until then, document this as a simulation fidelity limitation.
**Effort**: Large (requires hardware test rig)

---

## WP-09 — HST [0,1] input assumption violated by outlier kurtosis values
**Location**: online_detector.py:59-66
**Severity**: LOW
**Description**: The Welford normalisation maps features to approximately [0,1] via
`clip((z+3)/6, 0, 1)`. For kurtosis, healthy values cluster at z≈0 (mapped to 0.5).
Extreme fault kurtosis values of 20-40 produce z=(40-3)/sigma_k. If sigma_k≈2 at warmup,
z=18.5 -> normalised value = clip((18.5+3)/6, 0, 1) = 1.0 (hard clip). The HST trees
partition the feature space at training time, so novel extreme values (clipped to 1.0) fall
outside the learned partition range. The score returned for such extreme values may be
artificially low (outside partition -> score contribution is zero for that tree).
**Impact**: Paradoxically, extremely severe faults (kurtosis >> K_FAULT) might score
*lower* than moderate faults in the early learning phase. In practice, the absolute
kurtosis threshold K_FAULT=12 catches these cases before the HST clip matters.
**Proposed fix**: Widen the clip to (z+5)/10 for the kurtosis feature specifically, or
use a log transform before normalisation. Document as known limitation in ADR-001.
**Effort**: Small
