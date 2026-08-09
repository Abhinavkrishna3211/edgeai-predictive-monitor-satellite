#!/usr/bin/env python3
"""
synth_frames.py — synthetic (mic_kurtosis, mic_crest, imu_crest, hi_r, lo_r,
mid_r) tuple generation shared by classify_eval.py (Part 1) and
anomaly_pr_eval.py (Part 2) of the accuracy harness.

Every tuple is built to *deliberately* land in one of three zones relative to
the thresholds that define its intended label in
gateway/pipeline/alerting.py::_classify_fault_type():

  deep-inside  — far on the satisfying side of every defining condition
  on-boundary  — the smallest/largest value that still satisfies a defining
                 condition (exactly T for >=/<=,  T +/- delta for strict >/<)
  just-outside — one delta past on-boundary, on the failing side

Ground truth (`intended_label`) is always the label the tuple was built to
hit — never the classifier's output. Deltas are computed live from
recv_verify's K_WARN/K_FAULT/CREST_WARN so this stays correct if those
constants change; the hi_r/lo_r/mid_r band cuts (0.40, 0.45, 0.35, 0.30,
0.55, 0.20) are NOT rv attributes -- they're inline literals in
alerting.py:98-134 -- so they're hardcoded in _BAND_CUTS below and must be
re-synced if that function's band logic ever changes.

hi_r/lo_r/mid_r are fractions of total spectral power and must sum to 1.0
(mirrors _band_ratios()'s own invariant) so Part 2's synthetic-FFT
construction stays physically consistent. Every generator below fixes two of
the three ratios and derives the third as the remainder.
"""

import os
import sys

import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'mic_tools'))

import matplotlib
matplotlib.use('Agg')  # headless — recv_verify imports matplotlib.pyplot at module level

import recv_verify as rv  # noqa: E402

# Band-energy cut points hardcoded in alerting.py:98-134 (not rv attributes).
_BAND_CUTS = {
    'bearing_hi_r':      0.40,   # hi_r > 0.40                     (branch 2)
    'imbalance_lo_r':    0.45,   # lo_r > 0.45                     (branch 3)
    'misalign_mid_r':    0.35,   # mid_r > 0.35                    (branch 4)
    'loose_hi_r':        0.30,   # hi_r < 0.30                     (branch 5)
    'loose_lo_r':        0.55,   # lo_r < 0.55                     (branch 5)
    'loose_mid_r':       0.20,   # mid_r > 0.20                    (branch 5)
}

ALL_LABELS = [
    "Normal",
    "Bearing Fault — Early",
    "Bearing Fault — Advanced",
    "Mechanical Imbalance",
    "Shaft Misalignment",
    "Mechanical Looseness",
    "Severe Anomaly — Inspect",
    "Elevated Vibration",
    "Anomalous Vibration",
]


def _mag_delta(threshold):
    """1% relative delta for magnitude thresholds (kurtosis, crest), floored
    so it never vanishes to zero for a threshold near 0."""
    return max(0.01 * threshold, 1e-3)


_RATIO_DELTA = 0.01  # absolute delta for hi_r/lo_r/mid_r, all bounded [0,1]


def _zone_value(threshold, comparator, zone, delta):
    """Value of a variable in the given zone for `variable <comparator> threshold`."""
    if comparator == '>=':
        return {'deep-inside': threshold + 20 * delta,
                 'on-boundary': threshold,
                 'just-outside': threshold - delta}[zone]
    if comparator == '>':
        return {'deep-inside': threshold + 20 * delta,
                 'on-boundary': threshold + delta,
                 'just-outside': threshold - delta}[zone]
    if comparator == '<':
        return {'deep-inside': threshold - 20 * delta,
                 'on-boundary': threshold - delta,
                 'just-outside': threshold + delta}[zone]
    if comparator == '<=':
        return {'deep-inside': threshold - 20 * delta,
                 'on-boundary': threshold,
                 'just-outside': threshold + delta}[zone]
    raise ValueError(f"unknown comparator {comparator!r}")


