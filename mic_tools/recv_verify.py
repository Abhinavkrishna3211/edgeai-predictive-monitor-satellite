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
from gateway.pipeline.alerting import (
    _band_ratios, _extract_hst_features, _classify_fault_type, compute_alert,
)
from gateway.pipeline.ml_scoring import (
    _load_ml_model, _ml_score, _ml_score_with, _ml_score_tflite,
    _try_load_sat_model, _try_load_hst_state, _save_hst_state,
    _trigger_sat_training, _train_sat_model_bg,
)

# AES-128-GCM frame decryption (FrameDecryptor, _CRYPTO_AVAILABLE, AESGCM) moved
# to gateway/ingestion/tcp_legacy.py in Phase 8b3 task 1 along with the rest of
# the TCP-legacy receiver — nothing in this file needs it anymore.

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
# TCP-wire-format constants (EPM_MAGIC, HELLO_MAGIC, HEADER_FMT, HELLO_FMT, ...)
# moved to gateway/ingestion/tcp_legacy.py in Phase 8b3 task 1 — EPM_ALERT_OK/
# WARN/FAULT below are transport-agnostic (used by the shared
# _process_satellite_frame and by both ingestion paths) and stay here.

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

MIC_FS_HZ = 48000
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
# gateway.pipeline.adaptive_control (Phase 8b1 task 3). EPM_PROTO_V2_MAGIC moved
# to gateway/ingestion/tcp_legacy.py alongside the reply-packing code that uses
# it (Phase 8b3 task 1).


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


# recv_exact/parse_frame are now in gateway/ingestion/tcp_legacy.py, alongside
# the wire-format constants (EPM_MAGIC, HEADER_FMT, ...) they depend on
# (Phase 8b3 task 1).

# _band_ratios, _high_band_ratio, _spectral_centroid, _extract_hst_features,
# _classify_fault_type, compute_alert are imported above from
# gateway.pipeline.alerting (Phase 8b2 task 2).

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


# satellite_thread/accept_loop are now in gateway/ingestion/tcp_legacy.py
# (Phase 8b3 task 1), reaching this file's _process_satellite_frame,
# _log_security_event, and shared state via a lazy `import recv_verify as _rv`.

# _load_ml_model, _ml_score, _ml_score_with, _ml_score_tflite,
# _try_load_sat_model, _try_load_hst_state, _save_hst_state,
# _trigger_sat_training, _train_sat_model_bg are imported above from
# gateway.pipeline.ml_scoring (Phase 8b2 task 3).

# ─── Live plot ────────────────────────────────────────────────────────────────
# run_plot() moved to gateway/api/live_plot.py (Phase 8c task 1). It reaches
# this module's CREST_WARN/CREST_FAULT/HISTORY_LEN/WATERFALL_ROWS/MARKER_COLORS/
# _display via a lazy `import recv_verify as _rv`, same as every other
# gateway.api/gateway.pipeline module.

# ─── Web dashboard / reports ─────────────────────────────────────────────────
# _DASHBOARD_HTML, _sat_health, _safe_f, _ab_summary, _top_contribs,
# _build_status_json, _DashHandler, start_dashboard are imported above from
# gateway.api.dashboard; _generate_report_html is imported above from
# gateway.api.reports (Phase 8b2 task 1).


# ─── Entry point ─────────────────────────────────────────────────────────────
# Argument parsing + startup wiring moved to gateway/main.py (Phase 8b3 task 2).
# This stays a one-line forwarding shim rather than being retired outright so
# `python recv_verify.py ...` (direct invocation, existing muscle-memory/docs)
# and `import recv_verify; recv_verify.main()` (mic_tools/docker_start.py) both
# keep working unchanged. See
# docs/decisions/ADR-029-recv-verify-fate-and-main-py-split.md.

def main():
    from gateway.main import main as _main
    _main()


if __name__ == '__main__':
    main()
