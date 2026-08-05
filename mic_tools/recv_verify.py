#!/usr/bin/env python3
"""
recv_verify.py — EPM gateway: multi-satellite TCP receiver, alert engine, live plot.

Wire format (per satellite connection):
    [epm_hello_t  24 bytes]             sent once by satellite after connect
    then per frame:
      [uint32_t payload_bytes]  4 bytes (does NOT include itself)
      [epm_header_t header]    48 bytes
      [float mic_fft[mic_bins]]          mic_bins × 4 bytes
      [float imu_x_fft[imu_bins]]        imu_bins × 4 bytes  (radial A)
      [float imu_y_fft[imu_bins]]        imu_bins × 4 bytes  (radial B)
      [float imu_z_fft[imu_bins]]        imu_bins × 4 bytes  (axial)
    gateway sends 1-byte alert after each frame: 0x00=OK  0x01=WARN  0x02=FAULT

Usage:
    python recv_verify.py
    python recv_verify.py --port 5100 --fft-mic-n 1024 --fft-imu-n 2048
    python recv_verify.py --shaft-hz 50              # shaft harmonic markers on FFT
    python recv_verify.py --shaft-rpm 1500           # same via RPM
    python recv_verify.py --shaft-rpm 1500 --bearing 6205   # bearing fault freq markers
    python recv_verify.py --model model/epm_model    # ML-based alerting (after training)
"""

import argparse
import collections
import csv
import datetime
import json
import os
import socket
import struct
import math
import sys
import threading
import time

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

# Repo root on sys.path so `from gateway.pipeline.X import Y` resolves when
# this file is run standalone (python recv_verify.py ...).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# mic_tools/ directory — passed to gateway.registry.{satellite_state,baselines}
# so their model/logs path resolution matches this file's original
# os.path.dirname(__file__) behavior even though they no longer live here.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Optional: bearing fault frequency analysis (gateway/pipeline/bearing_math.py)
MARKER_COLORS: dict = {}   # populated below if bearing_math is importable
try:
    from gateway.pipeline.bearing_math import BearingFreqs, parse_bearing_arg
    from gateway.pipeline.bearing_math import MARKER_COLORS as MARKER_COLORS  # re-bind module-level name
    _BEARING_AVAILABLE = True
except ImportError:
    _BEARING_AVAILABLE = False

# Optional: online HST anomaly detection (river — install with: pip install river>=0.21.0)
_HST_AVAILABLE = False
try:
    from gateway.pipeline.online_detector import OnlineDetector as OnlineDetector
    _HST_AVAILABLE = True
except ImportError:
    OnlineDetector = None  # type: ignore[assignment,misc]

# Optional: Kalman exponential RUL estimator (pure numpy — always available)
_RUL_AVAILABLE = False
try:
    from gateway.pipeline.rul_estimator import ExponentialRUL, RULResult
    _RUL_AVAILABLE = True
except ImportError:
    ExponentialRUL = None  # type: ignore[assignment,misc]
    RULResult = None       # type: ignore[assignment,misc]

# Optional: Bayesian posterior fusion of multi-channel anomaly evidence
_FUSION_AVAILABLE = False
try:
    from gateway.pipeline.bayesian_fusion import BayesianFusion
    _FUSION_AVAILABLE = True
except ImportError:
    BayesianFusion = None  # type: ignore[assignment,misc]

# Optional: per-machine adaptive baselines (pure Python — always available if file is present)
_AB_AVAILABLE = False
try:
    from gateway.pipeline.adaptive_baseline import AdaptiveBaseline
    _AB_AVAILABLE = True
except ImportError:
    AdaptiveBaseline = None  # type: ignore[assignment,misc]

# Optional: ONNX Runtime autoencoder inference (reconstruction-error channel)
_AE_AVAILABLE = False
try:
    from gateway.pipeline.inference import InferenceEngine
    _AE_AVAILABLE = True
except ImportError:
    InferenceEngine = None  # type: ignore[assignment,misc]

# Optional: SQLite-backed storage for alert events, maintenance log, and model state
_STORAGE_AVAILABLE = False
try:
    from gateway.pipeline.storage import Storage, rotate_old_csvs
    _STORAGE_AVAILABLE = True
except ImportError:
    Storage = None          # type: ignore[assignment,misc,misc]
    rotate_old_csvs = None  # type: ignore[assignment]

# Satellite registry + adaptive-baseline/RUL persistence (gateway/registry/*).
# satellite_state.py does a *lazy* `import recv_verify` inside its functions
# (not at module level), so importing it here at module load time is safe —
# see gateway/registry/satellite_state.py's module docstring.
from gateway.registry.satellite_state import (
    SatelliteState, _sat_lock, _satellites,
    _sat_register, _sat_disconnect, _sat_count, _print_sat_table,
)
from gateway.registry.baselines import (
    _baselines_path, _save_baselines, _load_baselines,
    _save_rul_state, _load_rul_state,
    _sat_update_baseline, _recompute_z_baseline,
)
from gateway.pipeline.adaptive_control import _adaptive_overlap, _adaptive_avg_n
from gateway.api.led_control import _write_led, led_set_status
from gateway.api.notifications import (
    _log_alert_event, _log_drift_event, _load_maint_log, _save_maint_log,
    _fire_notification, _send_webhook, _send_email,
)
from gateway.api.dashboard import (
    _DASHBOARD_HTML, _sat_health, _safe_f, _ab_summary, _top_contribs,
    _build_status_json, _DashHandler, start_dashboard,
)
from gateway.api.reports import _generate_report_html

# Optional: AES-128-GCM frame decryption (cryptography>=42.0.0)
_CRYPTO_AVAILABLE = False
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _CRYPTO_AVAILABLE = True
except ImportError:
    AESGCM = None  # type: ignore[assignment,misc]

# Optional: mDNS gateway advertisement (zeroconf>=0.131.0)
_MDNS_AVAILABLE = False
_zc_instance   = None   # Zeroconf instance, kept alive for unregister on exit
try:
    from zeroconf import ServiceInfo, Zeroconf
    _MDNS_AVAILABLE = True
except ImportError:
    ServiceInfo = None  # type: ignore[assignment,misc]
    Zeroconf    = None  # type: ignore[assignment,misc]

# Optional: ML inference (scikit-learn — install with: pip install scikit-learn joblib)
_ML_MODEL = None   # populated by _load_ml_model() if --model is given (legacy global)
_sat_models: dict = {}  # mac_hex → model dict; one per satellite (takes priority over global)
_sat_models_lock = threading.Lock()   # protects _sat_models across satellite + training threads

N_TRAIN_FRAMES = 300  # OK frames to buffer before auto-training a per-satellite model

# ─── Alert history (for compliance audit trail) ───────────────────────────────
_ALERT_HISTORY      = collections.deque(maxlen=1000)
_ALERT_HISTORY_LOCK = threading.Lock()

# ─── SQLite storage (alert events, maintenance, model state) ──────────────────
_storage            = None   # Storage instance, set in main() after log_dir is known

# ─── Maintenance log (in-memory cache; backed by SQLite via _storage) ─────────
_MAINT_LOG          = {}     # mac_hex → maintenance record dict (read cache)
_MAINT_LOG_LOCK     = threading.Lock()
_MAINT_LOG_PATH     = None   # legacy JSON path — kept for migration only

# ─── Notifications ────────────────────────────────────────────────────────────
_NOTIFY_WEBHOOK     = None   # set by --notify-webhook
_NOTIFY_EMAIL_CFG   = None   # set by --notify-email
_NOTIFY_COOLDOWN    = {}     # mac_hex → epoch of last notification sent
_NOTIFY_COOLDOWN_S  = 300    # 5 min minimum between alerts per satellite

# ─── Encryption / security ────────────────────────────────────────────────────
_decryptor          = None   # FrameDecryptor instance, set in main() when --psk-hex given

# ─── Auth ─────────────────────────────────────────────────────────────────────
_AUTH_USER          = None   # set by --auth user:pass
_AUTH_PASS          = None

# ─── Branding ─────────────────────────────────────────────────────────────────
_FACTORY_NAME       = 'EPM Industrial Monitor'  # set by --factory-name

# ─── Protocol constants ───────────────────────────────────────────────────────

EPM_MAGIC   = 0xEA1DF00D
HELLO_MAGIC = 0xEA1D0000

HEADER_FMT  = '<IIIHHffffBfffBBB'   # 48 bytes — added mic_kurtosis float; last B is overflow_count
HEADER_SIZE = struct.calcsize(HEADER_FMT)
assert HEADER_SIZE == 48, f"Header size {HEADER_SIZE}"

HELLO_FMT   = '<I6sBB12s'
HELLO_SIZE  = struct.calcsize(HELLO_FMT)
assert HELLO_SIZE == 24, f"Hello size {HELLO_SIZE}"

EPM_ALERT_OK    = 0x00
EPM_ALERT_WARN  = 0x01
EPM_ALERT_FAULT = 0x02

CREST_WARN  = 5.0   # override with --crest-warn
CREST_FAULT = 10.0  # override with --crest-fault
K_WARN      = 6.0   # kurtosis warn  (Gaussian=3, early fault=6-10)
K_FAULT     = 12.0  # kurtosis fault (advanced fault=12+)
K_FAIL      = 40.0  # ISO 13381-1: severe-stage kurtosis threshold for rolling-element bearings
                    #   K>40 indicates imminent failure; used as RUL target by ExponentialRUL
CAL_FRAMES  = 30    # frames to collect for Z-score baseline
HISTORY_LEN    = 200   # ~90 s of history at 2.2 fps — Uno Q 4GB easily holds this per satellite
WATERFALL_ROWS = 120   # time rows in the mic FFT waterfall (~55 s at 2.2 fps)

# Alert persistence — prevents transient factory noise false positives
WARN_PERSIST        = 2   # consecutive non-OK frames required to raise alert
CLEAR_PERSIST       = 3   # consecutive OK frames to clear a WARN
FAULT_CLEAR_PERSIST = 8   # consecutive OK frames to clear a FAULT (longer hold so the alarm is noticed)

# High-band energy threshold — bearing faults excite 2-8kHz resonance;
# factory noise is mostly <500Hz. Only alert if high-band carries enough energy.
HIGH_BAND_MIN  = 0.12   # 12% of total mic energy must be in 2-8kHz band

MIC_FS_HZ = 16000
IMU_FS_HZ = 25600   # KX134 ODR — must match FFT_IMU_N and epm_config.h

_SERVER_START_T  = time.time()   # used by dashboard uptime counter
_led_last_update = 0.0           # monotonic epoch of last sysfs LED write

# Autoencoder inference engine + per-channel reconstruction-error baseline tracker
# Set at startup via --autoencoder; None when --autoencoder is not supplied.
_ae_engine:    object | None = None   # InferenceEngine
_ae_stats:     object | None = None   # numpy record with mean, std, mean_recon_err
# Per-satellite adaptive reconstruction-error baseline (tracks healthy drift over time)
# Keyed by mac_hex; values are AdaptiveBaseline instances.
_ae_baselines: dict = {}

# HST feature dimension: 7 mic-only stats; extend to 28 when IMU FIFO is real
# (mirrors the 7 mic stats across imu_x, imu_y, imu_z for 4×7=28)
FEATURE_DIM = 7

