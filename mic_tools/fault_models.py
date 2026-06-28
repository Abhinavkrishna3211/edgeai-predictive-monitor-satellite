#!/usr/bin/env python3
"""
fault_models.py — Physics-grounded bearing fault signal models for simulation.

Generates synthetic microphone FFT spectra (in dBFS) and time-domain statistics
(kurtosis, crest factor) matching the EPM firmware's exact output format, so the
full detection chain — threshold alerting, Bayesian fusion, HST anomaly detection,
Kalman RUL estimation — can be validated end-to-end without real hardware.

SPECTRUM FORMAT
---------------
The firmware accumulates linear POWER per FFT bin over SPEC_AVG_N frames, then:
    bin_db = 10 * log10(avg_power + 1e-12)
This module generates linear power spectra and converts identically.

BEARING FAULT PHYSICS
---------------------
Bearing defect frequencies are given by:
    BPFO = n/2 · f · (1 − d/D · cos α)    outer-race
    BPFI = n/2 · f · (1 + d/D · cos α)    inner-race
    BSF  = D/(2d) · f · (1 − (d/D·cos α)²) ball spin
    FTF  = 1/2   · f · (1 − d/D · cos α)  cage

Fault signatures:
  Outer race: pure tones at 1×, 2×, 3× BPFO — no sidebands (defect is stationary).
  Inner race: tones at BPFI ± k × shaft_hz — AM sidebands because the defect
              rotates through the load zone once per shaft revolution.
  Ball:       tones at 2×BSF ± k × FTF — ball defects occur twice per ball
              revolution and are modulated by cage rotation.
  Cage:       tones at FTF, 2×FTF — low-frequency, sub-shaft.

All fault types additionally excite structural resonances in the 2–8 kHz band
(the mechanism targeted by HIGH_BAND_MIN in recv_verify.py).

KURTOSIS MODEL
--------------
Kurtosis is a time-domain statistic (4th moment / variance²) that cannot be
derived from the FFT alone. Bearing impact impulses produce kurtosis > 3.
Model (physics-motivated, calibrated to match ISO 10816 experience):

    K(s) = 3 + (K_max − 3) · s^β     where β = 1.5

With K_max = 16, β = 1.5:
    s=0.0 → K = 3    (Gaussian noise, no fault)
    s=0.3 → K ≈ 5    (incipient — just above K_WARN=6 at s≈0.35)
    s=0.7 → K ≈ 10   (moderate)
    s=1.0 → K = 16   (severe)

EXPONENTIAL SEVERITY GROWTH
----------------------------
The Kalman RUL estimator fits K(t) = K0 · exp(λ·t).
Simulator produces: K(t) = K0 · exp(λ·t)  with K0 = 3.0.
This gives severity(t) = clamp((K(t) − 3) / (K_max − 3), 0, 1)^(1/β).
"""

import math
import numpy as np
from bearing_math import BearingFreqs, BearingGeometry, COMMON_BEARINGS

# ─── Default bearing and shaft ────────────────────────────────────────────────

DEFAULT_BEARING  = COMMON_BEARINGS['6205']   # SKF 6205: n=9, D=38.5 mm, d=10.3 mm
DEFAULT_SHAFT_HZ = 25.0                       # 1500 RPM

# ─── Kurtosis model constants ────────────────────────────────────────────────

K0    = 3.0    # healthy Gaussian kurtosis
K_MAX = 16.0   # severe fault kurtosis ceiling (conservative)
BETA  = 1.5    # acceleration exponent (>1 = late-stage acceleration)

# ─── Conversion helpers ───────────────────────────────────────────────────────

def to_dbfs(pwr: np.ndarray) -> np.ndarray:
    """Convert linear power spectrum to dBFS as the firmware does.

    Returns array of the same shape in dBFS (negative floats, typically -120..0).
    """
    return 10.0 * np.log10(np.maximum(pwr, 1e-12))


def _add_tone(pwr: np.ndarray, freq_hz: float, amplitude: float, fnyq: float) -> None:
    """Add a tone (in linear AMPLITUDE units) at freq_hz to a POWER spectrum.

    Power is amplitude², so one tone of amplitude A adds A² to that bin.
    Spreading across ±1 adjacent bins with a Hann-like shape avoids aliasing
    when the exact frequency doesn't land on a bin centre.
    """
    n   = len(pwr)
    idx = freq_hz / fnyq * n
    for di in (-1, 0, 1):
        i = int(round(idx)) + di
        if 0 < i < n:
            # Hann weighting: 1 at centre, 0.5 at ±1
            w = 1.0 if di == 0 else 0.5
            pwr[i] += (amplitude * w) ** 2


# ─── Spectrum generators ──────────────────────────────────────────────────────