def _ratios(hi=None, lo=None, mid=None):
    """Fix two of (hi_r, lo_r, mid_r), derive the third as the 1.0 remainder.
    Exactly one of hi/lo/mid must be None."""
    given = [v for v in (hi, lo, mid) if v is not None]
    if len(given) != 2:
        raise ValueError("exactly two of hi/lo/mid must be given; the third is derived")
    if hi is None:
        hi = 1.0 - lo - mid
    elif lo is None:
        lo = 1.0 - hi - mid
    else:
        mid = 1.0 - hi - lo
    return hi, lo, mid


def _tuple(intended_label, zone, swept_var, mic_kurtosis, mic_crest, imu_crest,
           hi_r, lo_r, mid_r):
    return dict(intended_label=intended_label, zone=zone, swept_var=swept_var,
                mic_kurtosis=mic_kurtosis, mic_crest=mic_crest, imu_crest=imu_crest,
                hi_r=hi_r, lo_r=lo_r, mid_r=mid_r)


# ─── Neutral (label-irrelevant) values, chosen to comfortably fail every
# branch a given label's tuples must fall through to be reached ─────────────
_LOW_KURTOSIS  = 1.0   # well below K_WARN for any plausible K_WARN >= 2
_LOW_CREST     = 1.0   # well below CREST_WARN for any plausible CREST_WARN >= 2
_LOW_HI_R      = 0.10  # well below the 0.40 bearing-fault band cut


def _gen_normal(K_WARN, K_FAULT, CREST_WARN):
    dk, dc = _mag_delta(K_WARN), _mag_delta(CREST_WARN)
    out = []
    hi, lo, mid = _ratios(hi=0.34, lo=0.33, mid=None)
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        k = _zone_value(K_WARN, '<', zone, dk)
        out.append(_tuple("Normal", zone, 'mic_kurtosis', k, _LOW_CREST, _LOW_CREST, hi, lo, mid))
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        c = _zone_value(CREST_WARN, '<', zone, dc)
        out.append(_tuple("Normal", zone, 'mic_crest', 2.0, c, _LOW_CREST, hi, lo, mid))
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        c = _zone_value(CREST_WARN, '<', zone, dc)
        out.append(_tuple("Normal", zone, 'imu_crest', 2.0, _LOW_CREST, c, hi, lo, mid))
    return out


def _gen_bearing(label, kurtosis_gate, K_WARN, dr=_RATIO_DELTA):
    """label='Bearing Fault — Early' uses kurtosis_gate=K_WARN (>= K_WARN, < K_FAULT);
    label='Bearing Fault — Advanced' uses kurtosis_gate=K_FAULT (>= K_FAULT)."""
    out = []
    dk = _mag_delta(kurtosis_gate)
    k_deep = _zone_value(kurtosis_gate, '>=', 'deep-inside', dk)
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        hr = _zone_value(0.40, '>', zone, dr)
        hi, lo, mid = _ratios(hi=hr, lo=(1 - hr) / 2, mid=None)
        out.append(_tuple(label, zone, 'hi_r', k_deep, _LOW_CREST, _LOW_CREST, hi, lo, mid))
    hi, lo, mid = _ratios(hi=0.60, lo=0.20, mid=None)
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        k = _zone_value(kurtosis_gate, '>=', zone, dk)
        out.append(_tuple(label, zone, 'mic_kurtosis', k, _LOW_CREST, _LOW_CREST, hi, lo, mid))
    return out


def _gen_imbalance(K_WARN, CREST_WARN):
    out = []
    gate = K_WARN * 1.4
    dk, dc, dr = _mag_delta(gate), _mag_delta(CREST_WARN), _RATIO_DELTA
    k_deep = _zone_value(gate, '<', 'deep-inside', dk)
    c_deep = _zone_value(CREST_WARN, '>=', 'deep-inside', dc)
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        c = _zone_value(CREST_WARN, '>=', zone, dc)
        hi, lo, mid = _ratios(hi=_LOW_HI_R, lo=0.65, mid=None)
        out.append(_tuple("Mechanical Imbalance", zone, 'mic_crest',
                           k_deep, c, _LOW_CREST, hi, lo, mid))
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        k = _zone_value(gate, '<', zone, dk)
        hi, lo, mid = _ratios(hi=_LOW_HI_R, lo=0.65, mid=None)
        out.append(_tuple("Mechanical Imbalance", zone, 'mic_kurtosis',
                           k, c_deep, _LOW_CREST, hi, lo, mid))
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        lr = _zone_value(0.45, '>', zone, dr)
        hi, lo, mid = _ratios(hi=_LOW_HI_R, lo=lr, mid=None)
        out.append(_tuple("Mechanical Imbalance", zone, 'lo_r',
                           k_deep, c_deep, _LOW_CREST, hi, lo, mid))
    return out


