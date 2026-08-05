"""gateway/pipeline/alerting.py — spectral band analysis, fault-type
classification, and the per-frame alert engine, extracted from
recv_verify.py (Phase 8b2 task 2).

Every function does a *lazy* `import recv_verify as _rv` inside its own body
(never at module level — recv_verify.py imports from this module at its own
module-load time, so a top-level `import recv_verify` here would try to
import a module that is still mid-initialization). Thresholds like K_WARN,
CREST_FAULT, Z_WARN_SIGMA, ... are CLI-configurable and reassigned wholesale
by recv_verify.main() via `global`; a bare top-level reference here would
silently stop tracking those reassignments, so every recv_verify-owned name
is read as `_rv.NAME` instead — see gateway/api/notifications.py's module
docstring for the same reasoning applied there.

`_ml_score`/`_ml_score_with` and `_sat_models`/`_sat_models_lock` still
physically live in recv_verify.py as of this phase (they move to
gateway/pipeline/ml_scoring.py in Phase 8b2 task 3); they are accessed via
the same lazy `_rv.` indirection, which means this file needs no further
edit when that move happens.
"""

import numpy as np

from gateway.registry.baselines import _sat_update_baseline

try:
    from gateway.pipeline.adaptive_baseline import AdaptiveBaseline
    _AB_AVAILABLE = True
except ImportError:
    AdaptiveBaseline = None  # type: ignore[assignment,misc]
    _AB_AVAILABLE = False


def _band_ratios(mic_fft_db):
    """Compute spectral band energy fractions from a dBFS FFT array.

    Returns (hi_r, lo_r, mid_r) — fractions of total power (DC bin excluded):
      lo  — 0–500 Hz    (mechanical/imbalance/floor noise)
      mid — 500–2000 Hz (resonance, shaft harmonics, misalignment)
      hi  — 2000 Hz–Nyquist (bearing fault resonance region)

    Computed once per frame and shared across _classify_fault_type and
    the alert engine to avoid duplicate 10**() conversions.
    """
    import recv_verify as _rv
    if len(mic_fft_db) < 2:
        return 0.0, 0.0, 0.0
    power   = 10.0 ** (np.clip(mic_fft_db, -140.0, 0.0) / 10.0)
    n       = len(power)
    hz_per  = _rv.MIC_FS_HZ / 2.0 / n
    lo_end  = max(1, int(500  / hz_per))
    mid_end = max(lo_end + 1, int(2000 / hz_per))
    total   = power[1:].sum() + 1e-10
    lo_r    = power[1:lo_end].sum()       / total
    mid_r   = power[lo_end:mid_end].sum() / total
    hi_r    = power[mid_end:].sum()       / total
    return hi_r, lo_r, mid_r


def _high_band_ratio(mic_fft_db):
    """Fraction of mic FFT power in the bearing resonance band (2 kHz–Nyquist)."""
    hi_r, _, _ = _band_ratios(mic_fft_db)
    return hi_r


def _spectral_centroid(mic_fft_db: np.ndarray) -> float:
    """Spectral centroid frequency normalised to [0, 1] over the Nyquist range."""
    import recv_verify as _rv
    if len(mic_fft_db) < 2:
        return 0.5
    power    = 10.0 ** (np.clip(mic_fft_db, -140.0, 0.0) / 10.0)
    n        = len(power)
    freqs    = np.arange(n, dtype=np.float64) * (_rv.MIC_FS_HZ / 2.0 / n)
    total    = power.sum() + 1e-10
    return float((freqs * power).sum() / total) / (_rv.MIC_FS_HZ / 2.0)


def _extract_hst_features(frame: dict, lo_r: float, mid_r: float,
                           hb: float) -> np.ndarray:
    """Build FEATURE_DIM-element feature vector for HST online anomaly detection.

    Features (7):  kurtosis, crest, rms, spectral_centroid,
                   band_energy_low, band_energy_mid, band_energy_high
    """
    mic_fft  = frame.get('mic_fft')
    centroid = _spectral_centroid(mic_fft) if mic_fft is not None else 0.5
    return np.array([
        float(frame['mic_kurtosis']),
        float(frame['mic_crest']),
        float(frame['mic_rms']),
        centroid,
        lo_r,
        mid_r,
        hb,
    ], dtype=np.float64)