def healthy_motor_spectrum(
        n_bins: int,
        fs_hz: float,
        shaft_hz: float = DEFAULT_SHAFT_HZ,
        rng: np.random.Generator = None) -> np.ndarray:
    """
    Realistic healthy motor power spectrum (linear units).

    Components:
    - 1/f pink noise floor
    - Shaft 1×, 2×, 3×, 4× harmonics (decreasing amplitude)
    - 50 Hz mains hum (AC motor)
    - Broadband Gaussian measurement noise

    Returns linear power spectrum of length n_bins.  Pass to to_dbfs() for dBFS.
    """
    if rng is None:
        rng = np.random.default_rng()

    freqs = np.linspace(0, fs_hz / 2, n_bins, endpoint=False)
    fnyq  = fs_hz / 2

    # 1/f pink noise floor (power ∝ 1/f)
    pwr = 1e-4 / (1.0 + freqs / 80.0)
    pwr[0] = 0.0  # DC bin zeroed (matches firmware)

    # Shaft harmonics (amplitude → power)
    for k, amp in enumerate([0.30, 0.12, 0.05, 0.02], start=1):
        _add_tone(pwr, k * shaft_hz, amp, fnyq)

    # Mains hum at 50 Hz (and 100 Hz, 150 Hz)
    for k, amp in enumerate([0.08, 0.03, 0.01], start=1):
        _add_tone(pwr, k * 50.0, amp, fnyq)

    # White measurement noise (adds to power directly)
    pwr += rng.exponential(scale=2e-6, size=n_bins)

    # Zero DC bin after noise — mirrors firmware convention (s_mag_db[0] = -120)
    pwr[0] = 0.0

    return np.maximum(pwr, 0.0)


def add_bearing_fault(
        pwr: np.ndarray,
        fs_hz: float,
        bf: BearingFreqs,
        fault_type: str,
        severity: float,
        rng: np.random.Generator = None) -> np.ndarray:
    """
    Add a physics-grounded bearing fault signature to a power spectrum.

    Parameters
    ----------
    pwr        : linear power spectrum (from healthy_motor_spectrum or similar)
    fs_hz      : sample rate of the microphone (Hz)
    bf         : BearingFreqs object (use BearingFreqs.from_shaft_hz())
    fault_type : 'outer' | 'inner' | 'ball' | 'cage'
    severity   : float in [0, 1] — 0 = no fault, 1 = severe
    rng        : numpy Generator for reproducible noise

    Returns modified spectrum (in-place + return for chaining).

    Fault signatures (ISO 10816 / Harris & Piersol):

      outer  — stationary defect: pure tones at 1×, 2×, 3× BPFO.
               No sidebands because the defect location is fixed in the load zone.

      inner  — rotating defect: amplitude-modulated at shaft frequency.
               Tones at BPFI ± k × shaft_hz (k = 0, 1, 2).
               Sidebands are ±70% of main tone amplitude.

      ball   — appears at 2 × BSF (defect contacts race twice per ball revolution).
               Modulated by cage: sidebands at 2×BSF ± k × FTF.

      cage   — low-energy sub-shaft tones at 1×, 2× FTF.

    All fault types add a broadband resonance bump in 2–8 kHz.
    This is the structural response to impact impulses and is what
    recv_verify.py's HIGH_BAND_MIN threshold detects.
    """
    if rng is None:
        rng = np.random.default_rng()
    if severity <= 0.0:
        return pwr

    fnyq      = fs_hz / 2
    fault_amp = severity * 0.45   # max linear amplitude at severity=1

    # Jitter: real bearing tones drift ±0.5% due to speed variation
    jitter = rng.normal(0, 0.005)

    def _tone(freq_hz: float, amp: float) -> None:
        _add_tone(pwr, freq_hz * (1 + jitter), amp * fault_amp, fnyq)

    if fault_type == 'outer':
        # Stationary defect → harmonics only
        _tone(bf.bpfo,      1.00)
        _tone(2 * bf.bpfo,  0.60)
        _tone(3 * bf.bpfo,  0.30)

    elif fault_type == 'inner':
        # Rotating defect → AM sidebands at ± shaft_hz
        for k in (0, 1, 2):
            _tone(bf.bpfi + k * bf.shaft_hz, 1.00 if k == 0 else 0.65 / k)
            if k > 0:
                _tone(bf.bpfi - k * bf.shaft_hz, 0.65 / k)

    elif fault_type == 'ball':
        # 2×BSF with cage-frequency sidebands
        _tone(2 * bf.bsf,               1.00)
        _tone(2 * bf.bsf + bf.ftf,      0.55)
        _tone(2 * bf.bsf - bf.ftf,      0.55)
        _tone(2 * bf.bsf + 2 * bf.ftf,  0.25)

    elif fault_type == 'cage':
        _tone(bf.ftf,      0.70)
        _tone(2 * bf.ftf,  0.35)

    else:
        raise ValueError(f"Unknown fault_type {fault_type!r}; "
                         "expected 'outer', 'inner', 'ball', or 'cage'")

    # Broadband resonance excitation 2–8 kHz — structural response to impacts
    # Gaussian bump centred at 4 kHz, σ = 1.5 kHz, scaled by severity
    idx_lo = max(1, int(2000 / fnyq * len(pwr)))
    idx_hi = min(len(pwr) - 1, int(8000 / fnyq * len(pwr)))
    if idx_lo < idx_hi:
        band_f = np.linspace(2000, 8000, idx_hi - idx_lo)
        resonance_amp = severity * 0.12 * np.exp(-((band_f - 4000.0) ** 2) / (2 * 1500.0 ** 2))
        pwr[idx_lo:idx_hi] += resonance_amp ** 2   # convert to power

    return np.maximum(pwr, 0.0)