def _gen_misalignment(K_FAULT, CREST_WARN):
    out = []
    dk, dc, dr = _mag_delta(K_FAULT), _mag_delta(CREST_WARN), _RATIO_DELTA
    ic_deep = _zone_value(CREST_WARN, '>=', 'deep-inside', dc)
    k_deep  = _zone_value(K_FAULT, '<', 'deep-inside', dk)
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        ic = _zone_value(CREST_WARN, '>=', zone, dc)
        hi, lo, mid = _ratios(hi=_LOW_HI_R, lo=None, mid=0.55)
        out.append(_tuple("Shaft Misalignment", zone, 'imu_crest',
                           k_deep, _LOW_CREST, ic, hi, lo, mid))
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        mr = _zone_value(0.35, '>', zone, dr)
        hi, lo, mid = _ratios(hi=_LOW_HI_R, lo=None, mid=mr)
        out.append(_tuple("Shaft Misalignment", zone, 'mid_r',
                           k_deep, _LOW_CREST, ic_deep, hi, lo, mid))
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        k = _zone_value(K_FAULT, '<', zone, dk)
        hi, lo, mid = _ratios(hi=_LOW_HI_R, lo=None, mid=0.55)
        out.append(_tuple("Shaft Misalignment", zone, 'mic_kurtosis',
                           k, _LOW_CREST, ic_deep, hi, lo, mid))
    return out


def _gen_looseness(K_WARN):
    out = []
    dk, dr = _mag_delta(K_WARN), _RATIO_DELTA
    k_deep = _zone_value(K_WARN, '>=', 'deep-inside', dk)
    hi_deep = _zone_value(0.30, '<', 'deep-inside', dr)
    mid_deep = _zone_value(0.20, '>', 'deep-inside', dr)
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        k = _zone_value(K_WARN, '>=', zone, dk)
        hi, lo, mid = _ratios(hi=hi_deep, lo=None, mid=mid_deep)
        out.append(_tuple("Mechanical Looseness", zone, 'mic_kurtosis',
                           k, _LOW_CREST, _LOW_CREST, hi, lo, mid))
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        hr = _zone_value(0.30, '<', zone, dr)
        hi, lo, mid = _ratios(hi=hr, lo=None, mid=mid_deep)
        out.append(_tuple("Mechanical Looseness", zone, 'hi_r',
                           k_deep, _LOW_CREST, _LOW_CREST, hi, lo, mid))
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        mr = _zone_value(0.20, '>', zone, dr)
        hi, lo, mid = _ratios(hi=hi_deep, lo=None, mid=mr)
        out.append(_tuple("Mechanical Looseness", zone, 'mid_r',
                           k_deep, _LOW_CREST, _LOW_CREST, hi, lo, mid))
    # lo_r < 0.55 is a fourth defining condition but is fully determined
    # (1 - hi_r - mid_r) once hi_r/mid_r are fixed above, so it is validated
    # rather than independently swept; see module docstring.
    return out