def _classify_fault_type(mic_kurtosis, mic_crest, imu_crest, hi_r, lo_r, mid_r):
    """Spectral pattern analysis — classify the likely fault mechanism.

    Accepts pre-computed band energy fractions from _band_ratios() so the
    dBFS→power conversion is not repeated for every frame.

    Returns a short label string suitable for display in dashboards and reports.
    """
    import recv_verify as _rv
    if (mic_kurtosis < _rv.K_WARN and mic_crest < _rv.CREST_WARN
            and imu_crest < _rv.CREST_WARN):
        return "Normal"

    # --- Bearing impact fault: impulsive + high-frequency resonance ---
    if hi_r > 0.40 and mic_kurtosis >= _rv.K_WARN:
        if mic_kurtosis >= _rv.K_FAULT:
            return "Bearing Fault — Advanced"
        return "Bearing Fault — Early"

    # --- Imbalance: sinusoidal, low-frequency dominant, moderate crest ---
    if mic_crest >= _rv.CREST_WARN and mic_kurtosis < _rv.K_WARN * 1.4 and lo_r > 0.45:
        return "Mechanical Imbalance"

    # --- Misalignment: 2× shaft tone in mid band, elevated IMU crest ---
    if imu_crest >= _rv.CREST_WARN and mid_r > 0.35 and mic_kurtosis < _rv.K_FAULT:
        return "Shaft Misalignment"

    # --- Looseness: broadband harmonics spread across all bands ---
    if mic_kurtosis >= _rv.K_WARN and hi_r < 0.30 and lo_r < 0.55 and mid_r > 0.20:
        return "Mechanical Looseness"

    if mic_kurtosis >= _rv.K_FAULT:
        return "Severe Anomaly — Inspect"
    if mic_kurtosis >= _rv.K_WARN:
        return "Elevated Vibration"

    return "Anomalous Vibration"