# Bayesian fusion posterior thresholds (tuned for prior=0.01, 2-3 channels)
P_FUSION_WARN  = 0.70   # posterior >= this → escalate raw to WARN
P_FUSION_FAULT = 0.95   # posterior >= this → escalate raw to FAULT

# Bayesian fusion hyperparameters — overridden by --fault-prior / --evidence-midpoint
_FAULT_PRIOR    = 0.01  # P(fault per frame); tune higher for noisier environments
_EVIDENCE_Z_MID = 2.0   # z-score at which a channel gives 50/50 evidence (Phase 3 sweep: z_mid=2 -> cohen_d +34%)
_bayesian_fusion = None  # BayesianFusion instance created in main()

# ─── Adaptive per-machine baseline thresholds ─────────────────────────────────
#
# When AdaptiveBaseline is warmed up (n_updates >= AB_WARMUP_FRAMES), deviations
# from the machine's own learned distribution escalate alerts independently of the
# global K_WARN/K_FAULT absolute constants.  This catches incipient bearing wear
# on quiet machines whose healthy kurtosis is well below K_WARN=6.
#
# Z_HB_SIGMA: if high-band energy is Z_HB_SIGMA above baseline, allow the alert
# through even when hb < HIGH_BAND_MIN — the machine itself is showing elevated
# structural resonance, so the absolute factory-floor threshold does not apply.
#
Z_WARN_SIGMA    = 4.0    # σ above machine baseline → WARN
Z_FAULT_SIGMA   = 6.0    # σ above machine baseline → FAULT
Z_HB_SIGMA      = 3.0    # σ above baseline high-band energy → bypass HIGH_BAND_MIN
AB_WARMUP_FRAMES = 30    # warm-up length (matches CAL_FRAMES)
AB_SAVE_INTERVAL = 1000  # persist baseline state every N healthy-frame updates

# ─── Adaptive-sensing reply (EPM protocol v2) ─────────────────────────────────
# _adaptive_overlap / _adaptive_avg_n are imported above from
# gateway.pipeline.adaptive_control (Phase 8b1 task 3).
EPM_PROTO_V2_MAGIC = 0xA2  # first byte of v2 reply — distinct from 0x00/0x01/0x02


# ─── Satellite registry ───────────────────────────────────────────────────────
# SatelliteState, _sat_lock, _satellites, _sat_register, _sat_disconnect,
# _sat_count, _print_sat_table are imported above from
# gateway.registry.satellite_state (Phase 8b1 task 2).
#
# _write_led / led_set_status are imported above from gateway.api.led_control
# (Phase 8b2 task 1).


# ─── Display state (most recently updated satellite) ─────────────────────────

class _DisplayState:
    def __init__(self):
        self._lock    = threading.Lock()
        self._frame   = None
        self._satname = "—"
        self._event   = threading.Event()

    def put(self, frame, satname):
        with self._lock:
            self._frame   = frame
            self._satname = satname
        self._event.set()

    def get(self):
        with self._lock:
            return self._frame, self._satname

    def wait(self, timeout=0.5):
        self._event.wait(timeout)
        self._event.clear()


_display = _DisplayState()