def _gen_severe_and_elevated(K_WARN, K_FAULT):
    """Fallthrough labels (branches 6/7): kurtosis>=K_FAULT / kurtosis>=K_WARN
    with hi_r<=0.40 (avoid Bearing) and band conditions for 3/4/5 all failing
    (mic_crest, imu_crest kept low so Imbalance/Misalignment's crest gates
    fail regardless of the ratios chosen)."""
    out = []
    dk = _mag_delta(K_FAULT)
    hi, lo, mid = _ratios(hi=0.10, lo=0.80, mid=None)  # hi_r<0.30 avoided intentionally: lo_r dominant, mid_r<0.20 keeps Looseness's mid_r>0.20 gate failing
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        k = _zone_value(K_FAULT, '>=', zone, dk)
        out.append(_tuple("Severe Anomaly — Inspect", zone, 'mic_kurtosis',
                           k, _LOW_CREST, _LOW_CREST, hi, lo, mid))
    dk2 = _mag_delta(K_WARN)
    k_elevated_deep = K_WARN + 2.0 * _mag_delta(K_WARN) * 10  # comfortably between K_WARN and K_FAULT
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        k = _zone_value(K_WARN, '>=', zone, dk2) if zone != 'deep-inside' else k_elevated_deep
        out.append(_tuple("Elevated Vibration", zone, 'mic_kurtosis',
                           k, _LOW_CREST, _LOW_CREST, hi, lo, mid))
    return out


def _gen_anomalous_vibration(K_WARN, CREST_WARN):
    """Branch 8: reachable only with kurtosis < K_WARN throughout (so 6/7
    never fire) but mic_crest or imu_crest >= CREST_WARN (so branch 1 -
    Normal - fails). Only a deep-inside case is meaningful here; there is no
    single defining threshold to boundary-sweep (it's the "none of the
    above" fallback), so on-boundary/just-outside reuse the CREST_WARN
    boundary that keeps it out of Normal."""
    out = []
    dc = _mag_delta(CREST_WARN)
    k = K_WARN - 20 * _mag_delta(K_WARN)
    hi, lo, mid = _ratios(hi=0.1, lo=0.1, mid=None)
    for zone in ('deep-inside', 'on-boundary', 'just-outside'):
        c = _zone_value(CREST_WARN, '>=', zone, dc)
        out.append(_tuple("Anomalous Vibration", zone, 'mic_crest',
                           k, c, _LOW_CREST, hi, lo, mid))
    return out


def _gen_dual_satisfaction_probes(K_WARN, CREST_WARN):
    """Three tuples deep-inside Bearing Fault AND simultaneously deep-inside
    one of {Imbalance, Misalignment, Looseness} -- forces the branch-2
    short-circuit to confirm it's real and reachable (see classify_eval.py's
    priority_collision classification)."""
    out = []
    dk_warn, dc = _mag_delta(K_WARN), _mag_delta(CREST_WARN)
    k_bearing = K_WARN + 20 * dk_warn  # deep-inside Bearing kurtosis gate

    # vs. Imbalance: hi_r deep>0.40 AND lo_r deep>0.45 AND mic_crest deep>=CREST_WARN
    # AND kurtosis < K_WARN*1.4 (still >= K_WARN so Bearing's gate also holds).
    hi, lo, mid = _ratios(hi=0.60, lo=0.50, mid=None)
    k_imb = min(k_bearing, K_WARN * 1.4 - 20 * _mag_delta(K_WARN * 1.4))
    c_deep = CREST_WARN + 20 * dc
    out.append(_tuple("__PROBE__ Bearing-vs-Imbalance", 'dual-satisfaction', None,
                       k_imb, c_deep, _LOW_CREST, hi, lo, mid))

    # vs. Misalignment: hi_r deep>0.40 AND mid_r deep>0.35 AND imu_crest deep>=CREST_WARN
    # AND kurtosis < K_FAULT (still >= K_WARN so Bearing's Early gate holds).
    hi, lo, mid = _ratios(hi=0.60, lo=None, mid=0.35 + 20 * _RATIO_DELTA)
    out.append(_tuple("__PROBE__ Bearing-vs-Misalignment", 'dual-satisfaction', None,
                       k_bearing, _LOW_CREST, c_deep, hi, lo, mid))

    # vs. Looseness: hi_r deep>0.40 (Bearing) vs. Looseness needs hi_r<0.30 --
    # these two conditions are mutually exclusive on hi_r itself, so a true
    # simultaneous-satisfaction tuple is impossible by construction; instead
    # this probe documents that impossibility explicitly (hi_r>0.40 always
    # forecloses Looseness's own hi_r<0.30 gate, so no collision can occur
    # here structurally -- reported as a non-finding, not skipped silently).
    out.append(None)  # placeholder, filtered out by caller; see report note
    return [t for t in out if t is not None], (
        "Bearing-vs-Looseness dual-satisfaction is structurally impossible: "
        "Bearing requires hi_r>0.40 while Looseness requires hi_r<0.30 -- "
        "the two conditions cannot both hold on the same tuple."
    )