# ─── Time-domain statistics ───────────────────────────────────────────────────

def fault_kurtosis(severity: float, rng: np.random.Generator = None) -> float:
    """
    Kurtosis as a function of bearing fault severity.

    Model:  K(s) = K0 + (K_MAX − K0) · s^BETA

    with K0=3 (Gaussian), K_MAX=16 (severe fault ceiling), BETA=1.5
    (slight late-stage acceleration observed in run-to-failure datasets).

    This matches the Kalman RUL estimator's assumption that K grows
    exponentially with time: since severity(t) = clamp(1 − exp(−λt), 0, 1),
    K(t) ≈ K0 · exp(λ_eff · t) for small severity.

    Gaussian noise (σ ≈ 5% of value) reproduces the measurement variance seen
    in rolling-element bearing test rigs.
    """
    if rng is None:
        rng = np.random.default_rng()
    s = float(np.clip(severity, 0.0, 1.0))
    k_det = K0 + (K_MAX - K0) * (s ** BETA)
    noise = rng.normal(0.0, 0.05 * k_det)
    return float(max(1.5, k_det + noise))


def fault_crest(severity: float, rng: np.random.Generator = None) -> float:
    """
    Crest factor (peak / RMS) as a function of severity.

    CF_healthy ≈ 3–4, CF_severe ≈ 11–14.
    Linear growth is a reasonable first-order model (crest reflects single
    largest impact amplitude which grows roughly proportional to damage area).
    """
    if rng is None:
        rng = np.random.default_rng()
    s = float(np.clip(severity, 0.0, 1.0))
    cf_det = 3.2 + s * 10.0
    noise  = rng.normal(0.0, 0.05 * cf_det)
    return float(max(1.0, cf_det + noise))


def fault_rms(severity: float, rng: np.random.Generator = None) -> float:
    """
    Mic RMS level as a function of severity.

    Healthy: 0.002–0.003 (quiet).  Severe: 0.015–0.025 (audible impacts).
    """
    if rng is None:
        rng = np.random.default_rng()
    s   = float(np.clip(severity, 0.0, 1.0))
    rms = 0.002 + s * 0.020
    return float(max(1e-5, rms + rng.normal(0, 0.0005)))


# ─── Exponential severity growth (matches Kalman RUL model) ──────────────────

def make_severity_fn(k_fail: float = 40.0, evolution_seconds: float = 1800.0):
    """
    Return a function  severity(t_seconds) → [0, 1]  that produces exponential
    kurtosis growth matching K(t) = K0 · exp(λ·t) used by the Kalman RUL
    estimator.

    Parameters
    ----------
    k_fail            : kurtosis at which fault is considered severe (default 40)
    evolution_seconds : time (s) at which K reaches k_fail (default 1800 = 30 min)

    Lambda is derived by:  λ = log(k_fail / K0) / evolution_seconds
    Then:  K(t) = K0 · exp(λ·t)
    And:   severity(t) = clamp((K(t) − K0) / (K_MAX − K0))^(1/BETA), 0, 1)
    """
    lam = math.log(k_fail / K0) / evolution_seconds

    def severity_fn(t_seconds: float) -> float:
        k = K0 * math.exp(lam * t_seconds)
        raw = (k - K0) / (K_MAX - K0)
        return float(np.clip(raw ** (1.0 / BETA), 0.0, 1.0))

    severity_fn.lam          = lam
    severity_fn.k_fail        = k_fail
    severity_fn.evolution_sec = evolution_seconds
    return severity_fn


# ─── Convenience: full frame generation ──────────────────────────────────────

FAULT_CYCLE = ('outer', 'inner', 'ball')   # cycles across simulated satellites


def generate_mic_frame(
        n_bins: int,
        fs_hz: float,
        bearing: BearingGeometry = DEFAULT_BEARING,
        shaft_hz: float = DEFAULT_SHAFT_HZ,
        fault_type: str = 'outer',
        severity: float = 0.0,
        rng: np.random.Generator = None):
    """
    Generate one mic frame (FFT dBFS array + time-domain statistics).

    Returns
    -------
    fft_db   : np.ndarray[n_bins]  dBFS spectrum (matches firmware format)
    kurtosis : float
    crest    : float
    rms      : float
    """
    if rng is None:
        rng = np.random.default_rng()

    bf  = BearingFreqs.from_shaft_hz(shaft_hz, bearing)
    pwr = healthy_motor_spectrum(n_bins, fs_hz, shaft_hz, rng)
    if severity > 0.0:
        add_bearing_fault(pwr, fs_hz, bf, fault_type, severity, rng)

    pwr[0]  = 0.0    # DC bin zeroed (firmware convention)
    fft_db  = to_dbfs(pwr)

    kurtosis = fault_kurtosis(severity, rng)
    crest    = fault_crest(severity, rng)
    rms_val  = fault_rms(severity, rng)

    return fft_db, kurtosis, crest, rms_val