# ─── Network helpers ──────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """Return the best local IPv4 address for gateway advertisement.

    Prefers hotspot/ICS subnets (192.168.137.x) over the default-route
    interface so that mDNS advertisements reach satellites on the mobile
    hotspot rather than the upstream LAN.
    """
    import ipaddress, socket as _sock
    candidates: list[str] = []
    try:
        for iface_addrs in socket.getaddrinfo(socket.gethostname(), None):
            addr = iface_addrs[4][0]
            try:
                ip = ipaddress.IPv4Address(addr)
                if not ip.is_loopback and not ip.is_link_local:
                    candidates.append(addr)
            except Exception:
                pass
    except Exception:
        pass
    # Prefer Windows Mobile Hotspot / ICS subnet 192.168.137.x
    for c in candidates:
        if c.startswith('192.168.137.'):
            return c
    # Fall back to default-route interface
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return candidates[0] if candidates else '127.0.0.1'


# ─── AES-128-GCM frame decryption ─────────────────────────────────────────────

class FrameDecryptor:
    """Decrypts AES-128-GCM encrypted EPM frames produced by the satellite firmware.

    Wire format (after the uint32_t payload_bytes length prefix):
        iv[12]           AES-GCM nonce (TRNG-generated per frame on satellite)
        ciphertext[N]    encrypted epm_header_t + FFT arrays
        tag[16]          GCM authentication tag

    The `cryptography` library combines ciphertext and tag as `ciphertext||tag`
    for its decrypt() call — FrameDecryptor handles the split transparently.
    """
    def __init__(self, psk_bytes: bytes):
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography package not installed — run: pip install 'cryptography>=42.0.0'")
        if len(psk_bytes) != 16:
            raise ValueError(f"PSK must be exactly 16 bytes (AES-128), got {len(psk_bytes)}")
        self._aes = AESGCM(psk_bytes)

    def decrypt(self, blob: bytes) -> bytes:
        """Decrypt a payload blob = iv[12] + ciphertext[N] + tag[16].

        Returns the plaintext bytes on success.
        Raises an exception (InvalidTag) if authentication fails — caller logs SECURITY.
        """
        if len(blob) < 12 + 16:
            raise ValueError(f"encrypted blob too short ({len(blob)} bytes)")
        iv         = blob[:12]
        tag        = blob[-16:]
        ciphertext = blob[12:-16]
        return self._aes.decrypt(iv, ciphertext + tag, None)


# ─── Security event logging ────────────────────────────────────────────────────

def _log_security_event(sat_name: str, mac_hex: str, detail: str):
    """Append a SECURITY event to the audit trail and SQLite DB."""
    event = {
        'time':       time.time(),
        'satellite':  sat_name,
        'mac':        mac_hex,
        'event_type': 'SECURITY',
        'alert':      'SECURITY',
        'prev':       'OK',
        'kurtosis':   0.0,
        'crest':      0.0,
        'z_score':    0.0,
        'detail':     detail,
    }
    with _ALERT_HISTORY_LOCK:
        _ALERT_HISTORY.appendleft(event)
    if _storage is not None:
        try:
            _storage.log_alert(sat_name, 'OK', 'SECURITY', 0.0, f'SECURITY: {detail}')
        except Exception:
            pass
    print(f"[SECURITY] {sat_name} ({mac_hex}): {detail}")


# ─── TCP helpers ──────────────────────────────────────────────────────────────

def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection")
        buf.extend(chunk)
    return bytes(buf)


def parse_frame(raw, exp_mic_bins, exp_imu_bins):
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"frame too short ({len(raw)})")

    (magic, frame_id, ts_ms,
     mic_bins, imu_bins,
     mic_rms, mic_crest, mic_dc, mic_kurtosis, mic_clip,
     imu_rms, imu_crest, imu_dc, imu_clip,
     imu_axes, overflow_count) = struct.unpack_from(HEADER_FMT, raw, 0)

    errs = []
    if magic != EPM_MAGIC:
        errs.append(f"BAD MAGIC 0x{magic:08X}")
    if mic_bins != exp_mic_bins:
        errs.append(f"mic_bins={mic_bins} exp={exp_mic_bins}")
    if imu_bins != exp_imu_bins:
        errs.append(f"imu_bins={imu_bins} exp={exp_imu_bins}")
    if imu_axes != 3:
        errs.append(f"imu_axes={imu_axes} exp=3")
    if mic_clip:
        errs.append("MIC CLIP")
    if imu_clip:
        errs.append("IMU CLIP")

    exp_size = HEADER_SIZE + mic_bins * 4 + imu_bins * 4 * imu_axes
    if len(raw) != exp_size:
        # Raise immediately — np.frombuffer on a short buffer silently returns
        # fewer elements than requested, corrupting all FFT arrays downstream.
        raise ValueError(
            f"frame payload {len(raw)} B != expected {exp_size} B "
            f"(mic_bins={mic_bins} imu_bins={imu_bins} imu_axes={imu_axes}); "
            f"header errors: {errs or 'none'}"
        )

    off     = HEADER_SIZE
    mic_fft = np.frombuffer(raw, dtype='<f4', count=mic_bins, offset=off).copy()
    off    += mic_bins * 4
    imu_x   = np.frombuffer(raw, dtype='<f4', count=imu_bins, offset=off).copy()
    off    += imu_bins * 4
    imu_y   = np.frombuffer(raw, dtype='<f4', count=imu_bins, offset=off).copy()
    off    += imu_bins * 4
    imu_z   = np.frombuffer(raw, dtype='<f4', count=imu_bins, offset=off).copy()

    return dict(frame_id=frame_id, ts_ms=ts_ms,
                mic_bins=mic_bins, imu_bins=imu_bins, imu_axes=imu_axes,
                mic_rms=mic_rms, mic_crest=mic_crest, mic_kurtosis=mic_kurtosis,
                imu_rms=imu_rms, imu_crest=imu_crest,
                mic_fft=mic_fft, imu_x=imu_x, imu_y=imu_y, imu_z=imu_z,
                overflow_count=overflow_count,
                errors=errs)


def _band_ratios(mic_fft_db):
    """Compute spectral band energy fractions from a dBFS FFT array.

    Returns (hi_r, lo_r, mid_r) — fractions of total power (DC bin excluded):
      lo  — 0–500 Hz    (mechanical/imbalance/floor noise)
      mid — 500–2000 Hz (resonance, shaft harmonics, misalignment)
      hi  — 2000 Hz–Nyquist (bearing fault resonance region)

    Computed once per frame and shared across _classify_fault_type and
    the alert engine to avoid duplicate 10**() conversions.
    """
    if len(mic_fft_db) < 2:
        return 0.0, 0.0, 0.0
    power   = 10.0 ** (np.clip(mic_fft_db, -140.0, 0.0) / 10.0)
    n       = len(power)
    hz_per  = MIC_FS_HZ / 2.0 / n
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
    if len(mic_fft_db) < 2:
        return 0.5
    power    = 10.0 ** (np.clip(mic_fft_db, -140.0, 0.0) / 10.0)
    n        = len(power)
    freqs    = np.arange(n, dtype=np.float64) * (MIC_FS_HZ / 2.0 / n)
    total    = power.sum() + 1e-10
    return float((freqs * power).sum() / total) / (MIC_FS_HZ / 2.0)


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
    if mic_kurtosis < K_WARN and mic_crest < CREST_WARN and imu_crest < CREST_WARN:
        return "Normal"

    # --- Bearing impact fault: impulsive + high-frequency resonance ---
    if hi_r > 0.40 and mic_kurtosis >= K_WARN:
        if mic_kurtosis >= K_FAULT:
            return "Bearing Fault — Advanced"
        return "Bearing Fault — Early"

    # --- Imbalance: sinusoidal, low-frequency dominant, moderate crest ---
    if mic_crest >= CREST_WARN and mic_kurtosis < K_WARN * 1.4 and lo_r > 0.45:
        return "Mechanical Imbalance"

    # --- Misalignment: 2× shaft tone in mid band, elevated IMU crest ---
    if imu_crest >= CREST_WARN and mid_r > 0.35 and mic_kurtosis < K_FAULT:
        return "Shaft Misalignment"

    # --- Looseness: broadband harmonics spread across all bands ---
    if mic_kurtosis >= K_WARN and hi_r < 0.30 and lo_r < 0.55 and mid_r > 0.20:
        return "Mechanical Looseness"

    if mic_kurtosis >= K_FAULT:
        return "Severe Anomaly — Inspect"
    if mic_kurtosis >= K_WARN:
        return "Elevated Vibration"

    return "Anomalous Vibration"


# _sat_update_baseline is imported above from gateway.registry.baselines.


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
    mic_kurtosis = frame['mic_kurtosis']
    mic_crest    = frame['mic_crest']
    imu_crest    = frame['imu_crest']
    mic_rms      = frame['mic_rms']

    _sat_update_baseline(sat, mic_rms, mic_kurtosis, K_WARN, CAL_FRAMES)

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
            and sat.ab_kurtosis.n_updates >= AB_WARMUP_FRAMES):
        _feat_z = {
            'mic_kurt':  sat.ab_kurtosis.z_score(mic_kurtosis),
            'mic_crest': sat.ab_crest.z_score(mic_crest),
            'mic_rms':   sat.ab_rms.z_score(mic_rms),
        }
        if sat.ab_hb is not None and sat.ab_hb.n_updates >= AB_WARMUP_FRAMES:
            _feat_z['mic_hb'] = sat.ab_hb.z_score(hb)
        _z_adapt_max = max(_feat_z.values())

    # ── Raw alert level (before noise filter + persistence) ──────────────────
    raw = EPM_ALERT_OK
    if mic_kurtosis >= K_FAULT or z_score >= 5.0 or _z_adapt_max >= Z_FAULT_SIGMA:
        raw = EPM_ALERT_FAULT
    elif mic_kurtosis >= K_WARN or z_score >= 3.0 or _z_adapt_max >= Z_WARN_SIGMA:
        raw = EPM_ALERT_WARN
    elif max(mic_crest, imu_crest) >= CREST_FAULT:
        raw = EPM_ALERT_FAULT
    elif max(mic_crest, imu_crest) >= CREST_WARN:
        raw = EPM_ALERT_WARN

    # ── Bayesian multi-channel fusion — escalates raw if multi-channel evidence
    # agrees; subject to the high-band filter and persistence below.
    # Channels: z_kurtosis, z_rms from calibration baseline + z_hst when ready.
    # Independence assumption: mic kurtosis (impulsive) and RMS (energetic)
    # respond differently to fault modes; HST uses 7 spectral features.
    # For correlated faults this overestimates joint evidence — acceptable for
    # detection; not for fault magnitude. See bayesian_fusion.py for details.
    p_fusion = 0.0
    if _FUSION_AVAILABLE and _bayesian_fusion is not None and sat.calibrated:
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
        if _ae_engine is not None and _ae_stats is not None and _AB_AVAILABLE:
            _ae_feats = np.array([
                mic_rms, mic_crest, mic_kurtosis,
                frame.get('imu_rms', 0.0), imu_crest, hb, z_score,
            ], dtype=np.float32)
            _ae_input  = (_ae_feats - _ae_stats['mean']) / _ae_stats['std']
            _ae_recon  = _ae_engine.run(_ae_input)[0]
            _ae_err    = float(np.mean((_ae_input - _ae_recon) ** 2))
            if sat.mac_hex not in _ae_baselines:
                _ae_baselines[sat.mac_hex] = AdaptiveBaseline()
            _ae_bl = _ae_baselines[sat.mac_hex]
            if raw == EPM_ALERT_OK:
                _ae_bl.update(_ae_err, is_healthy=True)
            _z_ae = _ae_bl.z_score(_ae_err) if _ae_bl.n_updates >= 30 else 0.0
            z_list.append(_z_ae)
            _feat_z['ae'] = _z_ae
        p_fusion = _bayesian_fusion.fuse(z_list)
        if p_fusion >= P_FUSION_FAULT:
            raw = max(raw, EPM_ALERT_FAULT)
        elif p_fusion >= P_FUSION_WARN:
            raw = max(raw, EPM_ALERT_WARN)
    sat.feat_z = dict(_feat_z)   # snapshot for live dashboard + attribution

    # ── Factory noise filter: only alert if high-band energy is present ───────
    # Bearing faults excite 2-8kHz; factory floor noise is mostly <500Hz.
    # Exception: if the machine's own HB baseline is elevated >= Z_HB_SIGMA, the
    # alert is a genuine structural-resonance event and must not be suppressed.
    if raw != EPM_ALERT_OK and hb < HIGH_BAND_MIN:
        _hb_adapt_ok = (sat.ab_hb is not None
                        and sat.ab_hb.n_updates >= AB_WARMUP_FRAMES
                        and sat.ab_hb.z_score(hb) >= Z_HB_SIGMA)
        if not _hb_adapt_ok:
            raw = EPM_ALERT_OK   # suppress: broadband floor noise, not a fault

    # ── Persistence / hysteresis ──────────────────────────────────────────────
    if raw != EPM_ALERT_OK:
        warn_streak += 1
        ok_streak    = 0
    else:
        ok_streak   += 1
        warn_streak  = 0

    # Raise: need WARN_PERSIST consecutive non-OK frames
    if warn_streak >= WARN_PERSIST:
        final = raw
    # Clear: FAULT requires more consecutive OK frames than WARN before auto-clearing
    elif ok_streak >= (FAULT_CLEAR_PERSIST if sent_alert == EPM_ALERT_FAULT else CLEAR_PERSIST):
        final = EPM_ALERT_OK
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
        with _sat_models_lock:
            sat_model = _sat_models.get(sat.mac_hex)
        if sat_model is not None:
            ml_alert = _ml_score_with(ml_frame, sat_model)
        else:
            ml_alert = _ml_score(ml_frame)   # falls back to global --model if set
        if ml_alert is not None and ml_alert > final:
            final = ml_alert   # escalate if ML is more confident

    return final, z_score, p_fusion, warn_streak, ok_streak


# ─── Per-frame processing (shared by every ingestion transport) ─────────────

def _process_satellite_frame(sat, frame, mac_hex, csv_w, csv_f,
                             warn_streak, ok_streak, sent_alert, last_frame_id):
    """Everything a frame dict goes through once it exists, regardless of
    which transport produced it (TCP+AES `parse_frame()` or MQTT
    `telemetry_frame.decode_frame()` + adapter): replay protection, band
    ratios, HST scoring/learning, alert computation, RUL, adaptive baseline,
    CSV logging, dashboard/history state, ML buffering, fleet LED, live
    display feed.

    Split out from the old single TCP `satellite_thread` loop (Phase 8a) so
    an MQTT ingestion path can reuse it unchanged rather than duplicating it.
    Deliberately excludes the TCP-specific adaptive-sensing reply
    (`_v2`/`conn.sendall`) and its ADAPT-event log — those belong to the v1/v2
    wire protocol's reverse channel, which MQTT's asymmetric cmd topic has no
    equivalent of (see PHASE_8A_PROMPT.md Task 3/4).

    Returns None if the frame was rejected (replay), in which case the caller
    must skip it exactly as the old inline `continue` did. Otherwise returns
    (alert, p_fault, now, warn_streak, ok_streak, sent_alert, last_frame_id)
    for the caller to persist / act on -- `now` is the wall-clock time this
    frame was processed at, needed by the TCP caller's ADAPT event log.
    """
    name = sat.name

    # ── Replay protection: frame_id must not decrease within a connection ──
    fid = frame.get('frame_id', 0)
    if last_frame_id >= 0 and fid <= last_frame_id:
        _log_security_event(
            name, mac_hex,
            f"Replay detected: frame_id {fid} <= last seen {last_frame_id}")
        return None
    last_frame_id = fid

    now   = time.time()
    fps   = sat.rolling_fps(now)

    # Compute band ratios once — shared by alert engine and fault classifier
    hb, lo_r, mid_r = _band_ratios(frame['mic_fft'])

    # ── Online HST detection — score before compute_alert() so the
    # HST z-score can feed the Bayesian fusion inside that function.
    # Learning happens AFTER alert is known (only on OK frames).
    _hst_feats = None
    hst_score  = 0.0
    if sat.hst_detector is not None:
        _hst_feats = _extract_hst_features(frame, lo_r, mid_r, hb)
        hst_score  = sat.hst_detector.score(_hst_feats)

    prev_alert = sent_alert   # alert from the PREVIOUS frame
    alert, z_score, p_fault, warn_streak, ok_streak = \
        compute_alert(sat, frame, warn_streak, ok_streak, sent_alert, hb,
                      hst_score)
    sent_alert = alert

    # Learn only on healthy frames (anomalous frames would corrupt model)
    _drift_refreshed = False
    if _hst_feats is not None and alert == EPM_ALERT_OK:
        sat.hst_detector.learn(_hst_feats)
        # ADWIN drift check — OK-frame scores only; see check_drift() docstring
        if sat.hst_detector.check_drift(hst_score, now):
            if len(sat.hst_feat_buf) >= 50:
                sat.hst_detector.refresh_baseline(list(sat.hst_feat_buf))
                _recompute_z_baseline(sat, sat.hst_feat_buf)
                _drift_refreshed = True
        sat.hst_feat_buf.append(_hst_feats)

    if _drift_refreshed:
        _log_drift_event(sat.name, mac_hex, len(sat.hst_feat_buf))
        print(f"[{name}] Concept drift — baseline refreshed "
              f"({len(sat.hst_feat_buf)} OK frames, "
              f"refresh #{sat.drift_count + 1})")

    # ── Kalman exponential RUL estimator ─────────────────────────────
    _rul_result = None
    if sat.rul_estimator is not None:
        _rul_result = sat.rul_estimator.update(
            float(frame['mic_kurtosis']), now)

    # ── Adaptive per-machine baseline update (OK-frames only) ─────────
    # Update AFTER the alert decision so an escalating frame does not
    # corrupt the distribution that will score the next frame.
    if sat.ab_kurtosis is not None:
        _is_ok = (alert == EPM_ALERT_OK)
        sat.ab_kurtosis.update(frame['mic_kurtosis'], _is_ok)
        sat.ab_crest.update(frame['mic_crest'],       _is_ok)
        sat.ab_rms.update(frame['mic_rms'],           _is_ok)
        sat.ab_hb.update(hb,                          _is_ok)
        # Live-update bl_mean/bl_std from EMA stats so the Bayesian
        # fusion always sees the current distribution, not the frozen
        # 30-frame initial window.
        if sat.ab_rms.n_updates >= AB_WARMUP_FRAMES:
            sat.bl_mean = np.array([sat.ab_rms.mean,  sat.ab_kurtosis.mean],
                                   dtype=np.float32)
            sat.bl_std  = np.array([sat.ab_rms.std,   sat.ab_kurtosis.std],
                                   dtype=np.float32)
            sat.calibrated = True
            # WP-06 fix: seed RUL K0 from the actual healthy kurtosis baseline
            # rather than assuming Gaussian kurtosis=3.0 for all machines.
            if (sat.rul_estimator is not None
                    and sat.rul_estimator.n_updates < AB_WARMUP_FRAMES * 2):
                import math as _math
                k0_est = max(sat.ab_kurtosis.mean, 1.5)
                sat.rul_estimator.x[0] = _math.log(k0_est)

    # Detect state transitions → audit trail + phone notifications
    if alert != prev_alert:
        _log_alert_event(sat.name, mac_hex, alert, prev_alert,
                         frame['mic_kurtosis'], frame['mic_crest'], z_score,
                         dict(sat.feat_z))
        if alert > prev_alert:   # notify on escalation only, not recovery
            _fire_notification(sat.name, mac_hex,
                               ['OK', 'WARN', 'FAULT'][min(alert, 2)],
                               frame['mic_kurtosis'], frame['mic_crest'], z_score)

    # ── CSV row ───────────────────────────────────────────────────────
    csv_w.writerow([
        f"{now:.3f}", frame['frame_id'], frame['ts_ms'],
        f"{frame['mic_rms']:.6f}", f"{frame['mic_crest']:.3f}",
        f"{frame['mic_kurtosis']:.3f}",
        f"{frame['imu_rms']:.6f}", f"{frame['imu_crest']:.3f}",
        f"{hb:.3f}", f"{z_score:.2f}", f"{p_fault:.4f}",
        ["OK", "WARN", "FAULT"][min(alert, 2)],
        frame['overflow_count']
    ])
    csv_f.flush()

    # I2S DMA overflow since the satellite's last frame — surfaces audio
    # gaps that would otherwise silently degrade mic_rms/kurtosis.
    if frame['overflow_count'] > 0:
        print(f"[{name}] WARNING: overflow_count={frame['overflow_count']} "
              f"(I2S DMA overflow — this frame may have an audio gap)")

    fault_type = _classify_fault_type(
        frame['mic_kurtosis'], frame['mic_crest'], frame['imu_crest'],
        hb, lo_r, mid_r,
    )
    frame['high_band_ratio'] = hb   # carry into display state / plot loop

    with _sat_lock:
        sat.frame_count  += 1
        sat.fps           = fps
        sat.last_t        = now
        sat.last_frame    = frame
        sat.alert         = alert
        sat.warn_streak   = warn_streak
        sat.ok_streak     = ok_streak
        sat.sent_alert    = sent_alert
        sat.fault_type    = fault_type
        # Dashboard history
        sat.last_z  = z_score
        sat.last_hb = hb
        sat.history_alerts.append(int(alert))
        sat.history_kurtosis.append(float(frame['mic_kurtosis']))
        sat.history_crest.append(float(frame['mic_crest']))
        if alert == EPM_ALERT_WARN:
            sat.warn_frames += 1
        elif alert == EPM_ALERT_FAULT:
            sat.fault_frames += 1
            sat.last_fault_t  = now
        # Buffer healthy OK frames for per-satellite auto-training
        if alert == EPM_ALERT_OK and sat.calibrated:
            _mic_fft = frame.get('mic_fft')
            _feat = {
                'mic_rms':         float(frame['mic_rms']),
                'mic_crest':       float(frame['mic_crest']),
                'mic_kurtosis':    float(frame['mic_kurtosis']),
                'imu_rms':         float(frame.get('imu_rms', 0.0)),
                'imu_crest':       float(frame.get('imu_crest', 0.0)),
                'high_band_ratio': float(hb),
                'z_score':         float(z_score),
                # mic_fft included for spectral autoencoder training
                'mic_fft':         _mic_fft.copy() if _mic_fft is not None else None,
            }
            sat.ml_buf.append(_feat)
            if len(sat.ml_buf) > N_TRAIN_FRAMES:
                sat.ml_buf = sat.ml_buf[-N_TRAIN_FRAMES:]
        sat.hst_score    = hst_score
        sat.p_fault      = p_fault
        sat.rul_result   = _rul_result
        if _drift_refreshed:
            sat.drift_count  += 1
            sat.last_drift_t  = now
        _need_train = (sat.calibrated and not sat.ml_training
                       and not sat.ml_trained
                       and len(sat.ml_buf) >= N_TRAIN_FRAMES)
        _should_save_hst = (sat.hst_detector is not None
                            and sat.frame_count % 500 == 0)
        _should_save_rul = (sat.rul_estimator is not None
                            and sat.frame_count % 500 == 0)
        _should_save_baseline = (
            sat.ab_kurtosis is not None
            and alert == EPM_ALERT_OK
            and sat.ab_kurtosis.n_updates > 0
            and sat.ab_kurtosis.n_updates % AB_SAVE_INTERVAL == 0)

    if _need_train:
        _trigger_sat_training(sat)
    if _should_save_hst:
        _save_hst_state(sat)
    if _should_save_rul:
        _save_rul_state(_storage, sat)
    if _should_save_baseline:
        _save_baselines(_BASE_DIR, _storage, sat)

    # Uno Q sysfs LED — reflect worst fleet state, at most once per second
    global _led_last_update
    _now_mono = time.monotonic()
    if _now_mono - _led_last_update >= 1.0:
        with _sat_lock:
            _worst_val = max(
                (s.sent_alert for s in _satellites.values() if s.connected),
                default=EPM_ALERT_OK,
            )
        led_set_status(["OK", "WARN", "FAULT"][min(_worst_val, 2)])
        _led_last_update = _now_mono

    _display.put(frame, name)

    cal_str   = (f"z={z_score:.1f}" if sat.calibrated
                 else f"cal{len(sat._cal_buf)}/{CAL_FRAMES}")
    alert_str = ["OK", "WARN", "FAULT"][min(alert, 2)]
    hst_str   = (f"hst={hst_score:.3f}" if sat.hst_detector is not None
                 else "hst=--")
    pf_str    = (f"pf={p_fault:.2f}" if sat.calibrated else "pf=--")
    status    = "OK" if not frame['errors'] else "WARN:" + ";".join(frame['errors'])
    print(f"[{name:<10}] #{frame['frame_id']:5d}  "
          f"fps={fps:.1f}  "
          f"rms={frame['mic_rms']:.5f}  "
          f"K={frame['mic_kurtosis']:.2f}  "
          f"CF={frame['mic_crest']:.2f}  "
          f"hb={hb:.2f}  {cal_str}  "
          f"{hst_str}  {pf_str}  alert={alert_str}  {status}")

    return alert, p_fault, now, warn_streak, ok_streak, sent_alert, last_frame_id


# ─── Per-satellite connection thread ─────────────────────────────────────────

def satellite_thread(conn, addr, exp_mic_bins, exp_imu_bins):
    mac_hex = None
    sat     = None
    csv_f   = None
    csv_w   = None
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(15.0)   # unblock recv_exact() if satellite goes silent

        # ── Parse hello packet ────────────────────────────────────────────────
        hello_raw = recv_exact(conn, HELLO_SIZE)
        magic, mac_bytes, fw_major, fw_minor, name_bytes = \
            struct.unpack(HELLO_FMT, hello_raw)

        if magic != HELLO_MAGIC:
            print(f"[{addr[0]}] Bad hello magic 0x{magic:08X} — dropping")
            return

        mac_hex = ':'.join(f'{b:02X}' for b in mac_bytes)
        name    = name_bytes.split(b'\x00')[0].decode('ascii', errors='replace')
        sat     = _sat_register(mac_hex, name, fw_major, fw_minor, addr)

        print(f"\n[+] Satellite connected: {name}  MAC={mac_hex}  "
              f"fw={fw_major}.{fw_minor}  from {addr[0]}:{addr[1]}")
        print(f"    Satellites active: {_sat_count()}")
        _print_sat_table()

        # ── CSV log: logs/csv/YYYY/MM/epm_{name}_{date}.csv, append on reconnect ──
        # Dated subdirectory layout allows background rotation to gzip by age.
        log_dir  = os.path.join(os.path.dirname(__file__), 'logs')
        now_dt   = datetime.datetime.now()
        csv_dir  = os.path.join(log_dir, 'csv', now_dt.strftime('%Y'), now_dt.strftime('%m'))
        os.makedirs(csv_dir, exist_ok=True)
        date_str = now_dt.strftime('%Y%m%d')
        csv_path = os.path.join(csv_dir, f"epm_{name}_{date_str}.csv")
        is_new   = not os.path.exists(csv_path)
        csv_f    = open(csv_path, 'a', newline='')
        csv_w    = csv.writer(csv_f)
        if is_new:
            csv_w.writerow(['wall_time', 'frame_id', 'device_ms',
                            'mic_rms', 'mic_crest', 'mic_kurtosis',
                            'imu_rms', 'imu_crest',
                            'high_band_ratio', 'z_score', 'p_fault', 'alert',
                            'overflow_count'])
        print(f"    Logging to: {csv_path}  ({'new' if is_new else 'append'})")

        # Maximum valid payload: header + mic FFT + 3 × IMU FFT + 1 KB margin.
        # In encrypted mode, add 12 (IV) + 16 (tag) overhead.
        # Guards against a malicious/buggy satellite sending a huge length prefix
        # that would cause recv_exact to try to allocate gigabytes.
        _plaintext_len = HEADER_SIZE + exp_mic_bins * 4 + exp_imu_bins * 4 * 3
        max_payload = (_plaintext_len + 12 + 16 + 1024)

        # Per-connection streak counters — kept as local variables so mutations
        # never race with the dashboard HTTP reader (all sat writes go through lock).
        warn_streak    = 0
        ok_streak      = 0
        sent_alert     = EPM_ALERT_OK
        last_frame_id  = -1   # replay protection: must be monotonically increasing

        while True:
            (payload_bytes,) = struct.unpack('<I', recv_exact(conn, 4))
            # Accept either plaintext or encrypted payload sizes
            min_size = HEADER_SIZE if _decryptor is None else (12 + HEADER_SIZE + 16)
            if payload_bytes < min_size or payload_bytes > max_payload:
                raise ValueError(
                    f"payload_bytes={payload_bytes} out of valid range "
                    f"[{min_size}..{max_payload}]")
            raw = recv_exact(conn, payload_bytes)

            # ── AES-128-GCM decryption (if gateway started with --psk-hex) ──────
            if _decryptor is not None:
                try:
                    raw = _decryptor.decrypt(raw)
                except Exception as exc:
                    _log_security_event(
                        sat.name, mac_hex,
                        f"GCM tag verification failed on frame — possible key mismatch "
                        f"or injected data ({exc})")
                    continue   # keep connection; satellite can retry after reboot

            frame = parse_frame(raw, exp_mic_bins, exp_imu_bins)

            _result = _process_satellite_frame(
                sat, frame, mac_hex, csv_w, csv_f,
                warn_streak, ok_streak, sent_alert, last_frame_id)
            if _result is None:
                continue   # replay — already logged inside _process_satellite_frame
            (alert, p_fault, now,
             warn_streak, ok_streak, sent_alert, last_frame_id) = _result

            # ── EPM v2 adaptive reply ──────────────────────────────────────────
            # Build and send the 8-byte v2 struct.  The AI posterior reshapes
            # the satellite's FFT pipeline: higher P(fault) → more overlap and
            # less averaging → faster temporal response to transient fault events.
            _ov  = _adaptive_overlap(p_fault)
            _avg = _adaptive_avg_n(p_fault)
            _v2  = struct.pack('<BBHBBBB',
                               EPM_PROTO_V2_MAGIC,        # proto_ver
                               alert,                     # alert_state
                               min(int(p_fault * 10000), 10000),  # fault_posterior
                               _ov,                       # fft_overlap_pct
                               _avg,                      # spec_avg_n
                               0, 0)                      # reserved
            try:
                conn.sendall(_v2)
            except OSError:
                break

            # Log ADAPT event when commanded parameters change
            if _ov != sat.adapt_overlap or _avg != sat.adapt_avg_n:
                _adapt_event = {
                    'time':       now,
                    'satellite':  sat.name,
                    'mac':        mac_hex,
                    'event_type': 'ADAPT',
                    'alert':      'INFO',
                    'prev':       'INFO',
                    'kurtosis':   0.0,
                    'crest':      0.0,
                    'z_score':    0.0,
                    'detail':     (f'Sensor adapt: overlap {sat.adapt_overlap}%→{_ov}%  '
                                   f'avg_n {sat.adapt_avg_n}→{_avg}  '
                                   f'p_fault={p_fault:.3f}'),
                }
                with _ALERT_HISTORY_LOCK:
                    _ALERT_HISTORY.appendleft(_adapt_event)
                sat.adapt_overlap = _ov
                sat.adapt_avg_n   = _avg

    except (ConnectionError, struct.error, OSError) as e:
        print(f"\n[-] {(sat.name if sat else mac_hex) or addr[0]} disconnected: {e}")
    except Exception as e:
        print(f"\n[-] {(sat.name if sat else mac_hex) or addr[0]} error: {e}")
    finally:
        if csv_f:
            try:
                csv_f.close()
            except OSError:
                pass
        try:
            conn.close()
        except OSError:
            pass
        if mac_hex:
            _sat_disconnect(mac_hex)
        print(f"    Satellites remaining: {_sat_count()}")
        _print_sat_table()


# ─── ML model loader ─────────────────────────────────────────────────────────

def _load_ml_model(model_prefix: str):
    """
    Load the IsolationForest model produced by ml_trainer.py.
    Sets the global _ML_MODEL dict so compute_alert() can use it.
    Silently skips if joblib/scikit-learn is not installed.
    """
    global _ML_MODEL
    meta_p  = model_prefix + '_meta.json'
    model_p = model_prefix + '_iso.joblib'
    if not (os.path.exists(meta_p) and os.path.exists(model_p)):
        print(f'[ml] Model files not found at "{model_prefix}" — using threshold-based alerting')
        return
    try:
        import joblib
        with open(meta_p) as f:
            meta = json.load(f)
        bundle = joblib.load(model_p)
        _ML_MODEL = {
            'scaler':    bundle['scaler'],
            'model':     bundle['model'],
            'feat_cols': meta.get('base_features', ['mic_rms', 'mic_crest',
                         'mic_kurtosis', 'imu_rms', 'imu_crest',
                         'high_band_ratio', 'z_score']),
            't_warn':    meta['threshold_warn'],
            't_fault':   meta['threshold_fault'],
        }
        print(f'[ml] Model loaded  trained={meta["trained_at"]}  '
              f'n={meta.get("n_samples","?")}  '
              f'contamination={meta.get("contamination",0):.0%}')
        # VERIFY-FIX: use ASCII comparison operators to avoid cp1252 UnicodeEncodeError on Windows.
        print(f'[ml] Thresholds -- WARN <= {_ML_MODEL["t_warn"]:.4f}   '
              f'FAULT <= {_ML_MODEL["t_fault"]:.4f}')
    except ImportError:
        print('[ml] WARNING: joblib/scikit-learn not installed — '
              'ignoring --model.  Run: pip install scikit-learn joblib')
    except Exception as e:
        print(f'[ml] WARNING: failed to load model: {e}')


def _ml_score(frame: dict) -> int | None:
    """
    Run ML inference on one frame dict using the global --model (legacy).
    Returns EPM_ALERT_* (0/1/2) or None if no model is loaded.
    Frame dict keys match CSV columns produced by the satellite thread.
    """
    if _ML_MODEL is None:
        return None
    return _ml_score_with(frame, _ML_MODEL)


def _ml_score_with(frame: dict, model: dict) -> int | None:
    """Run ML inference using an explicit model dict (per-satellite or global).

    Routes to TFLite neural autoencoder (NPU) when available, otherwise falls
    back to scikit-learn IsolationForest (CPU).
    """
    if model.get('type') == 'tflite':
        return _ml_score_tflite(frame, model)
    # ── IsolationForest fallback path ─────────────────────────────────────────
    try:
        f = frame if ('log_kurtosis' in frame and 'log_z' in frame) else dict(frame)
        if 'log_kurtosis' not in f:
            f['log_kurtosis'] = math.log1p(max(f.get('mic_kurtosis', 0.0), 0.0))
        if 'log_z' not in f:
            f['log_z'] = math.log1p(max(f.get('z_score', 0.0), 0.0))
        feat  = [f.get(c, 0.0) for c in model['feat_cols']]
        X_s   = model['scaler'].transform([feat])
        score = float(model['model'].decision_function(X_s)[0])
        if score <= model['t_fault']:
            return EPM_ALERT_FAULT
        if score <= model['t_warn']:
            return EPM_ALERT_WARN
        return EPM_ALERT_OK
    except Exception:
        return None


def _ml_score_tflite(frame: dict, model: dict) -> int | None:
    """Score one frame using the TFLite neural autoencoder (NPU path)."""
    try:
        from gateway.pipeline.autoencoder import make_feature_vector
        feat = make_feature_vector(frame)
    except ImportError:
        # Minimal fallback if autoencoder.py is somehow missing
        kurtosis = float(frame.get('mic_kurtosis', 3.0))
        z_score  = float(frame.get('z_score', 0.0))
        stats = [
            float(frame.get('mic_rms', 0.0)), float(frame.get('mic_crest', 1.0)),
            kurtosis, float(frame.get('imu_rms', 0.0)), float(frame.get('imu_crest', 1.0)),
            float(frame.get('high_band_ratio', 0.0)), z_score,
            math.log1p(max(kurtosis, 0.0)), math.log1p(max(z_score, 0.0)),
        ]
        import numpy as _np
        feat = _np.array(stats + [0.0] * 32, dtype=_np.float32)
    try:
        mse = model['inferencer'].infer(feat)
        if mse >= model['t_fault']:
            return EPM_ALERT_FAULT
        if mse >= model['t_warn']:
            return EPM_ALERT_WARN
        return EPM_ALERT_OK
    except Exception:
        return None


# ─── Per-satellite ML helpers ─────────────────────────────────────────────────

_TRAIN_FEATS = ['mic_rms', 'mic_crest', 'mic_kurtosis',
                'imu_rms', 'imu_crest', 'high_band_ratio', 'z_score']


def _try_load_sat_model(sat):
    """Load a per-satellite model from disk into _sat_models (silent if missing).

    Priority:
      1. TFLite neural autoencoder (NPU-ready) — <name>_autoencoder.tflite
      2. IsolationForest joblib bundle          — <name>_iso.joblib  (legacy)
    """
    model_dir = os.path.join(os.path.dirname(__file__), 'model')
    mac_slug  = sat.mac_hex.replace(':', '')

    # ── 1. Try TFLite autoencoder (NPU path) ─────────────────────────────────
    try:
        from gateway.pipeline.autoencoder import load_npu_model
        for stem in (sat.name, mac_slug):
            model_dict = load_npu_model(os.path.join(model_dir, stem))
            if model_dict is None:
                continue
            with _sat_models_lock:
                _sat_models[sat.mac_hex] = model_dict
            sat.ml_trained    = True
            sat.ml_trained_at = model_dict.get('trained_at')
            sat.ml_backend    = model_dict.get('backend', 'TFLite')
            print(f'[ml] [{sat.name}] Neural autoencoder loaded  '
                  f'backend={model_dict["backend"]}  '
                  f'n={model_dict["n_samples"]}  '
                  f'WARN≥{model_dict["t_warn"]:.4f}  '
                  f'FAULT≥{model_dict["t_fault"]:.4f}')
            return
    except ImportError:
        pass   # autoencoder.py or tflite_runtime not installed
    except Exception as e:
        print(f'[ml] [{sat.name}] TFLite load warning: {e}')

    # ── 2. Fall back to IsolationForest joblib ────────────────────────────────
    try:
        import joblib as _jl
        for stem in (sat.name, mac_slug):
            prefix = os.path.join(model_dir, stem)
            meta_p = prefix + '_meta.json'
            iso_p  = prefix + '_iso.joblib'
            if not (os.path.exists(meta_p) and os.path.exists(iso_p)):
                continue
            with open(meta_p) as f:
                meta = json.load(f)
            bundle = _jl.load(iso_p)
            with _sat_models_lock:
                _sat_models[sat.mac_hex] = {
                    'type':      'isolation_forest',
                    'scaler':    bundle['scaler'],
                    'model':     bundle['model'],
                    'feat_cols': meta.get('feature_cols',
                                          meta.get('base_features', _TRAIN_FEATS)),
                    't_warn':    meta['threshold_warn'],
                    't_fault':   meta['threshold_fault'],
                }
            sat.ml_trained    = True
            sat.ml_trained_at = meta.get('trained_at')
            sat.ml_backend    = 'IsolationForest (CPU)'
            print(f'[ml] [{sat.name}] IsolationForest loaded (no TFLite model found)  '
                  f'n={meta.get("n_samples", "?")}  '
                  f'WARN≤{meta["threshold_warn"]:.4f}  '
                  f'FAULT≤{meta["threshold_fault"]:.4f}')
            return
    except ImportError:
        pass
    except Exception as e:
        print(f'[ml] [{sat.name}] Warning: could not load model: {e}')


# ─── Per-satellite HST detector helpers ──────────────────────────────────────

def _try_load_hst_state(sat):
    """Load HST detector state from pickle; create a fresh detector if absent."""
    if not _HST_AVAILABLE:
        return
    log_dir  = os.path.join(os.path.dirname(__file__), 'logs')
    pkl_path = os.path.join(log_dir, f'hst_state_{sat.name}.pkl')
    det = OnlineDetector(n_features=FEATURE_DIM, n_trees=10)  # Phase 2 sweep: n_trees=10 optimal
    if os.path.exists(pkl_path):
        try:
            det.load(pkl_path)
            print(f'[hst] [{sat.name}] Resumed detector from {pkl_path}  '
                  f'(n={det._n} frames learned)')
        except Exception as e:
            print(f'[hst] [{sat.name}] Could not resume {pkl_path}: {e} — starting fresh')
            det = OnlineDetector(n_features=FEATURE_DIM, n_trees=10)
    sat.hst_detector = det


def _save_hst_state(sat):
    """Persist HST detector state to disk (called every 500 frames)."""
    if sat.hst_detector is None:
        return
    log_dir  = os.path.join(os.path.dirname(__file__), 'logs')
    pkl_path = os.path.join(log_dir, f'hst_state_{sat.name}.pkl')
    try:
        sat.hst_detector.save(pkl_path)
    except Exception as e:
        print(f'[hst] [{sat.name}] State save failed: {e}')


# ─── Per-satellite adaptive baseline / Kalman RUL persistence ────────────────
# _baselines_path, _save_baselines, _load_baselines, _save_rul_state,
# _load_rul_state are imported above from gateway.registry.baselines.


def _trigger_sat_training(sat):
    """Snapshot the OK-frame buffer and start background model training."""
    with _sat_lock:
        if sat.ml_training:
            return
        sat.ml_training = True
        buf_copy = list(sat.ml_buf)
    print(f'[ml] [{sat.name}] Auto-training on {len(buf_copy)} OK frames…')
    threading.Thread(
        target=_train_sat_model_bg,
        args=(sat, buf_copy),
        daemon=True,
        name=f'train-{sat.name}',
    ).start()


def _train_sat_model_bg(sat, buf):
    """Train per-satellite anomaly detection model in a daemon thread.

    Tries in order:
      1. Neural autoencoder → TFLite export (runs on Qualcomm NPU on Uno Q)
      2. IsolationForest fallback (if TensorFlow unavailable)
    """
    model_dir  = os.path.join(os.path.dirname(__file__), 'model')
    os.makedirs(model_dir, exist_ok=True)
    prefix     = os.path.join(model_dir, sat.name)
    trained_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # ── 1. Neural autoencoder path (preferred — NPU) ──────────────────────────
    try:
        from gateway.pipeline.autoencoder import make_feature_vector, train_autoencoder, export_tflite, load_npu_model

        feat_vecs = [make_feature_vector(f) for f in buf]
        X = np.array(feat_vecs, dtype=np.float32)
        X = X[~np.any(np.isnan(X) | np.isinf(X), axis=1)]

        if len(X) < 30:
            raise ValueError(f'only {len(X)} valid rows after cleaning')

        print(f'[ml] [{sat.name}] Training neural autoencoder on {len(X)} frames…')
        model, scaler, t_warn, t_fault, _ = train_autoencoder(X)

        export_tflite(model, scaler, prefix, X)

        meta = {
            'trained_at':      trained_at,
            'satellite':       sat.name,
            'mac':             sat.mac_hex,
            'n_samples':       len(X),
            'contamination':   0.05,
            'threshold_warn':  t_warn,
            'threshold_fault': t_fault,
            'model_type':      'autoencoder_tflite',
        }
        with open(prefix + '_meta.json', 'w') as mf:
            json.dump(meta, mf, indent=2)

        model_dict = load_npu_model(prefix)
        if model_dict:
            with _sat_models_lock:
                _sat_models[sat.mac_hex] = model_dict
            with _sat_lock:
                sat.ml_trained    = True
                sat.ml_trained_at = trained_at
                sat.ml_training   = False
                sat.ml_backend    = model_dict.get('backend', 'TFLite')
            print(f'[ml] [{sat.name}] Autoencoder trained + NPU loaded  '
                  f'backend={model_dict["backend"]}  '
                  f'WARN≥{t_warn:.4f}  FAULT≥{t_fault:.4f}')
            return

    except ImportError:
        print(f'[ml] [{sat.name}] TensorFlow not available — falling back to IsolationForest')
    except Exception as e:
        import traceback
        print(f'[ml] [{sat.name}] Autoencoder training failed: {e} — falling back to IsolationForest')
        print(traceback.format_exc())

    # ── 2. IsolationForest fallback (CPU — no TF required) ────────────────────
    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import StandardScaler
        import joblib as _jl

        X_raw    = np.array([[f.get(k, 0.0) for k in _TRAIN_FEATS] for f in buf],
                            dtype=np.float64)
        log_kurt = np.log1p(np.clip(X_raw[:, 2], 0, None))
        log_z    = np.log1p(np.clip(X_raw[:, 6], 0, None))
        X        = np.column_stack([X_raw, log_kurt, log_z])
        feat_cols = _TRAIN_FEATS + ['log_kurtosis', 'log_z']

        scaler = StandardScaler()
        X_s    = scaler.fit_transform(X)

        iso = IsolationForest(
            n_estimators=200, contamination=0.05,
            max_samples=min(512, len(X_s)),
            random_state=42, n_jobs=-1,
        )
        iso.fit(X_s)
        scores  = iso.decision_function(X_s)
        t_warn  = float(np.percentile(scores, 5.0))
        t_fault = float(np.percentile(scores, 1.67))

        bundle_path = prefix + '_iso.joblib'
        meta_path   = prefix + '_meta.json'

        _jl.dump({'scaler': scaler, 'model': iso}, bundle_path, compress=3)
        meta = {
            'trained_at':      trained_at,
            'satellite':       sat.name,
            'mac':             sat.mac_hex,
            'n_samples':       len(X_raw),
            'contamination':   0.05,
            'n_estimators':    200,
            'feature_cols':    feat_cols,
            'base_features':   _TRAIN_FEATS,
            'threshold_warn':  t_warn,
            'threshold_fault': t_fault,
            'model_type':      'isolation_forest',
        }
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

        with _sat_models_lock:
            _sat_models[sat.mac_hex] = {
                'type':      'isolation_forest',
                'scaler':    scaler,
                'model':     iso,
                'feat_cols': feat_cols,
                't_warn':    t_warn,
                't_fault':   t_fault,
            }
        with _sat_lock:
            sat.ml_trained    = True
            sat.ml_trained_at = trained_at
            sat.ml_training   = False
            sat.ml_backend    = 'IsolationForest (CPU)'

        print(f'[ml] [{sat.name}] IsolationForest trained: {len(X_raw)} samples  '
              f'WARN≤{t_warn:.4f}  FAULT≤{t_fault:.4f}  → {bundle_path}')
    except Exception as e:
        import traceback
        print(f'[ml] [{sat.name}] All training paths failed: {e}\n{traceback.format_exc()}')
        with _sat_lock:
            sat.ml_training = False


# ─── Accept loop ─────────────────────────────────────────────────────────────

def accept_loop(host, port, fft_mic_n, fft_imu_n):
    exp_mic = fft_mic_n // 2
    exp_imu = fft_imu_n // 2

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    print(f"[server] Listening on {host}:{port}  "
          f"(mic={fft_mic_n}-pt  imu={fft_imu_n}-pt × 3 axes)  up to 16 satellites")

    while True:
        conn, addr = srv.accept()
        threading.Thread(
            target=satellite_thread,
            args=(conn, addr, exp_mic, exp_imu),
            daemon=True,
            name=f"sat-{addr[0]}",
        ).start()


# ─── Live plot ────────────────────────────────────────────────────────────────

def run_plot(fft_mic_n, fft_imu_n, mic_fs=16000, imu_fs=25600, shaft_hz=None,
             bearing_freqs_mic=None, bearing_freqs_imu=None):
    mic_bins  = fft_mic_n // 2
    imu_bins  = fft_imu_n // 2
    mic_freqs = np.linspace(0, mic_fs / 2, mic_bins)
    imu_freqs = np.linspace(0, imu_fs / 2, imu_bins)

    # Per-satellite history — keyed by satellite name so alternating frames
    # from different satellites don't clear each other's accumulated history.
    _sat_crest_mic: dict = {}
    _sat_crest_imu: dict = {}
    _sat_kurt_mic:  dict = {}
    _sat_wf_buf:    dict = {}

    def _ensure_sat_history(name):
        if name not in _sat_crest_mic:
            _sat_crest_mic[name] = collections.deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
            _sat_crest_imu[name] = collections.deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
            _sat_kurt_mic[name]  = collections.deque([3.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
            _sat_wf_buf[name]    = np.full((WATERFALL_ROWS, mic_bins), -120.0, dtype=np.float32)

    # Waterfall: rows=time (newest at top), cols=frequency bins
    wf_buf = np.full((WATERFALL_ROWS, mic_bins), -120.0, dtype=np.float32)

    plt.ion()
    fig = plt.figure(figsize=(14, 13))
    fig.patch.set_facecolor('#0d0d0d')

    # Layout: 4 rows × 2 cols
    #  Row 0: MIC FFT (left)      | IMU X radial (right)
    #  Row 1: MIC Waterfall (full width, spans both cols)
    #  Row 2: IMU Y radial (left) | IMU Z axial (right)
    #  Row 3: Crest & Kurtosis history (full width)
    gs = gridspec.GridSpec(4, 2, figure=fig,
                           height_ratios=[1.0, 0.65, 1.0, 0.65],
                           hspace=0.52, wspace=0.3)

    ax_mic = fig.add_subplot(gs[0, 0])
    ax_x   = fig.add_subplot(gs[0, 1])
    ax_wf  = fig.add_subplot(gs[1, :])   # waterfall spans full width
    ax_y   = fig.add_subplot(gs[2, 0])
    ax_z   = fig.add_subplot(gs[2, 1])
    ax_cr  = fig.add_subplot(gs[3, :])

    def _style(ax, grid=True):
        ax.set_facecolor('#111111')
        ax.tick_params(colors='#aaaaaa', labelsize=7)
        for sp in ax.spines.values():
            sp.set_edgecolor('#333333')
        if grid:
            ax.grid(True, alpha=0.15, color='gray')

    for ax in (ax_mic, ax_x, ax_y, ax_z, ax_cr):
        _style(ax)
    _style(ax_wf, grid=False)  # waterfall has no grid

    def _fft_panel(ax, freqs, color, title, fs, bearing_freqs=None):
        (line,) = ax.plot(freqs, np.full(len(freqs), -130.0), lw=0.8, color=color)
        ax.set_xlim(0, fs / 2)
        ax.set_ylim(-130, 10)
        ax.set_ylabel('dBFS', color='#aaaaaa', fontsize=7)
        ax.set_xlabel('Hz',   color='#aaaaaa', fontsize=7)
        ax.set_title(title,   color='white',   fontsize=8)
        if shaft_hz and shaft_hz > 0:
            for h in range(1, 11):
                f = shaft_hz * h
                if f < fs / 2:
                    ax.axvline(f, color='#ffff44', alpha=0.3, lw=0.6, ls='--')
        # Bearing fault frequency markers (colored vertical lines + labels)
        if bearing_freqs:
            _DFLT_C = '#aaaaaa'
            for label, freq in bearing_freqs.items():
                if 0 < freq < fs / 2:
                    c = MARKER_COLORS.get(label, _DFLT_C)
                    ax.axvline(freq, color=c, alpha=0.55, lw=0.9, ls='-.')
                    ax.text(freq + fs / 2 * 0.005, 5, label,
                            color=c, fontsize=5, rotation=90,
                            va='top', ha='left', alpha=0.85)
        return line

    line_mic = _fft_panel(ax_mic, mic_freqs, 'cyan',
                          f'MIC FFT  {fft_mic_n}-pt  {mic_fs//1000} kHz', mic_fs,
                          bearing_freqs=bearing_freqs_mic)
    line_x   = _fft_panel(ax_x, imu_freqs, '#ff7f0e',
                          f'IMU X  radial  {fft_imu_n}-pt  {imu_fs//1000} kHz', imu_fs,
                          bearing_freqs=bearing_freqs_imu)
    line_y   = _fft_panel(ax_y, imu_freqs, '#2ca02c',
                          f'IMU Y  radial  {fft_imu_n}-pt  {imu_fs//1000} kHz', imu_fs,
                          bearing_freqs=bearing_freqs_imu)
    line_z   = _fft_panel(ax_z, imu_freqs, '#d62728',
                          f'IMU Z  axial   {fft_imu_n}-pt  {imu_fs//1000} kHz', imu_fs,
                          bearing_freqs=bearing_freqs_imu)

    # Stub signal verification markers
    for f, ax in ((50, ax_x), (50, ax_y), (150, ax_y), (100, ax_z)):
        ax.axvline(f, color='white', alpha=0.35, lw=0.6, ls=':')

    # ── Waterfall ─────────────────────────────────────────────────────────────
    frame_period_s = 1.0 / 2.2            # ~0.45 s per frame
    wf_duration_s  = WATERFALL_ROWS * frame_period_s
    img_wf = ax_wf.imshow(
        wf_buf,
        aspect='auto',
        origin='upper',                    # row 0 = newest (top)
        extent=[0, mic_fs / 2, wf_duration_s, 0],
        vmin=-120, vmax=-50,
        cmap='inferno',
        interpolation='nearest',
    )
    cbar = plt.colorbar(img_wf, ax=ax_wf, fraction=0.015, pad=0.01)
    cbar.set_label('dBFS', color='#aaaaaa', fontsize=7)
    cbar.ax.tick_params(colors='#aaaaaa', labelsize=7)
    ax_wf.set_xlabel('Hz',         color='#aaaaaa', fontsize=7)
    ax_wf.set_ylabel('Time (s) ↓', color='#aaaaaa', fontsize=7)
    ax_wf.set_title(
        f'MIC Waterfall  —  last {WATERFALL_ROWS} frames  (~{wf_duration_s:.0f} s, newest at top)',
        color='white', fontsize=8)
    ax_wf.tick_params(colors='#aaaaaa', labelsize=7)
    if shaft_hz and shaft_hz > 0:
        for h in range(1, 11):
            f = shaft_hz * h
            if f < mic_fs / 2:
                ax_wf.axvline(f, color='#ffff44', alpha=0.25, lw=0.8, ls='--')

    # ── Crest & kurtosis history ───────────────────────────────────────────────
    xc = np.arange(HISTORY_LEN)
    lc_mic,  = ax_cr.plot(xc, [0.0] * HISTORY_LEN, lw=1.0, color='cyan',    label='MIC crest')
    lc_imu,  = ax_cr.plot(xc, [0.0] * HISTORY_LEN, lw=1.0, color='#ff7f0e', label='IMU crest')
    lc_kurt, = ax_cr.plot(xc, [3.0] * HISTORY_LEN, lw=1.2, color='#aa44ff', label='MIC kurtosis/3')
    ax_cr.axhline(CREST_WARN,  color='yellow', ls='--', lw=0.8, alpha=0.8,
                  label=f'Warn {CREST_WARN}')
    ax_cr.axhline(CREST_FAULT, color='red',    ls='--', lw=0.8, alpha=0.8,
                  label=f'Fault {CREST_FAULT}')
    ax_cr.set_ylim(0, 10)
    ax_cr.set_xlim(0, HISTORY_LEN - 1)
    ax_cr.set_ylabel('Factor', color='#aaaaaa', fontsize=7)
    ax_cr.set_xlabel(f'Last {HISTORY_LEN} frames', color='#aaaaaa', fontsize=7)
    ax_cr.set_title('Crest & Kurtosis History — impulsive fault indicators (kurtosis÷3 scaled)',
                    color='white', fontsize=8)
    ax_cr.legend(ncol=5, loc='upper right', fontsize=7,
                 facecolor='#1a1a1a', edgecolor='#444', labelcolor='white')

    title_t = fig.suptitle('EPM Live Monitor — waiting for satellite…',
                            color='white', fontsize=9)
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.show()

    last_id  = -1

    while plt.fignum_exists(fig.number):
        _display.wait(timeout=0.3)
        frame, satname = _display.get()
        if frame is None or frame['frame_id'] == last_id:
            plt.pause(0.05)
            continue
        last_id = frame['frame_id']

        _ensure_sat_history(satname)
        crest_mic = _sat_crest_mic[satname]
        crest_imu = _sat_crest_imu[satname]
        kurt_mic  = _sat_kurt_mic[satname]
        wf_buf    = _sat_wf_buf[satname]

        for line, data, ax in (
            (line_mic, frame['mic_fft'], ax_mic),
            (line_x,   frame['imu_x'],  ax_x),
            (line_y,   frame['imu_y'],  ax_y),
            (line_z,   frame['imu_z'],  ax_z),
        ):
            if len(data) == len(line.get_xdata()):
                line.set_ydata(data)
                lo = max(float(np.min(data)) - 5, -130)
                hi = min(float(np.max(data)) + 5,   10)
                ax.set_ylim(lo, hi)

        # Waterfall: roll buffer down (row 0 = newest) and insert latest mic FFT
        mic_data = np.array(frame['mic_fft'], dtype=np.float32)
        if len(mic_data) == mic_bins:
            wf_buf[1:] = wf_buf[:-1]
            wf_buf[0]  = mic_data
            img_wf.set_data(wf_buf)
            # Auto-adjust colour range to live signal floor/peak
            sig_min = float(np.percentile(mic_data, 5))
            sig_max = float(np.percentile(mic_data, 99))
            img_wf.set_clim(vmin=max(sig_min - 5, -130), vmax=min(sig_max + 5, 0))

        crest_mic.append(float(frame['mic_crest']))
        crest_imu.append(float(frame['imu_crest']))
        kurt_mic.append(float(frame['mic_kurtosis']) / 3.0)
        lc_mic.set_ydata(list(crest_mic))
        lc_imu.set_ydata(list(crest_imu))
        lc_kurt.set_ydata(list(kurt_mic))

        n_conn = _sat_count()
        status = 'OK' if not frame['errors'] else 'WARN:' + ';'.join(frame['errors'])
        hb = frame.get('high_band_ratio', 0.0)
        title_t.set_text(
            f"EPM  [{satname}]  frame={frame['frame_id']}  "
            f"rms={frame['mic_rms']:.5f}  "
            f"K={frame['mic_kurtosis']:.2f}  "
            f"CF={frame['mic_crest']:.2f}  "
            f"HB={hb:.2f}  "
            f"{status}  (sat:{n_conn})"
        )

        fig.canvas.draw_idle()
        plt.pause(0.05)


# ─── Web dashboard / reports ─────────────────────────────────────────────────
# _DASHBOARD_HTML, _sat_health, _safe_f, _ab_summary, _top_contribs,
# _build_status_json, _DashHandler, start_dashboard are imported above from
# gateway.api.dashboard; _generate_report_html is imported above from
# gateway.api.reports (Phase 8b2 task 1).


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    global CREST_WARN, CREST_FAULT, _NOTIFY_WEBHOOK, _NOTIFY_EMAIL_CFG
    global _AUTH_USER, _AUTH_PASS, _FACTORY_NAME
    global _FAULT_PRIOR, _EVIDENCE_Z_MID, _bayesian_fusion
    global Z_WARN_SIGMA, Z_FAULT_SIGMA, _storage
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port',      type=int,   default=5100)
    parser.add_argument('--listen-ip', type=str,   default='0.0.0.0')
    parser.add_argument('--fft-mic-n', type=int,   default=1024)
    parser.add_argument('--fft-imu-n', type=int,   default=2048)
    parser.add_argument('--shaft-hz',   type=float, default=None,
                        help='Shaft frequency Hz — marks harmonics on all FFT panels')
    parser.add_argument('--shaft-rpm',  type=float, default=None,
                        help='Shaft speed RPM — alternative to --shaft-hz')
    parser.add_argument('--bearing',    type=str,   default=None,
                        help='Bearing type for fault freq markers: e.g. 6205 or n,D,d[,alpha]. '
                             'Requires --shaft-hz or --shaft-rpm. '
                             'Run: python bearing_math.py --list')
    parser.add_argument('--model',      type=str,   default=None,
                        help='ML model prefix from ml_trainer.py (e.g. model/epm_model). '
                             'Enables ML-based alerting alongside threshold detection.')
    parser.add_argument('--crest-warn',  type=float, default=None,
                        help=f'Crest factor WARN threshold (default {CREST_WARN})')
    parser.add_argument('--crest-fault', type=float, default=None,
                        help=f'Crest factor FAULT threshold (default {CREST_FAULT})')
    parser.add_argument('--dashboard-port', type=int, default=8080,
                        help='HTTP port for the web dashboard (default 8080)')
    parser.add_argument('--no-plot', action='store_true',
                        help='Skip the live matplotlib plot — for SSH / headless / '
                             'Uno Q / server environments with no display')
    parser.add_argument('--auth', type=str, default=None, metavar='USER:PASS',
                        help='Protect dashboard with HTTP Basic Auth (e.g. admin:secret). '
                             'Required for production deployments.')
    parser.add_argument('--notify-webhook', type=str, default=None, metavar='URL',
                        help='Webhook URL for FAULT alerts — supports Discord, Slack, Teams, '
                             'or any generic JSON endpoint.')
    parser.add_argument('--notify-email', type=str, default=None,
                        metavar='FROM:TO:HOST[:PORT[:USER:PASS]]',
                        help='SMTP config for email FAULT alerts (colon-separated). '
                             'Example: alerts@co.com:ops@co.com:smtp.co.com:587:user:pass')
    parser.add_argument('--factory-name', type=str, default=None,
                        help='Site/factory name shown in the dashboard header '
                             '(default: "EPM Industrial Monitor")')
    parser.add_argument('--fault-prior', type=float, default=0.01,
                        help='Bayesian prior P(fault per frame) for multi-channel '
                             'fusion (default 0.01). Raise to 0.05 for noisier sites.')
    parser.add_argument('--evidence-midpoint', type=float, default=_EVIDENCE_Z_MID,
                        help='Z-score at which a channel contributes 50/50 fault '
                             'evidence (default %.1f — Phase 3 sweep optimum).' % _EVIDENCE_Z_MID)
    parser.add_argument('--threshold-sigma', type=float, default=4.0,
                        help='Adaptive z-sigma threshold for WARN from per-machine baseline '
                             '(default 4.0; FAULT is set to this value + 2.0). '
                             'Lower values increase sensitivity on quiet machines.')
    parser.add_argument('--psk-hex', type=str, default=None,
                        metavar='HEX32',
                        help='32-character hex AES-128 key for frame decryption '
                             '(e.g. deadbeefdeadbeefdeadbeefdeadbeef). '
                             'Must match EPM_PSK in wifi_creds.h or the NVS key on satellites. '
                             'Also readable from the EPM_PSK env-var. '
                             'Omit to run in plaintext mode (dev/debug only).')
    parser.add_argument('--autoencoder', type=str, default=None,
                        metavar='PATH',
                        help='Path to ONNX autoencoder model (e.g. model/autoencoder.onnx). '
                             'Adds reconstruction-error as a 4th Bayesian fusion channel. '
                             'Stats sidecar <model>_stats.npz must exist alongside the model. '
                             'Omit to run without neural autoencoder channel.')
    parser.add_argument('--mqtt-host', type=str, default=None,
                        help='Broker host for the MQTT section-list ingestion path '
                             '(Phase 8a) -- e.g. 192.168.1.8. Runs alongside the existing '
                             'TCP+AES receiver (does not replace it); omit to disable. '
                             'See mqtt_ingest.py and ADR-027 for the satellite-identity '
                             'and imu_rms/imu_crest derivation this path uses.')
    parser.add_argument('--mqtt-port', type=int, default=1883,
                        help='Broker port for --mqtt-host (default 1883)')
    args = parser.parse_args()

    # Port-in-use guard — prevents silent conflicts with orphaned gateway instances.
    # SO_REUSEADDR would let a new instance bind but the old process keeps accepting
    # connections first, causing partial/lost frames. Fail fast instead.
    _guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _guard.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        _guard.bind((args.listen_ip, args.port))
        _guard.close()
    except OSError as _e:
        print(f'[ERROR] Port {args.port} is already in use: {_e}')
        print(f'        Find & kill the old process:')
        print(f'          netstat -ano | findstr :{args.port}')
        print(f'          Stop-Process -Id <PID> -Force')
        sys.exit(1)

    if args.crest_warn is not None:
        CREST_WARN = args.crest_warn
    if args.crest_fault is not None:
        CREST_FAULT = args.crest_fault
    _FAULT_PRIOR    = args.fault_prior
    _EVIDENCE_Z_MID = args.evidence_midpoint
    Z_WARN_SIGMA    = args.threshold_sigma
    Z_FAULT_SIGMA   = args.threshold_sigma + 2.0
    if _FUSION_AVAILABLE:
        _bayesian_fusion = BayesianFusion(
            prior=_FAULT_PRIOR, z_mid=_EVIDENCE_Z_MID, temperature=1.0)

    # ── Autoencoder ONNX model ─────────────────────────────────────────────────
    global _ae_engine, _ae_stats
    if args.autoencoder is not None:
        if not _AE_AVAILABLE:
            sys.exit('[EPM] --autoencoder requires onnxruntime. '
                     'Run: pip install onnxruntime>=1.17.0')
        stats_path = args.autoencoder.replace('.onnx', '_stats.npz')
        if not os.path.exists(stats_path):
            sys.exit(f'[EPM] Stats sidecar not found: {stats_path}\n'
                     '      Run train_autoencoder.py to generate it alongside the model.')
        _ae_engine = InferenceEngine(args.autoencoder)
        _ae_stats  = np.load(stats_path)
        print(f'[EPM] Autoencoder: {args.autoencoder}')
        print(f'[EPM]   healthy mean_recon_err={float(_ae_stats["mean_recon_err"]):.6f}'
              f'  backend={_ae_engine.backend_label}')

    # ── Auth ──────────────────────────────────────────────────────────────────
    if args.auth:
        if ':' not in args.auth:
            sys.exit('--auth must be USER:PASS (e.g. admin:secret)')
        _AUTH_USER, _AUTH_PASS = args.auth.split(':', 1)

    # ── Notifications ─────────────────────────────────────────────────────────
    if args.notify_webhook:
        _NOTIFY_WEBHOOK = args.notify_webhook

    if args.notify_email:
        parts = args.notify_email.split(':')
        if len(parts) < 3:
            sys.exit('--notify-email must be FROM:TO:HOST[:PORT[:USER:PASS]]')
        _NOTIFY_EMAIL_CFG = {
            'from': parts[0],
            'to':   parts[1],
            'host': parts[2],
            'port': int(parts[3]) if len(parts) > 3 else 587,
            'user': parts[4] if len(parts) > 4 else None,
            'pass': parts[5] if len(parts) > 5 else None,
        }

    # ── Factory name ──────────────────────────────────────────────────────────
    if args.factory_name:
        _FACTORY_NAME = args.factory_name

    for n, name in ((args.fft_mic_n, 'fft-mic-n'), (args.fft_imu_n, 'fft-imu-n')):
        if n <= 0 or (n & (n - 1)):
            sys.exit(f"--{name} must be a power of 2 (got {n})")

    # Resolve shaft_hz from either --shaft-hz or --shaft-rpm
    shaft_hz = args.shaft_hz
    if args.shaft_rpm is not None and shaft_hz is None:
        shaft_hz = args.shaft_rpm / 60.0

    # Parse bearing geometry and compute fault frequencies for FFT annotation
    bearing_freqs_mic = None
    bearing_freqs_imu = None
    if args.bearing:
        if not _BEARING_AVAILABLE:
            print('WARNING: bearing_math.py not found in the same directory — ignoring --bearing')
        elif shaft_hz is None:
            print('WARNING: --bearing requires --shaft-hz or --shaft-rpm — ignoring')
        else:
            geom = parse_bearing_arg(args.bearing)
            if geom is None:
                print(f'WARNING: unknown bearing "{args.bearing}" — ignoring. '
                      f'Run: python bearing_math.py --list')
            else:
                bf = BearingFreqs.from_shaft_hz(shaft_hz, geom)
                bf.print_table()
                bearing_freqs_mic = bf.markers(MIC_FS_HZ)
                bearing_freqs_imu = bf.markers(IMU_FS_HZ)

    # Load ML model if requested
    if args.model:
        _load_ml_model(args.model)

    # ── SQLite storage ────────────────────────────────────────────────────────
    global _storage
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    if _STORAGE_AVAILABLE:
        try:
            _storage = Storage(os.path.join(log_dir, 'epm.db'))
            print(f'[storage] SQLite DB: {os.path.join(log_dir, "epm.db")}  (WAL mode)')
        except Exception as e:
            print(f'[storage] WARNING: could not open DB: {e} — using JSON fallback')

    # ── Load maintenance log (from SQLite or legacy JSON) ─────────────────────
    _load_maint_log(os.path.join(log_dir, 'maintenance_log.json'))

    # ── CSV rotation background thread — gzip files older than 90 days ────────
    if _STORAGE_AVAILABLE and rotate_old_csvs is not None:
        def _csv_rotation_loop():
            csv_root = os.path.join(log_dir, 'csv')
            while True:
                time.sleep(3600)   # check hourly
                try:
                    n = rotate_old_csvs(csv_root, max_age_days=90)
                    if n:
                        print(f'[csv-rotation] Gzipped {n} CSV file(s) older than 90 days')
                except Exception as e:
                    print(f'[csv-rotation] Error: {e}')
        threading.Thread(target=_csv_rotation_loop, daemon=True,
                         name='csv-rotation').start()

    # ── AES-128-GCM frame decryption ──────────────────────────────────────────
    global _decryptor
    psk_hex = args.psk_hex or os.environ.get('EPM_PSK')
    if psk_hex:
        if not _CRYPTO_AVAILABLE:
            sys.exit('[crypto] ERROR: --psk-hex requires the cryptography package: '
                     'pip install "cryptography>=42.0.0"')
        psk_hex = psk_hex.strip()
        if len(psk_hex) != 32 or not all(c in '0123456789abcdefABCDEF' for c in psk_hex):
            sys.exit(f'[crypto] ERROR: --psk-hex must be exactly 32 hex characters (got {len(psk_hex)})')
        _decryptor = FrameDecryptor(bytes.fromhex(psk_hex))
        print(f'[crypto] AES-128-GCM decryption enabled — PSK: {psk_hex[:8]}...{psk_hex[-4:]} '
              f'(all {len(psk_hex)//2} bytes)')
    else:
        print('[crypto] Plaintext mode — pass --psk-hex or set EPM_PSK env-var to enable '
              'AES-128-GCM encryption (required for production)')

    # ── mDNS service advertisement ─────────────────────────────────────────────
    global _zc_instance
    if _MDNS_AVAILABLE:
        try:
            my_ip = get_local_ip()
            # Bind Zeroconf to the specific interface IP so it targets the correct
            # subnet (hotspot 192.168.137.x) instead of probing all interfaces.
            try:
                from zeroconf import InterfaceChoice
                zc = Zeroconf(interfaces=[my_ip])
            except (ImportError, TypeError):
                zc = Zeroconf()
            info  = ServiceInfo(
                type_     = '_epm-gateway._tcp.local.',
                name      = 'EPM-Gateway._epm-gateway._tcp.local.',
                addresses = [socket.inet_aton(my_ip)],
                port      = args.port,
                properties= {'version': '2.0', 'factory': _FACTORY_NAME},
                server    = 'epm-gateway.local.',
            )
            zc.register_service(info)
            _zc_instance = zc
            print(f'[mDNS] Advertised: epm-gateway.local:{args.port} -> {my_ip}')
            print('[mDNS] Satellites will auto-discover the gateway (SERVER_IP becomes optional)')
        except Exception as e:
            print(f'[mDNS] WARNING: registration failed ({type(e).__name__}: {e}) — satellites must use static SERVER_IP')
    else:
        print('[mDNS] zeroconf not installed — satellites must use static SERVER_IP. '
              'Install: pip install "zeroconf>=0.131.0"')

    print("EPM gateway — multi-satellite predictive maintenance receiver")
    print(f"Factory: {_FACTORY_NAME}")
    print(f"Expecting: mic={args.fft_mic_n}-pt  imu={args.fft_imu_n}-pt × 3 axes")
    if shaft_hz:
        print(f"Shaft: {shaft_hz:.3f} Hz  ({shaft_hz*60:.0f} RPM)")
    if bearing_freqs_mic:
        print(f"Bearing: {geom.name}  BPFO={bearing_freqs_mic.get('BPFO', 0):.1f} Hz  "
              f"BPFI={bearing_freqs_mic.get('BPFI', 0):.1f} Hz")
    if _ML_MODEL:
        print(f"ML alerting: active")
    if _HST_AVAILABLE:
        print(f"[EPM] OnlineDetector: river HalfSpaceTrees, fully on-device.")
        print(f"[EPM]   - n_trees=10 height=15 window=250 features={FEATURE_DIM}")
        print(f"[EPM]   - No network calls, no telemetry, no cloud dependencies.")
        print(f"[EPM]   - To verify: tcpdump -i any not port 22 and not port 5100 and not port 8080")
    else:
        print("[EPM] OnlineDetector: river not installed — HST scoring disabled.")
        print("[EPM]   Install: pip install 'river>=0.21.0'")
    if _FUSION_AVAILABLE:
        print(f"[EPM] BayesianFusion: multi-channel posterior P(fault|evidence).")
        print(f"[EPM]   prior={_FAULT_PRIOR:.3f}  z_mid={_EVIDENCE_Z_MID:.1f}  "
              f"WARN@p>{P_FUSION_WARN:.0%}  FAULT@p>{P_FUSION_FAULT:.0%}")
        _ae_ch = ' + z_ae (autoencoder)' if _ae_engine is not None else ''
        print(f"[EPM]   Channels: z_kurtosis, z_rms (+ z_hst when HST warmed up){_ae_ch}")
    else:
        print("[EPM] BayesianFusion: bayesian_fusion.py not found — multi-channel fusion disabled.")
    print("Firewall rule for TCP receiver (elevated PowerShell, run once):")
    print(f"  New-NetFirewallRule -DisplayName EPM-{args.port} -Direction Inbound "
          f"-Protocol TCP -LocalPort {args.port} -Action Allow")

    start_dashboard(args.dashboard_port)

    threading.Thread(
        target=accept_loop,
        args=(args.listen_ip, args.port, args.fft_mic_n, args.fft_imu_n),
        daemon=True,
    ).start()

    # ── MQTT section-list ingestion (Phase 8a) — additive, off by default ────
    if args.mqtt_host:
        import mqtt_ingest
        mqtt_ingest.start(sys.modules[__name__], args.mqtt_host, args.mqtt_port)
        print(f"[mqtt] Ingesting {mqtt_ingest.DATA_TOPIC_FILTER} from "
              f"{args.mqtt_host}:{args.mqtt_port}")

    if args.no_plot:
        print("[plot] --no-plot: running headless (TCP receiver + dashboard only)")
        print("[plot] Dashboard: http://localhost:{}/".format(args.dashboard_port))
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nExiting.")
    else:
        try:
            run_plot(args.fft_mic_n, args.fft_imu_n, shaft_hz=shaft_hz,
                     bearing_freqs_mic=bearing_freqs_mic,
                     bearing_freqs_imu=bearing_freqs_imu)
        except KeyboardInterrupt:
            print("\nExiting.")


if __name__ == '__main__':
    main()