def generate_all_tuples():
    """Returns (tuples, probe_tuples, structural_notes) where `tuples` covers
    all 9 labels x 3 zones x defining-conditions, `probe_tuples` are the
    dual-satisfaction priority-collision probes, and `structural_notes` lists
    any probe that could not be constructed for a structural reason."""
    K_WARN, K_FAULT, CREST_WARN = rv.K_WARN, rv.K_FAULT, rv.CREST_WARN

    tuples = []
    tuples += _gen_normal(K_WARN, K_FAULT, CREST_WARN)
    tuples += _gen_bearing("Bearing Fault — Early", K_WARN, K_WARN)
    tuples += _gen_bearing("Bearing Fault — Advanced", K_FAULT, K_WARN)
    tuples += _gen_imbalance(K_WARN, CREST_WARN)
    tuples += _gen_misalignment(K_FAULT, CREST_WARN)
    tuples += _gen_looseness(K_WARN)
    tuples += _gen_severe_and_elevated(K_WARN, K_FAULT)
    tuples += _gen_anomalous_vibration(K_WARN, CREST_WARN)

    probes, notes = _gen_dual_satisfaction_probes(K_WARN, CREST_WARN)
    return tuples, probes, [notes]


def deep_inside_base(label):
    """Single representative deep-inside tuple per label, for Part 2's
    severity-jittering. Returns a dict (same shape as _tuple())."""
    tuples, _, _ = generate_all_tuples()
    candidates = [t for t in tuples if t['intended_label'] == label and t['zone'] == 'deep-inside']
    if not candidates:
        raise ValueError(f"no deep-inside tuple generated for label {label!r}")
    return candidates[0]


# ─── Part 2 extensions: synthetic FFT + frame dicts + severity jitter ──────

def _synthetic_mic_fft_db(hi_r, lo_r, mid_r, fs_hz=None, n_bins=4096,
                           total_power_db=-10.0, floor_db=-120.0):
    """Build a dBFS mic-FFT array whose _band_ratios() recovers (hi_r, lo_r,
    mid_r) exactly (up to floor-power rounding), so the autoencoder's 32
    spectral bands, HST's spectral centroid, and hi_r/lo_r/mid_r all see one
    consistent underlying signal instead of independent scalar stubs.

    Power is spread uniformly across each band's bins (a synthesized tone
    would concentrate it in one bin instead, but band *ratios* — what every
    downstream consumer here reads — only depend on per-band totals).
    """
    if fs_hz is None:
        fs_hz = rv.MIC_FS_HZ
    hz_per  = fs_hz / 2.0 / n_bins
    lo_end  = max(1, int(500 / hz_per))
    mid_end = max(lo_end + 1, int(2000 / hz_per))

    floor_lin = 10.0 ** (floor_db / 10.0)
    power = np.full(n_bins, floor_lin, dtype=np.float64)
    total_lin = 10.0 ** (total_power_db / 10.0)

    lo_bins  = np.arange(1, lo_end)
    mid_bins = np.arange(lo_end, mid_end)
    hi_bins  = np.arange(mid_end, n_bins)
    if len(lo_bins):
        power[lo_bins] = lo_r * total_lin / len(lo_bins)
    if len(mid_bins):
        power[mid_bins] = mid_r * total_lin / len(mid_bins)
    if len(hi_bins):
        power[hi_bins] = hi_r * total_lin / len(hi_bins)

    db = 10.0 * np.log10(np.clip(power, 1e-14, None))
    return np.clip(db, floor_db, 0.0).astype(np.float32)