def compute_alert(sat, frame, warn_streak, ok_streak, sent_alert, hb,
                  hst_score: float = 0.0):
    """Compute per-frame alert level, z-score, and Bayesian fault posterior.

    hb is the pre-computed high-band energy ratio from _band_ratios(), passed
    in to avoid a duplicate dBFS→power conversion (the caller already has it).
    hst_score is the HST anomaly score for the current frame (0.0 if HST is
    not yet active); it is included as a third Bayesian fusion channel when
    the HST detector is warmed up.

    Streak counters are passed in and returned so this function has no
    side-effects on sat.  All sat mutations happen in satellite_thread under
    _sat_lock, eliminating data races with the dashboard HTTP reader thread.

    Returns (alert_byte, z_score, p_fusion, new_warn_streak, new_ok_streak).
    The caller is responsible for updating sent_alert = returned alert_byte.
    """
    import recv_verify as _rv

    mic_kurtosis = frame['mic_kurtosis']
    mic_crest    = frame['mic_crest']
    imu_crest    = frame['imu_crest']
    mic_rms      = frame['mic_rms']

    _sat_update_baseline(sat, mic_rms, mic_kurtosis, _rv.K_WARN, _rv.CAL_FRAMES)

    # ── Z-score (active after calibration) ───────────────────────────────────
    z_score = 0.0
    if sat.calibrated:
        features = np.array([mic_rms, mic_kurtosis], dtype=np.float32)
        z_scores = np.abs(features - sat.bl_mean) / sat.bl_std
        z_score  = float(z_scores.max())

    # ── Adaptive per-machine z-score (escalates raw, never suppresses) ───────
    # Fires when any feature deviates >= Z_WARN_SIGMA/Z_FAULT_SIGMA from THIS
    # machine's own EMA baseline.  Detects incipient wear on quiet machines whose
    # healthy kurtosis is below K_WARN before the absolute thresholds would fire.
    _z_adapt_max = 0.0
    _feat_z: dict = {}   # per-feature z-scores for attribution
    if (sat.ab_kurtosis is not None
            and sat.ab_kurtosis.n_updates >= _rv.AB_WARMUP_FRAMES):
        _feat_z = {
            'mic_kurt':  sat.ab_kurtosis.z_score(mic_kurtosis),
            'mic_crest': sat.ab_crest.z_score(mic_crest),
            'mic_rms':   sat.ab_rms.z_score(mic_rms),
        }
        if sat.ab_hb is not None and sat.ab_hb.n_updates >= _rv.AB_WARMUP_FRAMES:
            _feat_z['mic_hb'] = sat.ab_hb.z_score(hb)
        _z_adapt_max = max(_feat_z.values())

    # ── Raw alert level (before noise filter + persistence) ──────────────────
    raw = _rv.EPM_ALERT_OK
    if mic_kurtosis >= _rv.K_FAULT or z_score >= 5.0 or _z_adapt_max >= _rv.Z_FAULT_SIGMA:
        raw = _rv.EPM_ALERT_FAULT
    elif mic_kurtosis >= _rv.K_WARN or z_score >= 3.0 or _z_adapt_max >= _rv.Z_WARN_SIGMA:
        raw = _rv.EPM_ALERT_WARN
    elif max(mic_crest, imu_crest) >= _rv.CREST_FAULT:
        raw = _rv.EPM_ALERT_FAULT
    elif max(mic_crest, imu_crest) >= _rv.CREST_WARN:
        raw = _rv.EPM_ALERT_WARN

    # ── Bayesian multi-channel fusion — escalates raw if multi-channel evidence
    # agrees; subject to the high-band filter and persistence below.
    # Channels: z_kurtosis, z_rms from calibration baseline + z_hst when ready.
    # Independence assumption: mic kurtosis (impulsive) and RMS (energetic)
    # respond differently to fault modes; HST uses 7 spectral features.
    # For correlated faults this overestimates joint evidence — acceptable for
    # detection; not for fault magnitude. See bayesian_fusion.py for details.
    p_fusion = 0.0
    if _rv._FUSION_AVAILABLE and _rv._bayesian_fusion is not None and sat.calibrated:
        z_k = float((mic_kurtosis  - sat.bl_mean[1]) / sat.bl_std[1])
        z_r = float((mic_rms       - sat.bl_mean[0]) / sat.bl_std[0])
        z_list: list = [z_k, z_r]
        if sat.hst_detector is not None and sat.hst_detector.is_warmed_up():
            # Map HST [0,1] → z-scale using the detector's own EMA of healthy scores
            # as the offset (WP-04 fix: replaces hardcoded 0.3 with adaptive baseline).
            _hst_offset = sat.hst_detector._score_ema
            _z_hst = (hst_score - _hst_offset) / 0.05
            z_list.append(_z_hst)
            _feat_z['hst'] = _z_hst
        if _rv._ae_engine is not None and _rv._ae_stats is not None and _AB_AVAILABLE:
            _ae_feats = np.array([
                mic_rms, mic_crest, mic_kurtosis,
                frame.get('imu_rms', 0.0), imu_crest, hb, z_score,
            ], dtype=np.float32)
            _ae_input  = (_ae_feats - _rv._ae_stats['mean']) / _rv._ae_stats['std']
            _ae_recon  = _rv._ae_engine.run(_ae_input)[0]
            _ae_err    = float(np.mean((_ae_input - _ae_recon) ** 2))
            if sat.mac_hex not in _rv._ae_baselines:
                _rv._ae_baselines[sat.mac_hex] = AdaptiveBaseline()
            _ae_bl = _rv._ae_baselines[sat.mac_hex]
            if raw == _rv.EPM_ALERT_OK:
                _ae_bl.update(_ae_err, is_healthy=True)
            _z_ae = _ae_bl.z_score(_ae_err) if _ae_bl.n_updates >= 30 else 0.0
            z_list.append(_z_ae)
            _feat_z['ae'] = _z_ae
        p_fusion = _rv._bayesian_fusion.fuse(z_list)
        if p_fusion >= _rv.P_FUSION_FAULT:
            raw = max(raw, _rv.EPM_ALERT_FAULT)
        elif p_fusion >= _rv.P_FUSION_WARN:
            raw = max(raw, _rv.EPM_ALERT_WARN)
    sat.feat_z = dict(_feat_z)   # snapshot for live dashboard + attribution

    # ── Factory noise filter: only alert if high-band energy is present ───────
    # Bearing faults excite 2-8kHz; factory floor noise is mostly <500Hz.
    # Exception: if the machine's own HB baseline is elevated >= Z_HB_SIGMA, the
    # alert is a genuine structural-resonance event and must not be suppressed.
    if raw != _rv.EPM_ALERT_OK and hb < _rv.HIGH_BAND_MIN:
        _hb_adapt_ok = (sat.ab_hb is not None
                        and sat.ab_hb.n_updates >= _rv.AB_WARMUP_FRAMES
                        and sat.ab_hb.z_score(hb) >= _rv.Z_HB_SIGMA)
        if not _hb_adapt_ok:
            raw = _rv.EPM_ALERT_OK   # suppress: broadband floor noise, not a fault

    # ── Persistence / hysteresis ──────────────────────────────────────────────
    if raw != _rv.EPM_ALERT_OK:
        warn_streak += 1
        ok_streak    = 0
    else:
        ok_streak   += 1
        warn_streak  = 0

    # Raise: need WARN_PERSIST consecutive non-OK frames
    if warn_streak >= _rv.WARN_PERSIST:
        final = raw
    # Clear: FAULT requires more consecutive OK frames than WARN before auto-clearing
    elif ok_streak >= (_rv.FAULT_CLEAR_PERSIST if sent_alert == _rv.EPM_ALERT_FAULT
                        else _rv.CLEAR_PERSIST):
        final = _rv.EPM_ALERT_OK
    else:
        final = sent_alert   # hold previous state during transition

    # ── Optional ML override: take the more severe of threshold vs ML ─────────
    # Per-satellite model takes priority; --model global flag is the fallback.
    if sat.calibrated:
        ml_frame = {
            'mic_rms':         frame['mic_rms'],
            'mic_crest':       mic_crest,
            'mic_kurtosis':    mic_kurtosis,
            'imu_rms':         frame.get('imu_rms', 0.0),
            'imu_crest':       imu_crest,
            'high_band_ratio': hb,
            'z_score':         z_score,
        }
        with _rv._sat_models_lock:
            sat_model = _rv._sat_models.get(sat.mac_hex)
        if sat_model is not None:
            ml_alert = _rv._ml_score_with(ml_frame, sat_model)
        else:
            ml_alert = _rv._ml_score(ml_frame)   # falls back to global --model if set
        if ml_alert is not None and ml_alert > final:
            final = ml_alert   # escalate if ML is more confident

    return final, z_score, p_fusion, warn_streak, ok_streak