def tuple_to_frame(t):
    """Expand a 6-tuple dict into a full frame dict consumable by
    make_feature_vector() (autoencoder) and _extract_hst_features() (HST) —
    both need mic_rms/imu_rms/mic_fft that the bare classifier tuple doesn't
    carry. RMS values are a plausible monotonic proxy of crest factor
    (crest = peak/rms; without a real waveform we can't recover rms exactly,
    so this is a documented approximation, not measured data)."""
    hi_r, lo_r, mid_r = t['hi_r'], t['lo_r'], t['mid_r']
    mic_fft = _synthetic_mic_fft_db(hi_r, lo_r, mid_r)
    return dict(
        mic_kurtosis=t['mic_kurtosis'],
        mic_crest=t['mic_crest'],
        mic_rms=0.05 * t['mic_crest'],
        imu_crest=t['imu_crest'],
        imu_rms=0.05 * t['imu_crest'],
        high_band_ratio=hi_r,
        z_score=0.0,
        mic_fft=mic_fft,
    )


def generate_healthy_samples(n, rng, noise_scale=0.05):
    """n jittered Normal-zone tuples, clipped to stay strictly below
    K_WARN/CREST_WARN so every sample is genuinely healthy by the same
    thresholds the classifier itself uses (not just close to the deep-inside
    seed point)."""
    K_WARN, CREST_WARN = rv.K_WARN, rv.CREST_WARN
    k_ceiling = K_WARN - _mag_delta(K_WARN)
    c_ceiling = CREST_WARN - _mag_delta(CREST_WARN)
    base = deep_inside_base("Normal")
    out = []
    for _ in range(n):
        k  = min(max(base['mic_kurtosis'] * (1.0 + noise_scale * rng.standard_normal()), 0.1), k_ceiling)
        c  = min(max(base['mic_crest']    * (1.0 + noise_scale * rng.standard_normal()), 0.1), c_ceiling)
        ic = min(max(1.0                  * (1.0 + noise_scale * rng.standard_normal()), 0.1), c_ceiling)
        hi = float(np.clip(0.34 + 0.03 * rng.standard_normal(), 0.05, 0.35))
        lo = float(np.clip(0.33 + 0.03 * rng.standard_normal(), 0.05, 1.0 - hi - 0.05))
        mid = max(1.0 - hi - lo, 0.01)
        out.append(_tuple("Normal", 'healthy-jitter', None, k, c, ic, hi, lo, mid))
    return out


def generate_severity_variants(label, n, rng, severity_range=(1.0, 3.0), noise_scale=0.05):
    """n jittered variants of `label`'s deep-inside base tuple, scaled by a
    random severity factor in severity_range (kurtosis/crest scale up
    directly; hi/lo/mid ratios get multiplicative jitter then renormalize).
    All deep-inside base tuples sit 20*delta past their defining threshold(s)
    (see _zone_value), so a >=1.0x severity scale with noise_scale=0.05 stays
    on the satisfying side with overwhelming margin (order of 10 sigma) —
    the intended label stays true for every variant."""
    base = deep_inside_base(label)
    out = []
    for _ in range(n):
        sev = rng.uniform(*severity_range)

        def _n(scale=noise_scale):
            return 1.0 + scale * rng.standard_normal()

        k  = max(base['mic_kurtosis'] * sev * _n(), 0.0)
        c  = max(base['mic_crest']    * sev * _n(), 0.1)
        ic = max(base['imu_crest']    * sev * _n(), 0.1)
        hi = float(np.clip(base['hi_r'] * _n(), 0.001, 0.998))
        lo = float(np.clip(base['lo_r'] * _n(), 0.001, 0.998 - hi))
        mid = max(1.0 - hi - lo, 0.001)
        t = _tuple(label, 'severity-jitter', None, k, c, ic, hi, lo, mid)
        t['severity'] = sev
        out.append(t)
    return out


if __name__ == '__main__':
    tuples, probes, notes = generate_all_tuples()
    print(f"{len(tuples)} boundary/zone tuples, {len(probes)} dual-satisfaction probes")
    for n in notes:
        print("NOTE:", n)
