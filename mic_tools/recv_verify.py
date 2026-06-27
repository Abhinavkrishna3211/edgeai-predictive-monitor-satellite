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
import base64
import collections
import csv
import datetime
import glob
import json
import os
import smtplib
import socket
import struct
import math
import sys
import threading
import time
import urllib.parse
import urllib.request as _urllib_req
from email.mime.text import MIMEText
from http.server import HTTPServer, BaseHTTPRequestHandler

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

# Optional: bearing fault frequency analysis (bearing_math.py in same directory)
MARKER_COLORS: dict = {}   # populated below if bearing_math is importable
try:
    from bearing_math import BearingFreqs, parse_bearing_arg
    from bearing_math import MARKER_COLORS as MARKER_COLORS  # re-bind module-level name
    _BEARING_AVAILABLE = True
except ImportError:
    _BEARING_AVAILABLE = False

# Optional: ML inference (scikit-learn — install with: pip install scikit-learn joblib)
_ML_MODEL = None   # populated by _load_ml_model() if --model is given

# ─── Alert history (for compliance audit trail) ───────────────────────────────
_ALERT_HISTORY      = collections.deque(maxlen=1000)
_ALERT_HISTORY_LOCK = threading.Lock()

# ─── Maintenance log (persisted to logs/maintenance_log.json) ─────────────────
_MAINT_LOG          = {}     # mac_hex → maintenance record dict
_MAINT_LOG_LOCK     = threading.Lock()
_MAINT_LOG_PATH     = None   # set at startup

# ─── Notifications ────────────────────────────────────────────────────────────
_NOTIFY_WEBHOOK     = None   # set by --notify-webhook
_NOTIFY_EMAIL_CFG   = None   # set by --notify-email
_NOTIFY_COOLDOWN    = {}     # mac_hex → epoch of last notification sent
_NOTIFY_COOLDOWN_S  = 300    # 5 min minimum between alerts per satellite

# ─── Auth ─────────────────────────────────────────────────────────────────────
_AUTH_USER          = None   # set by --auth user:pass
_AUTH_PASS          = None

# ─── Branding ─────────────────────────────────────────────────────────────────
_FACTORY_NAME       = 'EPM Industrial Monitor'  # set by --factory-name

# ─── Protocol constants ───────────────────────────────────────────────────────

EPM_MAGIC   = 0xEA1DF00D
HELLO_MAGIC = 0xEA1D0000

HEADER_FMT  = '<IIIHHffffBfffBBx'   # 48 bytes — added mic_kurtosis float
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
CAL_FRAMES  = 30    # frames to collect for Z-score baseline
HISTORY_LEN    = 200   # ~90 s of history at 2.2 fps — Uno Q 4GB easily holds this per satellite
WATERFALL_ROWS = 120   # time rows in the mic FFT waterfall (~55 s at 2.2 fps)

# Alert persistence — prevents transient factory noise false positives
WARN_PERSIST  = 2   # consecutive WARN frames required to raise alert
CLEAR_PERSIST = 3   # consecutive OK  frames required to clear alert

# High-band energy threshold — bearing faults excite 2-8kHz resonance;
# factory noise is mostly <500Hz. Only alert if high-band carries enough energy.
HIGH_BAND_MIN  = 0.12   # 12% of total mic energy must be in 2-8kHz band

MIC_FS_HZ = 16000
IMU_FS_HZ = 25600   # KX134 ODR — must match FFT_IMU_N and epm_config.h

_SERVER_START_T = time.time()   # used by dashboard uptime counter

# ─── Satellite registry ───────────────────────────────────────────────────────

class SatelliteState:
    def __init__(self, mac_hex, name, fw_major, fw_minor, addr):
        self.mac_hex     = mac_hex
        self.name        = name
        self.fw_major    = fw_major
        self.fw_minor    = fw_minor
        self.addr        = addr
        self.connected   = True
        self.frame_count = 0
        self.connect_t   = time.time()
        self.last_t      = time.time()
        self.fps         = 0.0
        self.last_frame  = None
        self.alert       = EPM_ALERT_OK
        # Z-score adaptive baseline
        self._cal_buf    = []
        self.calibrated  = False
        self.bl_mean     = None
        self.bl_std      = None
        # Alert persistence / hysteresis
        self.warn_streak  = 0   # consecutive frames above threshold
        self.ok_streak    = 0   # consecutive frames below threshold
        self.sent_alert   = EPM_ALERT_OK  # last byte actually sent to satellite
        # Rolling FPS (last 10 frame timestamps)
        self._ts_buf     = collections.deque(maxlen=10)
        # Dashboard / maintenance tracking (cumulative — NOT reset on reconnect)
        self.warn_frames  = 0
        self.fault_frames = 0
        self.last_fault_t = None          # epoch of most recent FAULT frame
        self.last_z       = 0.0
        self.last_hb      = 0.0           # most recent high-band energy ratio
        self.fault_type   = "Normal"   # spectral fault classification label
        self.history_alerts   = collections.deque([0]   * HISTORY_LEN, maxlen=HISTORY_LEN)
        self.history_kurtosis = collections.deque([3.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
        self.history_crest    = collections.deque([3.0] * HISTORY_LEN, maxlen=HISTORY_LEN)

    def fps_str(self):
        return f"{self.fps:.1f}" if self.connected else "—"

    def rolling_fps(self, now):
        self._ts_buf.append(now)
        if len(self._ts_buf) < 2:
            return 0.0
        return (len(self._ts_buf) - 1) / max(self._ts_buf[-1] - self._ts_buf[0], 1e-3)


_sat_lock   = threading.Lock()
_satellites = {}   # mac_hex → SatelliteState


def _sat_register(mac_hex, name, fw_major, fw_minor, addr):
    with _sat_lock:
        if mac_hex in _satellites:
            sat = _satellites[mac_hex]
            sat.connected    = True
            sat.name         = name      # update name in case it changed or was corrupt
            sat.fw_major     = fw_major
            sat.fw_minor     = fw_minor
            sat.connect_t    = time.time()
            sat.frame_count  = 0
            sat.fps          = 0.0
            sat.addr         = addr
        else:
            sat = SatelliteState(mac_hex, name, fw_major, fw_minor, addr)
            _satellites[mac_hex] = sat
    return sat


def _sat_disconnect(mac_hex):
    with _sat_lock:
        if mac_hex in _satellites:
            _satellites[mac_hex].connected = False


def _sat_count():
    with _sat_lock:
        return sum(1 for s in _satellites.values() if s.connected)


def _print_sat_table():
    with _sat_lock:
        sats = list(_satellites.values())
    if not sats:
        return
    print(f"  {'NAME':<12} {'MAC':<17} {'FW':<6} {'FPS':<6} STATUS")
    print(f"  {'-'*12} {'-'*17} {'-'*6} {'-'*6} {'─'*14}")
    for s in sats:
        status    = "CONNECTED" if s.connected else "disconnected"
        alert_str = ["OK", "WARN", "FAULT"][min(s.alert, 2)] if s.connected else "—"
        print(f"  {s.name:<12} {s.mac_hex:<17} "
              f"{s.fw_major}.{s.fw_minor:<5} {s.fps_str():<6} {status}  {alert_str}")


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
     imu_axes) = struct.unpack_from(HEADER_FMT, raw, 0)

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
        errs.append(f"size {len(raw)} != expected {exp_size}")

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


def _sat_update_baseline(sat, mic_rms, mic_kurtosis):
    if sat.calibrated:
        return
    sat._cal_buf.append([mic_rms, mic_kurtosis])
    if len(sat._cal_buf) >= CAL_FRAMES:
        arr = np.array(sat._cal_buf, dtype=np.float32)
        sat.bl_mean = arr.mean(axis=0)
        sat.bl_std  = arr.std(axis=0) + 1e-6
        sat.calibrated = True
        print(f"  [{sat.name}] Baseline ready: "
              f"rms_mean={sat.bl_mean[0]:.5f}  "
              f"kurt_mean={sat.bl_mean[1]:.2f}  "
              f"kurt_std={sat.bl_std[1]:.2f}")


def compute_alert(sat, frame, warn_streak, ok_streak, sent_alert, hb):
    """Compute per-frame alert level and z-score.

    hb is the pre-computed high-band energy ratio from _band_ratios(), passed
    in to avoid a duplicate dBFS→power conversion (the caller already has it).

    Streak counters are passed in and returned so this function has no
    side-effects on sat.  All sat mutations happen in satellite_thread under
    _sat_lock, eliminating data races with the dashboard HTTP reader thread.

    Returns (alert_byte, z_score, new_warn_streak, new_ok_streak).
    The caller is responsible for updating sent_alert = returned alert_byte.
    """
    mic_kurtosis = frame['mic_kurtosis']
    mic_crest    = frame['mic_crest']
    imu_crest    = frame['imu_crest']
    mic_rms      = frame['mic_rms']

    _sat_update_baseline(sat, mic_rms, mic_kurtosis)

    # ── Z-score (active after calibration) ───────────────────────────────────
    z_score = 0.0
    if sat.calibrated:
        features = np.array([mic_rms, mic_kurtosis], dtype=np.float32)
        z_scores = np.abs(features - sat.bl_mean) / sat.bl_std
        z_score  = float(z_scores.max())

    # ── Raw alert level (before noise filter + persistence) ──────────────────
    raw = EPM_ALERT_OK
    if mic_kurtosis >= K_FAULT or z_score >= 5.0:
        raw = EPM_ALERT_FAULT
    elif mic_kurtosis >= K_WARN or z_score >= 3.0:
        raw = EPM_ALERT_WARN
    elif max(mic_crest, imu_crest) >= CREST_FAULT:
        raw = EPM_ALERT_FAULT
    elif max(mic_crest, imu_crest) >= CREST_WARN:
        raw = EPM_ALERT_WARN

    # ── Factory noise filter: only alert if high-band energy is present ───────
    # Bearing faults excite 2-8kHz; factory floor noise is mostly <500Hz.
    if raw != EPM_ALERT_OK and hb < HIGH_BAND_MIN:
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
    # Clear: need CLEAR_PERSIST consecutive OK frames to go back to OK
    elif ok_streak >= CLEAR_PERSIST:
        final = EPM_ALERT_OK
    else:
        final = sent_alert   # hold previous state during transition

    # ── Optional ML override: take the more severe of threshold vs ML ─────────
    # Only applied after calibration so the model sees representative features.
    if sat.calibrated and _ML_MODEL is not None:
        ml_frame = {
            'mic_rms':        frame['mic_rms'],
            'mic_crest':      mic_crest,
            'mic_kurtosis':   mic_kurtosis,
            'imu_rms':        frame.get('imu_rms', 0.0),
            'imu_crest':      imu_crest,
            'high_band_ratio': hb,
            'z_score':        z_score,
        }
        ml_alert = _ml_score(ml_frame)
        if ml_alert is not None and ml_alert > final:
            final = ml_alert   # escalate if ML is more confident

    return final, z_score, warn_streak, ok_streak


# ─── Per-satellite connection thread ─────────────────────────────────────────

def satellite_thread(conn, addr, exp_mic_bins, exp_imu_bins):
    mac_hex = None
    sat     = None
    csv_f   = None
    csv_w   = None
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

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

        # ── CSV log: one file per satellite per calendar day, append on reconnect ──
        # Avoids the explosion of one-frame files when the satellite reconnects.
        log_dir  = os.path.join(os.path.dirname(__file__), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        date_str = datetime.datetime.now().strftime('%Y%m%d')
        csv_path = os.path.join(log_dir, f"epm_{name}_{date_str}.csv")
        is_new   = not os.path.exists(csv_path)
        csv_f    = open(csv_path, 'a', newline='')
        csv_w    = csv.writer(csv_f)
        if is_new:
            csv_w.writerow(['wall_time', 'frame_id', 'device_ms',
                            'mic_rms', 'mic_crest', 'mic_kurtosis',
                            'imu_rms', 'imu_crest',
                            'high_band_ratio', 'z_score', 'alert'])
        print(f"    Logging to: {csv_path}  ({'new' if is_new else 'append'})")

        # Maximum valid payload: header + mic FFT + 3 × IMU FFT + 1 KB margin.
        # Guards against a malicious/buggy satellite sending a huge length prefix
        # that would cause recv_exact to try to allocate gigabytes.
        max_payload = (HEADER_SIZE
                       + exp_mic_bins * 4
                       + exp_imu_bins * 4 * 3
                       + 1024)

        # Per-connection streak counters — kept as local variables so mutations
        # never race with the dashboard HTTP reader (all sat writes go through lock).
        warn_streak = 0
        ok_streak   = 0
        sent_alert  = EPM_ALERT_OK

        while True:
            (payload_bytes,) = struct.unpack('<I', recv_exact(conn, 4))
            if payload_bytes < HEADER_SIZE or payload_bytes > max_payload:
                raise ValueError(
                    f"payload_bytes={payload_bytes} out of valid range "
                    f"[{HEADER_SIZE}..{max_payload}]")
            raw   = recv_exact(conn, payload_bytes)
            frame = parse_frame(raw, exp_mic_bins, exp_imu_bins)

            now   = time.time()
            fps   = sat.rolling_fps(now)

            # Compute band ratios once — shared by alert engine and fault classifier
            hb, lo_r, mid_r = _band_ratios(frame['mic_fft'])

            prev_alert = sent_alert   # alert from the PREVIOUS frame
            alert, z_score, warn_streak, ok_streak = \
                compute_alert(sat, frame, warn_streak, ok_streak, sent_alert, hb)
            sent_alert = alert

            # Detect state transitions → audit trail + phone notifications
            if alert != prev_alert:
                _log_alert_event(sat.name, mac_hex, alert, prev_alert,
                                 frame['mic_kurtosis'], frame['mic_crest'], z_score)
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
                f"{hb:.3f}", f"{z_score:.2f}",
                ["OK", "WARN", "FAULT"][min(alert, 2)]
            ])
            csv_f.flush()

            try:
                conn.sendall(bytes([alert]))
            except OSError:
                break

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

            _display.put(frame, name)

            cal_str   = (f"z={z_score:.1f}" if sat.calibrated
                         else f"cal{len(sat._cal_buf)}/{CAL_FRAMES}")
            alert_str = ["OK", "WARN", "FAULT"][min(alert, 2)]
            status    = "OK" if not frame['errors'] else "WARN:" + ";".join(frame['errors'])
            print(f"[{name:<10}] #{frame['frame_id']:5d}  "
                  f"fps={fps:.1f}  "
                  f"rms={frame['mic_rms']:.5f}  "
                  f"K={frame['mic_kurtosis']:.2f}  "
                  f"CF={frame['mic_crest']:.2f}  "
                  f"hb={hb:.2f}  {cal_str}  "
                  f"alert={alert_str}  {status}")

    except (ConnectionError, struct.error) as e:
        print(f"\n[-] {(sat.name if sat else mac_hex) or addr[0]} disconnected: {e}")
    except Exception as e:
        print(f"\n[-] {(sat.name if sat else mac_hex) or addr[0]} error: {e}")
    finally:
        if csv_f:
            csv_f.close()
        conn.close()
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
        print(f'[ml] Thresholds — WARN ≤ {_ML_MODEL["t_warn"]:.4f}   '
              f'FAULT ≤ {_ML_MODEL["t_fault"]:.4f}')
    except ImportError:
        print('[ml] WARNING: joblib/scikit-learn not installed — '
              'ignoring --model.  Run: pip install scikit-learn joblib')
    except Exception as e:
        print(f'[ml] WARNING: failed to load model: {e}')


def _ml_score(frame: dict) -> int | None:
    """
    Run ML inference on one frame dict.
    Returns EPM_ALERT_* (0/1/2) or None if no model is loaded.
    Frame dict keys match CSV columns produced by the satellite thread.
    """
    if _ML_MODEL is None:
        return None
    try:
        feat = [frame.get(c, 0.0) for c in _ML_MODEL['feat_cols']]
        X_s  = _ML_MODEL['scaler'].transform([feat])
        score = float(_ML_MODEL['model'].decision_function(X_s)[0])
        if score <= _ML_MODEL['t_fault']:
            return EPM_ALERT_FAULT
        if score <= _ML_MODEL['t_warn']:
            return EPM_ALERT_WARN
        return EPM_ALERT_OK
    except Exception:
        return None


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


# ─── Web dashboard ───────────────────────────────────────────────────────────

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#07111e">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="EPM Monitor">
<link rel="manifest" href="/manifest.json">
<title>EPM &middot; Industrial Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
<style>
:root{
  --bg:#07111e;--card:#0d1a27;--card2:#122031;--border:#1a2f44;
  --text:#dde6f0;--muted:#5b7a96;--dim:#2d4d66;
  --ok:#22c55e;--warn:#f59e0b;--fault:#ef4444;--blue:#3b82f6;--acc:#8b5cf6;
  --ok-d:rgba(34,197,94,.13);--warn-d:rgba(245,158,11,.13);--fault-d:rgba(239,68,68,.13);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;min-height:100vh}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* HEADER */
header{display:flex;align-items:center;padding:0 22px;height:56px;background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:200;gap:14px;box-shadow:0 2px 20px rgba(0,0,0,.5)}
.logo{display:flex;align-items:center;gap:9px;flex-shrink:0}
.logo-icon{width:32px;height:32px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);border-radius:7px;display:grid;place-items:center;font-size:1rem;flex-shrink:0}
.logo-name{font-size:.88rem;font-weight:700;letter-spacing:.02em;line-height:1.1}
.logo-sub{font-size:.55rem;color:var(--muted);letter-spacing:.06em;text-transform:uppercase}
.hdr-sep{width:1px;height:26px;background:var(--border);flex-shrink:0}
#factory-lbl{font-size:.78rem;font-weight:600;color:var(--blue)}
.hdr-right{display:flex;align-items:center;gap:10px;margin-left:auto}
.chip{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:20px;font-size:.63rem;font-weight:600;white-space:nowrap}
.chip-ok{background:var(--ok-d);color:var(--ok)}
.chip-warn{background:var(--warn-d);color:var(--warn)}
.chip-fault{background:var(--fault-d);color:var(--fault)}
.chip-blue{background:rgba(59,130,246,.1);color:var(--blue)}
.chip-muted{background:rgba(91,122,150,.08);color:var(--muted)}
.ldot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.ldot.ok{background:var(--ok);animation:pok 2s infinite}
.ldot.warn{background:var(--warn);animation:pwarn 1s infinite}
.ldot.fault{background:var(--fault);animation:pfault .4s infinite}
@keyframes pok{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(34,197,94,.4)}70%{box-shadow:0 0 0 5px rgba(34,197,94,0)}}
@keyframes pwarn{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(245,158,11,.5)}70%{box-shadow:0 0 0 5px rgba(245,158,11,0)}}
@keyframes pfault{0%,20%,40%,60%,80%,100%{opacity:1}10%,30%,50%,70%,90%{opacity:.1}}
.hdr-uptime{font-size:.62rem;color:var(--muted);font-variant-numeric:tabular-nums}
@media(max-width:680px){.hdr-sep,#factory-lbl,.hdr-uptime,.chip-muted{display:none}}

/* BANNER */
#banner{display:none;align-items:center;justify-content:center;gap:8px;padding:9px 22px;font-size:.8rem;font-weight:700;letter-spacing:.02em}
#banner.fault{display:flex;background:rgba(239,68,68,.07);color:var(--fault);border-bottom:2px solid rgba(239,68,68,.4);animation:bfl 1.5s infinite}
#banner.warn{display:flex;background:rgba(245,158,11,.06);color:var(--warn);border-bottom:2px solid rgba(245,158,11,.3)}
@keyframes bfl{0%,100%{border-bottom-color:rgba(239,68,68,.4)}50%{border-bottom-color:rgba(239,68,68,.8)}}
.bpulse{display:inline-block;animation:shake .55s infinite}
@keyframes shake{0%,100%{transform:rotate(0)}25%{transform:rotate(-9deg)}75%{transform:rotate(9deg)}}

/* SUMMARY */
.summary{display:flex;flex-wrap:wrap;gap:9px;padding:14px 22px 0}
.tile{flex:1;min-width:100px;background:var(--card);border:1px solid var(--border);border-radius:9px;padding:12px 14px}
.tile-lbl{font-size:.58rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:5px}
.tile-val{font-size:1.85rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.tile-sub{font-size:.6rem;color:var(--dim);margin-top:3px}

/* TABS */
.tabs-bar{padding:13px 22px 0}
.tabs{display:inline-flex;gap:1px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:3px}
.tab{padding:6px 15px;border-radius:6px;font-size:.74rem;font-weight:500;color:var(--muted);cursor:pointer;border:none;background:none;transition:all .15s;white-space:nowrap}
.tab:hover{color:var(--text);background:rgba(255,255,255,.04)}
.tab.active{background:var(--card2);color:var(--text);font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,.3)}
.tbadge{display:inline-flex;align-items:center;justify-content:center;min-width:16px;height:16px;border-radius:8px;background:var(--fault);color:#fff;font-size:.55rem;font-weight:700;padding:0 4px;margin-left:4px;vertical-align:middle}
.pane{display:none;padding:13px 22px 48px}
.pane.active{display:block}

/* MACHINE CARDS */
.cards-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(336px,1fr));gap:13px}
.no-sats{text-align:center;padding:80px 0;color:var(--muted)}
.no-sats h2{color:var(--text);font-size:.92rem;margin-bottom:7px}
.no-sats p{font-size:.76rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:11px;overflow:hidden;transition:transform .18s,box-shadow .18s,border-color .2s}
.card:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(0,0,0,.4)}
.card.ok{border-color:rgba(34,197,94,.2)}
.card.warn{border-color:rgba(245,158,11,.55);box-shadow:0 0 0 1px rgba(245,158,11,.1)}
.card.fault{border-color:rgba(239,68,68,.7);box-shadow:0 0 0 2px rgba(239,68,68,.12),0 0 26px rgba(239,68,68,.07)}
.card.offline{opacity:.38;filter:grayscale(.5)}
.c-head{display:flex;align-items:flex-start;justify-content:space-between;padding:12px 14px 9px;border-bottom:1px solid var(--border)}
.c-name{font-size:1rem;font-weight:700}
.c-mac{font-size:.6rem;color:var(--muted);font-family:monospace;margin-top:2px}
.c-fw{font-size:.57rem;color:var(--dim);margin-top:1px}
.c-right{display:flex;flex-direction:column;align-items:flex-end;gap:5px}
.sdot{width:9px;height:9px;border-radius:50%}
.sdot.ok{background:var(--ok);animation:pok 2s infinite}
.sdot.warn{background:var(--warn);animation:pwarn 1s infinite}
.sdot.fault{background:var(--fault);animation:pfault .4s infinite}
.badge{padding:2px 8px;border-radius:8px;font-size:.64rem;font-weight:800;letter-spacing:.04em}
.badge.ok{background:var(--ok-d);color:var(--ok)}
.badge.warn{background:var(--warn-d);color:var(--warn)}
.badge.fault{background:var(--fault-d);color:var(--fault)}
.c-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border)}
.met{background:var(--card);padding:8px 11px}
.met.sp3{grid-column:span 3}
.ml{font-size:.56rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.mv{font-size:.93rem;font-weight:600;font-family:monospace;margin-top:2px}
.c-health{display:flex;align-items:center;gap:10px;padding:9px 14px}
.c-hbar-wrap{flex:1}
.c-hbar-top{display:flex;justify-content:space-between;font-size:.58rem;color:var(--muted);margin-bottom:4px}
.c-hbar{height:5px;border-radius:3px;background:var(--border);overflow:hidden}
.c-hfill{height:100%;border-radius:3px;transition:width .7s,background .5s}
.c-hpct{font-size:1.15rem;font-weight:700;min-width:46px;text-align:right;font-variant-numeric:tabular-nums}
.c-rec{margin:0 12px 9px;padding:8px 11px;border-radius:7px;font-size:.72rem}
.c-rec.ok{background:rgba(34,197,94,.05);border:1px solid rgba(34,197,94,.17)}
.c-rec.warn{background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.22)}
.c-rec.fault{background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.3)}
.c-rec-t{font-weight:700;margin-bottom:2px}
.c-rec-s{color:var(--muted);font-size:.64rem;line-height:1.5;margin-top:2px}
.c-chart{padding:2px 12px 8px}
.c-maint-row{display:flex;align-items:center;justify-content:space-between;padding:5px 14px;background:rgba(7,17,30,.45);border-top:1px solid var(--border);font-size:.63rem;color:var(--muted);gap:6px;flex-wrap:wrap}
.c-maint-val{color:var(--text);font-weight:600}
.c-actions{display:flex;gap:6px;padding:8px 12px;background:var(--card2);border-top:1px solid var(--border)}
.btn{padding:5px 11px;border-radius:6px;font-size:.68rem;font-weight:600;cursor:pointer;border:1px solid transparent;transition:all .15s;white-space:nowrap;text-decoration:none;display:inline-flex;align-items:center;gap:4px}
.btn-blue{background:rgba(59,130,246,.14);color:var(--blue);border-color:rgba(59,130,246,.28)}
.btn-blue:hover{background:rgba(59,130,246,.24)}
.btn-ghost{background:transparent;color:var(--muted);border-color:var(--border)}
.btn-ghost:hover{background:rgba(255,255,255,.04);color:var(--text)}
.c-foot{display:flex;justify-content:space-between;padding:6px 14px;background:rgba(7,17,30,.55);border-top:1px solid var(--border);font-size:.6rem;color:var(--muted);flex-wrap:wrap;gap:3px}

/* TABLE (alert log) */
.pane-head{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px;gap:10px;flex-wrap:wrap}
.pane-title{font-size:.86rem;font-weight:700}
.pane-note{font-size:.63rem;color:var(--muted);margin-top:3px}
.tbl-wrap{background:var(--card);border:1px solid var(--border);border-radius:9px;overflow:hidden;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.72rem;min-width:580px}
thead tr{background:var(--card2)}
th{padding:8px 12px;text-align:left;font-size:.6rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600;border-bottom:1px solid var(--border)}
td{padding:8px 12px;border-bottom:1px solid rgba(26,47,68,.5);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.015)}
.trans{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:5px;font-size:.66rem;font-weight:700}
.trans.esc{background:var(--fault-d);color:var(--fault)}
.trans.rec{background:var(--ok-d);color:var(--ok)}
.trans.war{background:var(--warn-d);color:var(--warn)}
.empty-cell{text-align:center;color:var(--muted);padding:32px!important;font-size:.76rem}

/* MAINTENANCE CARDS */
.maint-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:11px}
.mc{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:14px}
.mc-name{font-size:.86rem;font-weight:700;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:6px}
.mc-row{display:flex;justify-content:space-between;align-items:baseline;padding:5px 0;border-bottom:1px solid rgba(26,47,68,.45);font-size:.72rem;gap:8px}
.mc-row:last-of-type{border-bottom:none}
.mc-lbl{color:var(--muted);font-size:.63rem;flex-shrink:0}
.mc-val{font-weight:600;text-align:right;font-size:.72rem}
.mc-empty{color:var(--muted);font-size:.72rem;text-align:center;padding:16px 0}
.mc-foot{margin-top:11px;padding-top:9px;border-top:1px solid var(--border)}

/* REPORTS */
.rep-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}
.rep-card{background:var(--card);border:1px solid var(--border);border-radius:9px;padding:16px}
.rep-card h3{font-size:.78rem;font-weight:700;margin-bottom:11px;display:flex;align-items:center;gap:6px}
.rep-row{display:flex;justify-content:space-between;padding:5px 0;font-size:.71rem;border-bottom:1px solid rgba(26,47,68,.4)}
.rep-row:last-child{border-bottom:none}
.rep-key{color:var(--muted)}
.rep-val{font-weight:600;font-family:monospace;font-size:.68rem;text-align:right}
.chk-list{list-style:none}
.chk-li{display:flex;align-items:center;gap:7px;padding:5px 0;border-bottom:1px solid rgba(26,47,68,.35);font-size:.71rem}
.chk-li:last-child{border-bottom:none}
.chk-icon{width:15px;text-align:center;font-size:.78rem;flex-shrink:0}
.exp-col{display:flex;flex-direction:column;gap:7px;margin-top:8px}
.exp-btn{display:flex;align-items:center;gap:7px;padding:8px 12px;border-radius:7px;background:var(--card2);border:1px solid var(--border);color:var(--text);font-size:.71rem;cursor:pointer;text-decoration:none;transition:all .15s;font-family:inherit;width:100%;text-align:left}
.exp-btn:hover{border-color:var(--blue);background:rgba(59,130,246,.06)}

/* MODAL */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:500;backdrop-filter:blur(5px);align-items:center;justify-content:center;padding:20px}
.modal-bg.open{display:flex}
/* QR MODAL */
#qr-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:600;backdrop-filter:blur(5px);align-items:center;justify-content:center}
#qr-modal.open{display:flex}
.qr-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px 28px;text-align:center;box-shadow:0 24px 60px rgba(0,0,0,.8)}
.qr-box h3{font-size:.88rem;margin-bottom:6px}
.qr-box p{font-size:.72rem;color:var(--muted);margin-bottom:14px}
.qr-close{margin-top:14px;background:var(--card2);border:1px solid var(--border);color:var(--text);padding:6px 20px;border-radius:6px;cursor:pointer;font-size:.78rem}
.modal{background:var(--card);border:1px solid var(--border);border-radius:12px;width:100%;max-width:450px;max-height:90vh;overflow-y:auto;box-shadow:0 24px 60px rgba(0,0,0,.7)}
.modal-hd{display:flex;align-items:center;justify-content:space-between;padding:15px 18px;border-bottom:1px solid var(--border)}
.modal-title{font-size:.88rem;font-weight:700}
.modal-x{width:25px;height:25px;border-radius:6px;border:none;background:rgba(255,255,255,.06);color:var(--text);cursor:pointer;font-size:.85rem;display:grid;place-items:center;transition:background .15s}
.modal-x:hover{background:rgba(255,255,255,.12)}
.modal-bd{padding:16px 18px}
.modal-info{font-size:.66rem;color:var(--muted);padding:6px 9px;background:rgba(7,17,30,.6);border-radius:6px;margin-bottom:13px;font-family:monospace}
.fg{margin-bottom:12px}
.fg label{display:block;font-size:.64rem;color:var(--muted);margin-bottom:4px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.fi{width:100%;background:rgba(7,17,30,.8);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 9px;font-size:.76rem;font-family:inherit;transition:border-color .15s}
.fi:focus{outline:none;border-color:var(--blue)}
.fi::placeholder{color:var(--dim)}
.fg-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.modal-ft{display:flex;justify-content:flex-end;gap:8px;padding:12px 18px;border-top:1px solid var(--border)}
.btn-sm{padding:6px 14px;border-radius:6px;font-size:.71rem;font-weight:600;cursor:pointer;border:1px solid var(--border);transition:all .15s}
.btn-sm-ok{background:var(--blue);color:#fff;border-color:var(--blue)}
.btn-sm-ok:hover{background:#2563eb}
.btn-sm-cancel{background:transparent;color:var(--muted)}
.btn-sm-cancel:hover{color:var(--text);background:rgba(255,255,255,.04)}

/* TOAST */
#toast{position:fixed;bottom:20px;right:20px;padding:10px 16px;border-radius:8px;font-size:.74rem;font-weight:700;z-index:9999;transform:translateY(50px);opacity:0;transition:all .25s;pointer-events:none;max-width:300px}
#toast.in{transform:translateY(0);opacity:1}
#toast.ok-t{background:#15803d;color:#fff}
#toast.err-t{background:#b91c1c;color:#fff}

footer{text-align:center;padding:11px;color:var(--dim);font-size:.6rem;border-top:1px solid var(--border)}
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-icon">&#9881;</div>
    <div>
      <div class="logo-name">EPM Monitor</div>
      <div class="logo-sub">EdgeAI Predictive Maintenance</div>
    </div>
  </div>
  <div class="hdr-sep"></div>
  <span id="factory-lbl">Factory</span>
  <div class="hdr-right">
    <span class="chip chip-muted" id="gstatus">&#9679; LOADING</span>
    <span class="chip chip-muted" id="notif-chip">&#128276; Notify OFF</span>
    <span class="chip chip-blue" id="auth-chip" style="display:none">&#128274; Secured</span>
    <div style="display:flex;align-items:center;gap:5px">
      <div class="ldot ok" id="live-dot"></div>
      <span class="hdr-uptime" id="hdr-clock">&#8212;</span>
    </div>
    <span class="hdr-uptime">UP <strong id="hdr-up">&#8212;</strong></span>
  </div>
</header>

<div id="banner"></div>

<div class="summary">
  <div class="tile"><div class="tile-lbl">Connected</div><div class="tile-val" id="s-conn" style="color:var(--blue)">0</div><div class="tile-sub">satellites online</div></div>
  <div class="tile"><div class="tile-lbl">Healthy</div><div class="tile-val" id="s-ok" style="color:var(--ok)">0</div><div class="tile-sub">running OK</div></div>
  <div class="tile"><div class="tile-lbl">Warning</div><div class="tile-val" id="s-warn" style="color:var(--warn)">0</div><div class="tile-sub">elevated vibration</div></div>
  <div class="tile"><div class="tile-lbl">Fault</div><div class="tile-val" id="s-fault" style="color:var(--fault)">0</div><div class="tile-sub">needs attention</div></div>
  <div class="tile"><div class="tile-lbl">Fault Events</div><div class="tile-val" id="s-fevt" style="color:var(--fault)">0</div><div class="tile-sub">this session</div></div>
  <div class="tile"><div class="tile-lbl">Avg Health</div><div class="tile-val" id="s-health">&#8212;</div><div class="tile-sub" id="s-fstate" style="color:var(--muted)">&#8212;</div></div>
</div>

<div class="tabs-bar">
  <div class="tabs">
    <button class="tab active" data-tab="machines">&#9881; Machines</button>
    <button class="tab" data-tab="alerts">&#128203; Alert Log <span class="tbadge" id="alert-badge" style="display:none">0</span></button>
    <button class="tab" data-tab="maintenance">&#128295; Maintenance</button>
    <button class="tab" data-tab="reports">&#128202; Reports</button>
  </div>
</div>

<!-- MACHINES -->
<div class="pane active" id="pane-machines">
  <div class="cards-grid" id="grid">
    <div class="no-sats"><h2>Waiting for satellites&hellip;</h2><p>Power on XIAO ESP32-S3 nodes or run <code>satellite_sim.py</code></p></div>
  </div>
</div>

<!-- ALERT LOG -->
<div class="pane" id="pane-alerts">
  <div class="pane-head">
    <div>
      <div class="pane-title">Alert History &mdash; Audit Trail</div>
      <div class="pane-note">&#128274; Compliance-ready log of all machine state transitions. Every OK&rarr;WARN&rarr;FAULT transition is timestamped and recorded.</div>
    </div>
    <div style="display:flex;gap:7px">
      <button class="btn btn-ghost" onclick="loadAlerts()">&#8635; Refresh</button>
      <button class="btn btn-blue" onclick="exportAlerts()">&#8595; Export JSON</button>
    </div>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead><tr><th>Time</th><th>Machine</th><th>Transition</th><th>Kurtosis</th><th>Crest</th><th>Z-Score</th><th>MAC</th></tr></thead>
      <tbody id="alert-tbody"><tr><td class="empty-cell" colspan="7">Switch to this tab to load alert history.</td></tr></tbody>
    </table>
  </div>
</div>

<!-- MAINTENANCE -->
<div class="pane" id="pane-maintenance">
  <div class="pane-head">
    <div>
      <div class="pane-title">Maintenance Records</div>
      <div class="pane-note">Persisted to <code>logs/maintenance_log.json</code> &mdash; survives gateway restarts. Keyed by hardware MAC address.</div>
    </div>
    <button class="btn btn-ghost" onclick="loadMaintenance()">&#8635; Refresh</button>
  </div>
  <div class="maint-grid" id="maint-grid"><p style="color:var(--muted);font-size:.76rem">Loading&hellip;</p></div>
</div>

<!-- REPORTS -->
<div class="pane" id="pane-reports">
  <div class="pane-head"><div class="pane-title">Reports &amp; System Info</div></div>
  <div class="rep-grid">

    <div class="rep-card">
      <h3><span>&#128187;</span> System Status</h3>
      <div class="rep-row"><span class="rep-key">Factory / Site</span><span class="rep-val" id="r-factory">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">Gateway Uptime</span><span class="rep-val" id="r-uptime">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">Satellites</span><span class="rep-val" id="r-sats">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">K Warn / Fault</span><span class="rep-val" id="r-kth">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">CF Warn / Fault</span><span class="rep-val" id="r-cfth">&#8212;</span></div>
      <div class="rep-row"><span class="rep-key">Notifications</span><span class="rep-val" id="r-notif">&#8212;</span></div>
    </div>

    <div class="rep-card">
      <h3><span>&#9989;</span> Compliance Checklist</h3>
      <ul class="chk-list">
        <li class="chk-li"><span class="chk-icon" id="chk-conn">&#9744;</span>All machines connected</li>
        <li class="chk-li"><span class="chk-icon" id="chk-health">&#9744;</span>No active FAULT conditions</li>
        <li class="chk-li"><span class="chk-icon" id="chk-maint">&#9744;</span>Maintenance records up to date</li>
        <li class="chk-li"><span class="chk-icon" id="chk-auth">&#9744;</span>Dashboard access secured (--auth)</li>
        <li class="chk-li"><span class="chk-icon" id="chk-notif">&#9744;</span>Alert notifications configured</li>
        <li class="chk-li"><span class="chk-icon" id="chk-cal">&#9744;</span>All sensors calibrated</li>
        <li class="chk-li"><span class="chk-icon" id="chk-log">&#10003;</span>Audit trail active (in-memory, 1000 events)</li>
      </ul>
    </div>

    <div class="rep-card">
      <h3><span>&#8595;</span> Export Data</h3>
      <p style="font-size:.7rem;color:var(--muted);margin-bottom:10px;line-height:1.6">Download sensor logs and audit records for compliance reports, insurance claims, or offline ML analysis.</p>
      <div class="exp-col" id="export-sat-list"><span style="font-size:.7rem;color:var(--muted)">Connect a satellite to enable CSV exports.</span></div>
      <div style="margin-top:9px;padding-top:9px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:7px">
        <a class="exp-btn" href="/api/report" target="_blank">&#128202; Full Factory Report (all machines, printable PDF)</a>
        <button class="exp-btn" onclick="exportAlerts()">&#128203; Alert Log Export (JSON)</button>
      </div>
    </div>

    <div class="rep-card">
      <h3><span>&#128267;</span> Power &amp; Battery</h3>
      <div class="rep-row"><span class="rep-key">Power Source</span><span class="rep-val">USB / External 5V</span></div>
      <div class="rep-row"><span class="rep-key">Battery %</span><span class="rep-val">N/A (USB powered)</span></div>
      <div class="rep-row"><span class="rep-key">WiFi Power Mode</span><span class="rep-val">WIFI_PS_NONE</span></div>
      <p style="margin-top:9px;font-size:.66rem;color:var(--muted);line-height:1.65">
        For LiPo: set <code>esp_wifi_set_ps(WIFI_PS_MIN_MODEM)</code> in wifi_task.c and add
        <code>CONFIG_PM_ENABLE=y</code> to sdkconfig.defaults (~30% power saving).
        Battery % requires ADC on a free GPIO &mdash; not yet wired.
      </p>
    </div>

    <div class="rep-card">
      <h3><span>&#128242;</span> Alert Notifications</h3>
      <p style="font-size:.7rem;color:var(--muted);margin-bottom:9px;line-height:1.6">Sends emergency alerts to phone/Slack/Discord/email on FAULT detection. 5-min rate limit prevents spam.</p>
      <div class="rep-row"><span class="rep-key">Webhook Active</span><span class="rep-val" id="r-wh">Not configured</span></div>
      <div class="rep-row"><span class="rep-key">Email Active</span><span class="rep-val" id="r-email">Not configured</span></div>
      <p style="margin-top:9px;font-size:.65rem;color:var(--dim);line-height:1.65">
        Enable: <code>--notify-webhook URL</code> (Discord/Slack/Teams)<br>
        or: <code>--notify-email from:to:host[:port[:user:pass]]</code>
      </p>
    </div>

    <div class="rep-card">
      <h3><span>&#127963;</span> For Auditors &amp; Inspectors</h3>
      <p style="font-size:.7rem;color:var(--muted);line-height:1.65;margin-bottom:8px">
        Complete digital record of machine health, fault events, and maintenance history.
        All data is timestamped (epoch) and keyed by hardware MAC address &mdash; impossible to spoof without physical device access.
      </p>
      <ul class="chk-list">
        <li class="chk-li"><span class="chk-icon">&#128203;</span>Alert Log tab: every state-change since startup</li>
        <li class="chk-li"><span class="chk-icon">&#128295;</span>Maintenance tab: technician + service records</li>
        <li class="chk-li"><span class="chk-icon">&#128196;</span>CSV files: per-machine daily sensor data</li>
        <li class="chk-li"><span class="chk-icon">&#128274;</span>HTTP Basic Auth: user-level access control</li>
        <li class="chk-li"><span class="chk-icon">&#128268;</span>Hardware MAC: unique ID per sensor node</li>
      </ul>
    </div>

  </div>
</div>

<!-- MAINTENANCE MODAL -->
<div class="modal-bg" id="maint-modal">
  <div class="modal">
    <div class="modal-hd">
      <span class="modal-title">&#128295; Log Maintenance</span>
      <button class="modal-x" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="modal-bd">
      <div class="modal-info" id="modal-info">&#8212;</div>
      <input type="hidden" id="modal-mac">
      <div class="fg-row">
        <div class="fg"><label>Last Service Date *</label><input class="fi" type="date" id="f-last"></div>
        <div class="fg"><label>Next Scheduled</label><input class="fi" type="date" id="f-next"></div>
      </div>
      <div class="fg"><label>Technician / Team *</label><input class="fi" type="text" id="f-tech" placeholder="e.g. John Smith / Maintenance Team A"></div>
      <div class="fg">
        <label>Service Type</label>
        <select class="fi" id="f-type">
          <option>Routine Inspection</option>
          <option>Bearing Replacement</option>
          <option>Lubrication Service</option>
          <option>Alignment Check</option>
          <option>Vibration Analysis</option>
          <option>Full Overhaul</option>
          <option>Emergency Repair</option>
          <option>Sensor Calibration</option>
          <option>Other</option>
        </select>
      </div>
      <div class="fg"><label>Notes / Observations</label><textarea class="fi" id="f-notes" rows="3" placeholder="Parts replaced, readings taken, observations&hellip;" style="resize:vertical"></textarea></div>
    </div>
    <div class="modal-ft">
      <button class="btn-sm btn-sm-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-sm btn-sm-ok" onclick="submitMaint()">Save Record</button>
    </div>
  </div>
</div>

<!-- QR CODE MODAL -->
<div id="qr-modal" onclick="if(event.target===this)closeQR()">
  <div class="qr-box">
    <h3 class="qr-name"></h3>
    <p>Scan to open the live inspection report on any device</p>
    <canvas id="qr-canvas"></canvas>
    <button class="qr-close" onclick="closeQR()">Close</button>
  </div>
</div>

<div id="toast"></div>
<footer id="footer">EPM Dashboard &mdash; Auto-refreshes every 2 s</footer>

<script>
const CH={};let TH={k_warn:6,k_fault:12,cf_warn:5,cf_fault:10};let lastKey='';let STATUS=null;
let alertsLoaded=false;

const $=id=>document.getElementById(id);
function fmtUp(s){if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s';return Math.floor(s/3600)+'h '+String(Math.floor((s%3600)/60)).padStart(2,'0')+'m';}
function fmtDt(ts){return ts?new Date(ts*1000).toLocaleString(undefined,{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}):'never';}
function fmtFuture(days){if(!days&&days!==0)return '';const d=new Date(Date.now()+days*864e5);return d.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});}
function kCol(k){return k>TH.k_fault?'var(--fault)':k>TH.k_warn?'var(--warn)':'var(--ok)';}
function hCol(h){return h>=75?'var(--ok)':h>=50?'var(--warn)':'var(--fault)';}
function mCls(d){return d===0?'fault':d<=30?'warn':'ok';}
function rulCol(d){return d===null?'var(--ok)':d>90?'var(--ok)':d>30?'var(--warn)':d>7?'#ff9500':'var(--fault)';}
function fmtRul(d){if(d===null||d===undefined)return '✓ Stable';if(d<0.05)return '⚠ Threshold reached';if(d<1)return '⚠ <1 day';if(d<7)return '⚠ ~'+Math.round(d)+' day'+(Math.round(d)!==1?'s':'');return '~'+Math.round(d)+' days';}
function transCls(from,to){const r={OK:0,WARN:1,FAULT:2};return (r[to]||0)>(r[from]||0)?'esc':(r[to]||0)<(r[from]||0)?'rec':'war';}

function toast(msg,ok=true){const t=$('toast');t.textContent=msg;t.className='in '+(ok?'ok-t':'err-t');setTimeout(()=>{t.className='';},3200);}

/* --- fault type helpers --- */
function ftCls(ft){
  if(!ft||ft==='Normal')return 'ok';
  if(ft.includes('Fault')||ft.includes('Severe')||ft.includes('Advanced'))return 'fault';
  if(ft.includes('Looseness')||ft.includes('Misalign')||ft.includes('Anomal')||ft.includes('Elevated'))return 'warn';
  return 'info';
}
function showQR(name,mac){
  const m=$('qr-modal');
  m.querySelector('.qr-name').textContent=name+(mac?' · '+mac:'');
  const url=location.origin+'/api/report?name='+encodeURIComponent(name);
  const canvas=m.querySelector('#qr-canvas');
  canvas.width=0;canvas.height=0;
  if(typeof QRCode!=='undefined'){
    QRCode.toCanvas(canvas,url,{width:220,margin:2,color:{dark:'#111111',light:'#ffffff'}},function(err){if(err)console.error(err);});
  }
  m.className='open';
}
function closeQR(){$('qr-modal').className='';}
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeQR();});

/* --- card HTML --- */
function cardHTML(s){
  const al=s.alert.toLowerCase(),m=s.metrics||{},ml=s.maint_log||{};
  const hc=hCol(s.health_score),mc=mCls(s.maintenance_days);
  const fd=fmtFuture(s.maintenance_days),due=fd?' · Due: <strong>'+fd+'</strong>':'';
  const lm=ml.last_date||'—',tech=ml.technician?'· '+ml.technician:'';
  return '<div class="card '+al+(s.connected?'':' offline')+'" id="C_'+s.name+'">'
    +'<div class="c-head"><div><div class="c-name">'+s.name+'</div><div class="c-mac">'+s.mac+'</div>'
    +'<div class="c-fw">FW '+s.fw+(s.calibrated?' · ✓ Calibrated':' · ⧖ Calibrating')+'</div></div>'
    +'<div class="c-right"><div class="sdot '+al+'"></div><span class="badge '+al+'">'+s.alert+'</span>'
    +'<span class="ft-badge ft-'+ftCls(s.fault_type||'Normal')+'" id="FT_'+s.name+'">'+(s.fault_type||'Normal')+'</span></div></div>'
    +'<div class="c-metrics">'
    +'<div class="met"><div class="ml">Kurtosis</div><div class="mv" style="color:'+kCol(m.mic_kurtosis||0)+'" id="K_'+s.name+'">'+(m.mic_kurtosis||0).toFixed(2)+'</div></div>'
    +'<div class="met"><div class="ml">Crest Factor</div><div class="mv" id="CF_'+s.name+'">'+(m.mic_crest||0).toFixed(2)+'</div></div>'
    +'<div class="met"><div class="ml">High-Band %</div><div class="mv" id="HB_'+s.name+'">'+(((m.high_band_ratio||0)*100).toFixed(1))+'%</div></div>'
    +'<div class="met"><div class="ml">Mic RMS</div><div class="mv" id="RMS_'+s.name+'">'+(m.mic_rms||0).toFixed(5)+'</div></div>'
    +'<div class="met"><div class="ml">Z-Score</div><div class="mv" style="color:'+(s.z_score>3?'var(--fault)':s.z_score>1.5?'var(--warn)':'inherit')+'" id="Z_'+s.name+'">'+s.z_score.toFixed(1)+'</div></div>'
    +'<div class="met"><div class="ml">Frame Rate</div><div class="mv" id="FPS_'+s.name+'">'+s.fps.toFixed(1)+' fps</div></div>'
    +'<div class="met sp3"><div class="ml">Est. Remaining Useful Life</div>'
    +'<div class="mv" id="RUL_'+s.name+'" style="color:'+rulCol(s.rul_days)+';font-size:.8rem">'+fmtRul(s.rul_days)+'</div></div>'
    +'</div>'
    +'<div class="c-health"><div class="c-hbar-wrap">'
    +'<div class="c-hbar-top"><span>Machine Health</span><span id="HS_'+s.name+'" style="color:'+hc+'">'+s.health_score+'%</span></div>'
    +'<div class="c-hbar"><div class="c-hfill" id="HF_'+s.name+'" style="width:'+s.health_score+'%;background:'+hc+'"></div></div>'
    +'</div></div>'
    +'<div class="c-rec '+mc+'" id="MNT_'+s.name+'">'
    +'<div class="c-rec-t">🔧 '+s.maintenance+due+'</div>'
    +'<div class="c-rec-s">Warn: '+s.warn_frames+' · Fault: '+s.fault_frames+(s.last_fault_t?' · Last fault: '+fmtDt(s.last_fault_t):' ')+'</div>'
    +'</div>'
    +'<div class="c-chart"><canvas id="CH_'+s.name+'" height="62"></canvas></div>'
    +'<div class="c-maint-row"><span>🔧 Last maint: <span class="c-maint-val" id="LM_'+s.name+'">'+lm+'</span>'+tech+'</span>'
    +(ml.next_date?'<span>Next: <strong>'+ml.next_date+'</strong></span>':'')
    +'</div>'
    +'<div class="c-actions">'
    +'<button class="btn btn-ghost" onclick="showQR(\''+s.name+'\',\''+s.mac+'\')">&#128247; QR</button>'
    +'<button class="btn btn-blue" onclick="openModal(\''+s.mac+'\',\''+s.name+'\')">&#128221; Log Maintenance</button>'
    +'<a class="btn btn-ghost" href="/api/report?name='+encodeURIComponent(s.name)+'" target="_blank">&#128202; Report</a>'
    +'<a class="btn btn-ghost" href="/api/export?name='+encodeURIComponent(s.name)+'" download>&#8595; CSV</a>'
    +'</div>'
    +'<div class="c-foot">'
    +'<span id="CF2_'+s.name+'">Frames: '+s.frame_count.toLocaleString()+'</span>'
    +'<span id="CF3_'+s.name+'">Up '+fmtUp(s.uptime_s)+'</span>'
    +'<span id="CF4_'+s.name+'">'+(s.connected?'🟢 Online':'🔴 Offline')+'</span>'
    +'</div></div>';
}

/* --- build/update sparkline chart --- */
function buildChart(s){
  const el=$('CH_'+s.name);if(!el)return;
  const h=s.history||{kurtosis:[],crest:[]},n=h.kurtosis.length;
  const wl=Array(n).fill(TH.k_warn),fl=Array(n).fill(TH.k_fault);
  if(CH[s.name]){const c=CH[s.name];c.data.datasets[0].data=h.kurtosis;c.data.datasets[1].data=h.crest;c.data.datasets[2].data=wl;c.data.datasets[3].data=fl;c.update('none');return;}
  CH[s.name]=new Chart(el,{type:'line',data:{labels:Array.from({length:n},(_,i)=>i),datasets:[
    {label:'Kurtosis',data:h.kurtosis,borderColor:'rgba(59,130,246,.9)',backgroundColor:'rgba(59,130,246,.07)',borderWidth:1.5,pointRadius:0,tension:.3,fill:true},
    {label:'Crest',data:h.crest,borderColor:'rgba(245,158,11,.8)',backgroundColor:'rgba(245,158,11,.04)',borderWidth:1.5,pointRadius:0,tension:.3},
    {label:'Warn',data:wl,borderColor:'rgba(245,158,11,.4)',borderWidth:1,pointRadius:0,borderDash:[4,4]},
    {label:'Fault',data:fl,borderColor:'rgba(239,68,68,.4)',borderWidth:1,pointRadius:0,borderDash:[4,4]},
  ]},options:{responsive:true,animation:false,plugins:{legend:{display:true,position:'top',labels:{color:'#5b7a96',font:{size:8},boxWidth:9,padding:6}}},scales:{
    x:{display:false},
    y:{min:0,max:Math.max(TH.k_fault+3,14),ticks:{color:'#2d4d66',font:{size:8}},grid:{color:'rgba(255,255,255,.03)'},border:{color:'#1a2f44'}}
  }}});
}

/* --- update card in-place (no full re-render) --- */
function upCard(s){
  const card=$('C_'+s.name);if(!card)return;
  const al=s.alert.toLowerCase(),m=s.metrics||{},ml=s.maint_log||{};
  const hc=hCol(s.health_score),mc=mCls(s.maintenance_days);
  const fd=fmtFuture(s.maintenance_days),due=fd?' · Due: <strong>'+fd+'</strong>':'';
  card.className='card '+al+(s.connected?'':' offline');
  card.querySelector('.sdot').className='sdot '+al;
  const b=card.querySelector('.badge');b.className='badge '+al;b.textContent=s.alert;
  const g=(id,v)=>{const e=$(id);if(e)e.textContent=v;};
  const gs=(id,p,v)=>{const e=$(id);if(e)e.style[p]=v;};
  g('K_'+s.name,(m.mic_kurtosis||0).toFixed(2));gs('K_'+s.name,'color',kCol(m.mic_kurtosis||0));
  g('CF_'+s.name,(m.mic_crest||0).toFixed(2));
  g('HB_'+s.name,((m.high_band_ratio||0)*100).toFixed(1)+'%');
  g('RMS_'+s.name,(m.mic_rms||0).toFixed(5));
  g('Z_'+s.name,s.z_score.toFixed(1));gs('Z_'+s.name,'color',s.z_score>3?'var(--fault)':s.z_score>1.5?'var(--warn)':'');
  g('FPS_'+s.name,s.fps.toFixed(1)+' fps');
  const rul=$('RUL_'+s.name);if(rul){rul.textContent=fmtRul(s.rul_days);rul.style.color=rulCol(s.rul_days);}
  const hf=$('HF_'+s.name);if(hf){hf.style.width=s.health_score+'%';hf.style.background=hc;}
  gs('HS_'+s.name,'color',hc);g('HS_'+s.name,s.health_score+'%');
  const mnt=$('MNT_'+s.name);
  if(mnt){mnt.className='c-rec '+mc;mnt.innerHTML='<div class="c-rec-t">🔧 '+s.maintenance+due+'</div>'
    +'<div class="c-rec-s">Warn: '+s.warn_frames+' · Fault: '+s.fault_frames+(s.last_fault_t?' · Last fault: '+fmtDt(s.last_fault_t):' ')+'</div>';}
  const ftEl=$('FT_'+s.name);if(ftEl){ftEl.textContent=s.fault_type||'Normal';ftEl.className='ft-badge ft-'+ftCls(s.fault_type||'Normal');}
  g('LM_'+s.name,ml.last_date||'—');
  g('CF2_'+s.name,'Frames: '+s.frame_count.toLocaleString());
  g('CF3_'+s.name,'Up '+fmtUp(s.uptime_s));
  g('CF4_'+s.name,s.connected?'🟢 Online':'🔴 Offline');
}

/* --- tab switching --- */
document.querySelectorAll('.tab').forEach(t=>{
  t.addEventListener('click',()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('.pane').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    $('pane-'+t.dataset.tab).classList.add('active');
    const tab=t.dataset.tab;
    if(tab==='alerts'&&!alertsLoaded){loadAlerts();alertsLoaded=true;}
    if(tab==='maintenance')loadMaintenance();
    if(tab==='reports'&&STATUS)updateReports(STATUS);
  });
});

/* --- main 2s refresh --- */
async function refresh(){
  try{
    const r=await fetch('/api/status');
    if(!r.ok){const gs=$('gstatus');if(gs){gs.textContent='⚠ Server error '+r.status;gs.className='chip chip-fault';}return;}

    const d=await r.json();STATUS=d;TH=d.thresholds||TH;
    const sats=d.satellites;
    const ok_n=sats.filter(s=>s.alert==='OK').length;
    const wn_n=sats.filter(s=>s.alert==='WARN').length;
    const ft_n=sats.filter(s=>s.alert==='FAULT').length;
    const avg=sats.length?Math.round(sats.reduce((a,s)=>a+s.health_score,0)/sats.length):null;

    // header
    $('factory-lbl').textContent=d.factory_name||'Factory';
    document.title='EPM · '+(d.factory_name||'Monitor');
    $('hdr-up').textContent=fmtUp(d.server_uptime_s);
    $('hdr-clock').textContent=new Date().toLocaleTimeString();
    const nc=$('notif-chip');
    if(d.notify_active){nc.textContent='🔔 Notify ON';nc.className='chip chip-ok';}
    else{nc.textContent='🔔 Notify OFF';nc.className='chip chip-muted';}
    const gs=$('gstatus');
    if(ft_n>0){gs.textContent='● FAULT';gs.className='chip chip-fault';}
    else if(wn_n>0){gs.textContent='● WARNING';gs.className='chip chip-warn';}
    else if(sats.length){gs.textContent='● ALL OK';gs.className='chip chip-ok';}
    else{gs.textContent='● STANDBY';gs.className='chip chip-muted';}
    const ld=$('live-dot');
    ld.className='ldot '+(ft_n>0?'fault':wn_n>0?'warn':'ok');

    // banner
    const bn=$('banner');
    if(ft_n>0){bn.className='fault';bn.innerHTML='<span class="bpulse">🚨</span> FAULT — '+sats.filter(s=>s.alert==='FAULT').map(s=>s.name).join(', ')+' — Immediate inspection required';}
    else if(wn_n>0){bn.className='warn';bn.innerHTML='⚡ WARNING — '+sats.filter(s=>s.alert==='WARN').map(s=>s.name).join(', ')+' — Elevated vibration detected';}
    else bn.className='';

    // summary tiles
    $('s-conn').textContent=d.satellite_count;
    $('s-ok').textContent=ok_n;
    $('s-warn').textContent=wn_n;
    $('s-fault').textContent=ft_n;
    $('s-fevt').textContent=d.total_faults_today;
    const sh=$('s-health');sh.textContent=avg!==null?avg+'%':'—';sh.style.color=avg!==null?hCol(avg):'';
    const sf=$('s-fstate');
    sf.textContent=ft_n>0?'⚠ Factory alert':wn_n>0?'Factory warning':sats.length?'Factory healthy':'No satellites';
    sf.style.color=ft_n>0?'var(--fault)':wn_n>0?'var(--warn)':'var(--ok)';

    // alert badge on tab
    const ab=$('alert-badge');
    if(ft_n>0){ab.style.display='inline-flex';ab.textContent=ft_n;}else{ab.style.display='none';}

    // cards
    const key=sats.map(s=>s.name).sort().join(',');
    const grid=$('grid');
    if(key!==lastKey){
      Object.values(CH).forEach(c=>c.destroy());
      for(const k in CH)delete CH[k];
      grid.innerHTML=sats.length?sats.map(cardHTML).join(''):'<div class="no-sats"><h2>Waiting for satellites…</h2><p>Power on XIAO ESP32-S3 or run satellite_sim.py</p></div>';
      lastKey=key;
    }else{
      sats.forEach(upCard);
    }
    sats.forEach(buildChart);

    // export list + live report tab
    updateExportList(sats);
    if($('pane-reports').classList.contains('active'))updateReports(d);
    if($('pane-maintenance').classList.contains('active'))renderMaintGrid(sats);

    $('footer').textContent='EPM Gateway · '+(d.factory_name||'EPM')+' · Auto-refresh 2 s · K≥'+TH.k_warn+' WARN / K≥'+TH.k_fault+' FAULT · CF≥'+TH.cf_warn+' WARN / CF≥'+TH.cf_fault+' FAULT';
  }catch(e){
    console.warn('[refresh]',e);
    const gs=$('gstatus');if(gs){gs.textContent='⚠ API error';gs.className='chip chip-fault';}
  }
}

/* --- reports tab --- */
function updateReports(d){
  const sats=d.satellites||[];
  const ft_n=sats.filter(s=>s.alert==='FAULT').length;
  const wn_n=sats.filter(s=>s.alert==='WARN').length;
  $('r-factory').textContent=d.factory_name||'—';
  $('r-uptime').textContent=fmtUp(d.server_uptime_s);
  $('r-sats').textContent=sats.length;
  $('r-kth').textContent=TH.k_warn+' / '+TH.k_fault;
  $('r-cfth').textContent=TH.cf_warn+' / '+TH.cf_fault;
  $('r-notif').textContent=d.notify_active?'Active':'Not configured';
  $('r-wh').textContent=d.notify_active?'Configured':'Not configured';
  $('r-email').textContent=d.notify_active?'Check gateway log':'Not configured';
  const ck=(id,ok)=>{$(id).textContent=ok?'✅':'❌';};
  ck('chk-conn',sats.length>0&&sats.every(s=>s.connected));
  ck('chk-health',ft_n===0&&wn_n===0);
  ck('chk-maint',sats.length>0&&sats.every(s=>s.maint_log&&s.maint_log.last_date));
  ck('chk-auth',false); // can't detect from client side; user must use --auth
  ck('chk-notif',d.notify_active);
  ck('chk-cal',sats.length>0&&sats.every(s=>s.calibrated));
}

function updateExportList(sats){
  const el=$('export-sat-list');
  if(!sats||!sats.length){el.innerHTML='<span style="font-size:.7rem;color:var(--muted)">Connect a satellite to enable CSV exports.</span>';return;}
  el.innerHTML=sats.map(s=>
    '<a class="exp-btn" href="/api/report?name='+encodeURIComponent(s.name)+'" target="_blank">&#128202; '+s.name+' &mdash; Full HTML Report (printable PDF)</a>'
    +'<a class="exp-btn" href="/api/export?name='+encodeURIComponent(s.name)+'" download>&#128196; '+s.name+' &mdash; Latest sensor CSV</a>'
  ).join('');
}

/* --- alert log --- */
async function loadAlerts(){
  const tbody=$('alert-tbody');
  tbody.innerHTML='<tr><td class="empty-cell" colspan="7">Loading…</td></tr>';
  try{
    const r=await fetch('/api/alerts?n=500');const data=await r.json();
    if(!data.length){tbody.innerHTML='<tr><td class="empty-cell" colspan="7">No alert transitions recorded yet. State changes appear here in real time.</td></tr>';return;}
    tbody.innerHTML=data.map(ev=>{
      const tc=transCls(ev.prev,ev.alert);
      const dt=new Date(ev.time*1000);
      const dts=dt.toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'})+' '+dt.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      return '<tr>'
        +'<td style="font-size:.65rem;color:var(--muted);font-family:monospace;white-space:nowrap">'+dts+'</td>'
        +'<td style="font-weight:700">'+ev.satellite+'</td>'
        +'<td><span class="trans '+tc+'">'+ev.prev+' → '+ev.alert+'</span></td>'
        +'<td style="font-family:monospace;color:'+(ev.kurtosis>TH.k_fault?'var(--fault)':ev.kurtosis>TH.k_warn?'var(--warn)':'')+'">'+ev.kurtosis.toFixed(2)+'</td>'
        +'<td style="font-family:monospace">'+ev.crest.toFixed(2)+'</td>'
        +'<td style="font-family:monospace;color:'+(ev.z_score>3?'var(--fault)':ev.z_score>1.5?'var(--warn)':'')+'">'+ev.z_score.toFixed(1)+'</td>'
        +'<td style="font-size:.6rem;color:var(--muted);font-family:monospace">'+(ev.mac||'—')+'</td>'
        +'</tr>';
    }).join('');
    alertsLoaded=true;
  }catch(e){tbody.innerHTML='<tr><td class="empty-cell" colspan="7">Error: '+e.message+'</td></tr>';}
}

async function exportAlerts(){
  try{
    const r=await fetch('/api/alerts?n=1000');const data=await r.json();
    const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);
    a.download='epm_alert_log_'+new Date().toISOString().slice(0,10)+'.json';a.click();
    toast('Alert log exported ✓');
  }catch(e){toast('Export failed: '+e.message,false);}
}

/* --- maintenance tab --- */
async function loadMaintenance(){
  try{
    const r=await fetch('/api/maintenance');const data=await r.json();
    if(STATUS)renderMaintGrid(STATUS.satellites,data);
  }catch(e){console.warn('[maint]',e);}
}

function renderMaintGrid(sats,maintData){
  const mg=$('maint-grid');
  if(!sats||!sats.length){mg.innerHTML='<p style="color:var(--muted);font-size:.76rem">No satellites connected.</p>';return;}
  mg.className='maint-grid';
  mg.innerHTML=sats.map(s=>{
    const ml=(maintData&&maintData[s.mac])||s.maint_log||{};
    const has=ml&&ml.last_date;
    return '<div class="mc">'
      +'<div class="mc-name">'+s.name+'<span class="badge '+s.alert.toLowerCase()+'">'+s.alert+'</span></div>'
      +(has
        ?'<div class="mc-row"><span class="mc-lbl">Last Service</span><span class="mc-val">'+ml.last_date+'</span></div>'
         +'<div class="mc-row"><span class="mc-lbl">Technician</span><span class="mc-val">'+(ml.technician||'—')+'</span></div>'
         +'<div class="mc-row"><span class="mc-lbl">Type</span><span class="mc-val">'+(ml.maint_type||'—')+'</span></div>'
         +'<div class="mc-row"><span class="mc-lbl">Next Scheduled</span><span class="mc-val">'+(ml.next_date||'—')+'</span></div>'
         +(ml.notes?'<div class="mc-row" style="flex-direction:column;gap:3px"><span class="mc-lbl">Notes</span><span style="font-size:.68rem;color:var(--muted);margin-top:2px">'+ml.notes+'</span></div>':'')
         +'<div class="mc-row"><span class="mc-lbl">Updated</span><span class="mc-val" style="font-size:.62rem;color:var(--muted)">'+fmtDt(ml.updated_at)+'</span></div>'
        :'<div class="mc-empty">No maintenance record yet.</div>')
      +'<div class="mc-foot"><button class="btn btn-blue" style="width:100%" onclick="openModal(\''+s.mac+'\',\''+s.name+'\')">&#128221; Log Maintenance</button></div>'
      +'</div>';
  }).join('');
}

/* --- modal --- */
function openModal(mac,name){
  $('modal-mac').value=mac;
  $('modal-info').textContent=name+' · '+mac;
  if(STATUS){
    const s=STATUS.satellites.find(x=>x.mac===mac);
    const ml=(s&&s.maint_log)||{};
    $('f-last').value=ml.last_date||new Date().toISOString().slice(0,10);
    $('f-next').value=ml.next_date||'';
    $('f-tech').value=ml.technician||'';
    $('f-type').value=ml.maint_type||'Routine Inspection';
    $('f-notes').value=ml.notes||'';
  }
  $('maint-modal').classList.add('open');
  setTimeout(()=>$('f-tech').focus(),50);
}
function closeModal(){$('maint-modal').classList.remove('open');}
$('maint-modal').addEventListener('click',e=>{if(e.target===$('maint-modal'))closeModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});

async function submitMaint(){
  const mac=$('modal-mac').value;
  const lastDate=$('f-last').value,tech=$('f-tech').value.trim();
  if(!mac||!lastDate||!tech){toast('Last date and technician are required.',false);return;}
  try{
    const r=await fetch('/api/maintenance',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mac,last_date:lastDate,technician:tech,maint_type:$('f-type').value,notes:$('f-notes').value.trim(),next_date:$('f-next').value})});
    const d=await r.json();
    if(d.ok){toast('Maintenance record saved ✓');closeModal();loadMaintenance();refresh();}
    else toast('Save failed: '+(d.error||'unknown'),false);
  }catch(e){toast('Error: '+e.message,false);}
}

refresh();
setInterval(refresh,2000);
setInterval(()=>{if($('pane-alerts').classList.contains('active'))loadAlerts();},12000);
</script>
</body>
</html>
</html>"""


# ─── Alert history ────────────────────────────────────────────────────────────

def _log_alert_event(sat_name, mac_hex, new_alert, prev_alert, kurtosis, crest, z_score):
    """Append a state-change event to the in-memory audit trail."""
    labels = ['OK', 'WARN', 'FAULT']
    event = {
        'time':      time.time(),
        'satellite': sat_name,
        'mac':       mac_hex,
        'alert':     labels[min(new_alert, 2)],
        'prev':      labels[min(prev_alert, 2)],
        'kurtosis':  round(kurtosis, 2),
        'crest':     round(crest, 2),
        'z_score':   round(z_score, 1),
    }
    with _ALERT_HISTORY_LOCK:
        _ALERT_HISTORY.appendleft(event)


# ─── Maintenance log ──────────────────────────────────────────────────────────

def _load_maint_log(path):
    global _MAINT_LOG, _MAINT_LOG_PATH
    _MAINT_LOG_PATH = path
    if os.path.exists(path):
        try:
            with open(path) as f:
                _MAINT_LOG = json.load(f)
            print(f'[maint] Loaded {len(_MAINT_LOG)} maintenance record(s)')
        except Exception as e:
            print(f'[maint] WARNING: could not read {path}: {e}')
            _MAINT_LOG = {}


def _save_maint_log():
    if _MAINT_LOG_PATH:
        try:
            with open(_MAINT_LOG_PATH, 'w') as f:
                json.dump(_MAINT_LOG, f, indent=2)
        except Exception as e:
            print(f'[maint] WARNING: save failed: {e}')


# ─── Notifications ────────────────────────────────────────────────────────────

def _fire_notification(sat_name, mac_hex, alert_str, kurtosis, crest, z_score):
    """Rate-limited: max 1 notification per satellite per 5 minutes."""
    now = time.time()
    if now - _NOTIFY_COOLDOWN.get(mac_hex, 0) < _NOTIFY_COOLDOWN_S:
        return
    _NOTIFY_COOLDOWN[mac_hex] = now
    ts  = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    msg = (f'EPM ALERT [{alert_str}] — {sat_name}\n'
           f'Kurtosis: {kurtosis:.2f}  Crest Factor: {crest:.2f}  Z-Score: {z_score:.1f}\n'
           f'Time: {ts}\nGateway: {_FACTORY_NAME}')
    if _NOTIFY_WEBHOOK:
        threading.Thread(target=_send_webhook,
                         args=(msg, sat_name, alert_str), daemon=True).start()
    if _NOTIFY_EMAIL_CFG:
        threading.Thread(target=_send_email,
                         args=(msg, sat_name, alert_str), daemon=True).start()


def _send_webhook(msg, sat_name, alert_str):
    url   = _NOTIFY_WEBHOOK
    emoji = '\U0001f6a8' if alert_str == 'FAULT' else '⚠️'
    color = 0xef4444 if alert_str == 'FAULT' else 0xf59e0b
    if 'discord.com/api/webhooks' in url:
        payload = json.dumps({
            'username': 'EPM Monitor',
            'embeds': [{'title': f'{emoji} {alert_str} — {sat_name}',
                        'description': msg, 'color': color,
                        'timestamp': datetime.datetime.utcnow().isoformat()}],
        }).encode()
    elif 'hooks.slack.com' in url or 'slack.com/services' in url:
        payload = json.dumps({
            'text': f'{emoji} *{alert_str}* — {sat_name}',
            'blocks': [{'type': 'section',
                        'text': {'type': 'mrkdwn', 'text': f'```{msg}```'}}],
        }).encode()
    else:
        payload = json.dumps({'alert': alert_str, 'satellite': sat_name,
                               'message': msg, 'timestamp': time.time()}).encode()
    try:
        req = _urllib_req.Request(url, data=payload,
                                   headers={'Content-Type': 'application/json'})
        with _urllib_req.urlopen(req, timeout=10) as resp:
            print(f'[notify] Webhook → HTTP {resp.status}  ({alert_str} / {sat_name})')
    except Exception as e:
        print(f'[notify] Webhook failed: {e}')


def _send_email(msg, sat_name, alert_str):
    cfg = _NOTIFY_EMAIL_CFG
    m   = MIMEText(msg)
    m['Subject'] = f'EPM {alert_str}: {sat_name} — {_FACTORY_NAME}'
    m['From']    = cfg['from']
    m['To']      = cfg['to']
    try:
        with smtplib.SMTP(cfg['host'], cfg.get('port', 587), timeout=15) as s:
            s.ehlo()
            s.starttls()
            if cfg.get('user'):
                s.login(cfg['user'], cfg['pass'])
            s.send_message(m)
        print(f'[notify] Email sent ({alert_str} / {sat_name})')
    except Exception as e:
        print(f'[notify] Email failed: {e}')


def _sat_health(sat):
    """Compute 0–100 health score, maintenance recommendation, and RUL estimate.

    Returns (health_score, maintenance_str, maintenance_days, rul_days).
    rul_days is None when kurtosis is stable or trending downward; otherwise it
    is the estimated number of days until kurtosis crosses the FAULT threshold
    based on a linear regression over the most recent history window.
    """
    total = max(sat.frame_count, 1)
    score = 100.0
    score -= (sat.warn_frames  / total) * 25.0   # max −25 for sustained WARN
    score -= (sat.fault_frames / total) * 60.0   # max −60 for sustained FAULT
    score -= min(sat.last_z * 3.0, 15.0)          # max −15 for high z-score
    score  = max(0.0, min(100.0, score))

    if sat.fault_frames > 0 and score < 40:
        maint, days = "CRITICAL — Immediate inspection required", 0
    elif score < 50:
        maint, days = "DEGRADED — Inspect within 7 days", 7
    elif score < 70:
        maint, days = "MONITOR — Schedule inspection within 30 days", 30
    elif score < 85:
        maint, days = "GOOD — Routine inspection within 90 days", 90
    else:
        maint, days = "EXCELLENT — Routine inspection in 180 days", 180

    # ── Remaining Useful Life (RUL) estimate from kurtosis trend ─────────────
    rul_days = None
    hist = list(sat.history_kurtosis)
    n    = len(hist)
    if n >= 10 and sat.fps > 0.01:
        xs    = np.arange(n, dtype=np.float64)
        ys    = np.array(hist, dtype=np.float64)
        slope = float(np.polyfit(xs, ys, 1)[0])   # frames per unit kurtosis rise
        current_k = float(ys[-1])
        # Only compute if trending upward and not yet at fault threshold
        if slope > 0.005 and current_k < K_FAULT:
            frames_to_fault = (K_FAULT - current_k) / slope
            rul_seconds = frames_to_fault / sat.fps
            rul_days    = max(0.0, round(rul_seconds / 86400.0, 1))

    return round(score, 1), maint, days, rul_days


def _safe_f(v, default=0.0):
    """Return float v if finite; replace NaN/Inf with default so json.dumps never raises."""
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _build_status_json():
    now = time.time()
    with _sat_lock:
        sats = list(_satellites.values())

    sat_list = []
    for s in sats:
        health, maint, maint_days, rul_days = _sat_health(s)
        m = {}
        if s.last_frame:
            m = {
                'mic_rms':         round(_safe_f(s.last_frame.get('mic_rms',  0)), 6),
                'mic_kurtosis':    round(_safe_f(s.last_frame.get('mic_kurtosis', 0)), 2),
                'mic_crest':       round(_safe_f(s.last_frame.get('mic_crest', 0)), 2),
                'imu_rms':         round(_safe_f(s.last_frame.get('imu_rms',  0)), 5),
                'imu_crest':       round(_safe_f(s.last_frame.get('imu_crest', 0)), 2),
                'high_band_ratio': round(_safe_f(s.last_hb), 3),
            }
        with _MAINT_LOG_LOCK:
            maint_rec = dict(_MAINT_LOG.get(s.mac_hex, {}))
        sat_list.append({
            'name':             s.name,
            'mac':              s.mac_hex,
            'fw':               f"{s.fw_major}.{s.fw_minor}",
            'alert':            ['OK', 'WARN', 'FAULT'][min(int(s.sent_alert), 2)],
            'connected':        s.connected,
            'uptime_s':         int(now - s.connect_t),
            'frame_count':      s.frame_count,
            'fps':              round(_safe_f(s.fps), 1),
            'calibrated':       s.calibrated,
            'health_score':     health,
            'maintenance':      maint,
            'maintenance_days': maint_days,
            'rul_days':         rul_days,
            'warn_frames':      s.warn_frames,
            'fault_frames':     s.fault_frames,
            'last_fault_t':     s.last_fault_t,
            'z_score':          round(_safe_f(s.last_z), 2),
            'fault_type':       s.fault_type,
            'metrics':          m,
            'maint_log':        maint_rec,
            'history': {
                'alerts':   list(s.history_alerts),
                'kurtosis': [round(_safe_f(v), 2) for v in s.history_kurtosis],
                'crest':    [round(_safe_f(v), 2) for v in s.history_crest],
            },
        })

    return json.dumps({
        'factory_name':       _FACTORY_NAME,
        'server_uptime_s':    int(now - _SERVER_START_T),
        'timestamp':          now,
        'satellite_count':    sum(1 for s in sat_list if s['connected']),
        'total_faults_today': sum(s['fault_frames'] for s in sat_list),
        'notify_active':      bool(_NOTIFY_WEBHOOK or _NOTIFY_EMAIL_CFG),
        'thresholds': {
            'k_warn':  K_WARN, 'k_fault': K_FAULT,
            'cf_warn': CREST_WARN, 'cf_fault': CREST_FAULT,
        },
        'satellites': sat_list,
    })


def _generate_report_html(sat_name=None):
    """Generate a professional, print-ready HTML inspection report from live data."""
    import html as _html

    def esc(s):
        return _html.escape(str(s))

    def fmt_dt(ts):
        if not ts:
            return 'N/A'
        return datetime.datetime.fromtimestamp(ts).strftime('%b %d, %Y  %H:%M')

    def fmt_up(s):
        if s < 60:
            return f'{s}s'
        if s < 3600:
            return f'{s//60}m {s%60:02d}s'
        return f'{s//3600}h {(s%3600)//60:02d}m'

    def fmt_rul(d):
        if d is None:
            return 'Stable — no upward trend'
        if d < 0.05:
            return '⚠ Fault threshold reached'
        if d < 1:
            return '⚠ < 1 day'
        if d < 30:
            return f'~{round(d)} days'
        return f'~{round(d)} days (stable)'

    def status_badge(a):
        cfg = {'OK': ('#166534','#dcfce7'), 'WARN': ('#92400e','#fef3c7'), 'FAULT': ('#991b1b','#fee2e2')}
        fg, bg = cfg.get(a, ('#374151','#f3f4f6'))
        return f'<span style="background:{bg};color:{fg};padding:2px 10px;border-radius:4px;font-weight:700;font-size:.78rem;border:1px solid {fg}30">{a}</span>'

    def health_bar(h):
        c = '#16a34a' if h >= 75 else '#d97706' if h >= 50 else '#dc2626'
        return (f'<span style="display:inline-flex;align-items:center;gap:8px">'
                f'<span style="display:inline-block;width:120px;height:7px;background:#e5e7eb;border-radius:4px;overflow:hidden">'
                f'<span style="display:block;width:{h}%;height:100%;background:{c};border-radius:4px"></span></span>'
                f'<strong style="color:{c}">{h}%</strong></span>')

    now_ts  = time.time()
    now_str = datetime.datetime.now().strftime('%A, %B %d, %Y at %H:%M:%S')

    with _sat_lock:
        all_sats = list(_satellites.values())
    sats = [s for s in all_sats if s.name == sat_name] if sat_name else all_sats

    with _ALERT_HISTORY_LOCK:
        all_alerts = list(_ALERT_HISTORY)
    with _MAINT_LOG_LOCK:
        maint = dict(_MAINT_LOG)

    sat_rows = []
    for s in sats:
        health, maint_str, maint_days, rul_days = _sat_health(s)
        ml = maint.get(s.mac_hex, {})
        m  = s.last_frame or {}
        sat_alerts = [ev for ev in all_alerts if ev['satellite'] == s.name]
        sat_rows.append(dict(s=s, health=health, maint_str=maint_str,
                             maint_days=maint_days, rul_days=rul_days,
                             ml=ml, m=m, alerts=sat_alerts))

    fault_n  = sum(1 for r in sat_rows if r['s'].sent_alert == EPM_ALERT_FAULT)
    warn_n   = sum(1 for r in sat_rows if r['s'].sent_alert == EPM_ALERT_WARN)
    ok_n     = len(sat_rows) - fault_n - warn_n
    avg_h    = round(sum(r['health'] for r in sat_rows) / len(sat_rows), 1) if sat_rows else 0
    tot_fault_evts = sum(r['s'].fault_frames for r in sat_rows)
    tot_warn_evts  = sum(r['s'].warn_frames  for r in sat_rows)
    scope_alerts   = [ev for ev in all_alerts if not sat_name or ev['satellite'] == sat_name]

    if fault_n > 0:
        risk, risk_c, risk_bg = 'HIGH', '#dc2626', '#fef2f2'
    elif warn_n > 0:
        risk, risk_c, risk_bg = 'MEDIUM', '#d97706', '#fffbeb'
    elif avg_h >= 75:
        risk, risk_c, risk_bg = 'LOW', '#16a34a', '#f0fdf4'
    else:
        risk, risk_c, risk_bg = 'MEDIUM', '#d97706', '#fffbeb'

    h = []
    W = h.append
    title = f'EPM Report — {esc(sat_name) if sat_name else "All Machines"}'

    W(f'''<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><title>{title}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:#1f2937;background:#fff;font-size:13px;line-height:1.65}}
@media print{{.no-print{{display:none!important}}body{{font-size:11px}}.page-break{{page-break-before:always}}h2{{page-break-after:avoid}}}}
.cover{{background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);color:#fff;padding:36px 40px 30px}}
.cover h1{{font-size:1.55rem;font-weight:700;margin-bottom:3px}}
.cover .sub{{font-size:.78rem;opacity:.72;margin-top:5px}}
.cover-meta{{display:flex;flex-wrap:wrap;gap:24px;margin-top:18px;padding-top:18px;border-top:1px solid rgba(255,255,255,.15)}}
.cover-stat{{font-size:.72rem;opacity:.75}}.cover-stat strong{{display:block;font-size:.95rem;color:#fff;opacity:1;margin-bottom:1px}}
.body{{padding:28px 36px}}
.section{{margin-bottom:28px}}
h2{{font-size:.88rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:#374151;border-bottom:2px solid #e5e7eb;padding-bottom:6px;margin-bottom:14px;display:flex;align-items:center;gap:8px}}
.risk-box{{border:2px solid;border-radius:8px;padding:13px 16px;margin-bottom:16px;display:flex;align-items:flex-start;gap:12px}}
.risk-icon{{font-size:1.4rem;flex-shrink:0;margin-top:1px}}
.risk-label{{font-size:1rem;font-weight:800;margin-bottom:3px}}
.risk-note{{font-size:.78rem;color:#374151;line-height:1.55}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:9px;margin-bottom:16px}}
.kpi{{border:1px solid #e5e7eb;border-radius:8px;padding:11px 12px;text-align:center}}
.kpi-val{{font-size:1.75rem;font-weight:800;line-height:1;margin:3px 0}}
.kpi-lbl{{font-size:.6rem;text-transform:uppercase;letter-spacing:.07em;color:#6b7280}}
table{{width:100%;border-collapse:collapse;font-size:.8rem;margin-bottom:6px}}
th{{background:#f3f4f6;padding:7px 10px;text-align:left;font-size:.65rem;text-transform:uppercase;letter-spacing:.07em;color:#6b7280;border:1px solid #e5e7eb;font-weight:600}}
td{{padding:7px 10px;border:1px solid #e5e7eb;vertical-align:top}}
tr:nth-child(even) td{{background:#f9fafb}}
.sat-card{{border:1px solid #e5e7eb;border-radius:10px;margin-bottom:20px;overflow:hidden}}
.sat-card-head{{background:#f8fafc;padding:11px 15px;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.sat-card-head h3{{font-size:.92rem;font-weight:700;flex:1}}
.sat-card-body{{padding:14px 16px}}
.met-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-bottom:12px}}
.met{{border:1px solid #e5e7eb;border-radius:6px;padding:8px 10px}}
.met-lbl{{font-size:.57rem;text-transform:uppercase;letter-spacing:.05em;color:#9ca3af}}
.met-val{{font-size:.95rem;font-weight:700;margin-top:1px;font-family:monospace}}
.rec{{padding:9px 12px;border-radius:6px;margin-bottom:10px;border-left:4px solid;font-size:.8rem}}
.rec.ok{{background:#f0fdf4;border-color:#22c55e}}.rec.warn{{background:#fffbeb;border-color:#f59e0b}}.rec.fault{{background:#fef2f2;border-color:#ef4444}}
.sub-h{{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;margin:12px 0 6px;border-bottom:1px solid #f3f4f6;padding-bottom:4px}}
.no-data{{color:#9ca3af;font-style:italic;font-size:.78rem;padding:6px 0}}
.tag{{display:inline-block;padding:1px 7px;border-radius:4px;font-size:.68rem;font-weight:700}}
.t-ok{{background:#dcfce7;color:#166534}}.t-warn{{background:#fef3c7;color:#92400e}}.t-fault{{background:#fee2e2;color:#991b1b}}
.analysis-box{{background:#f8fafc;border:1px solid #e5e7eb;border-radius:7px;padding:11px 13px;margin-bottom:10px;font-size:.8rem}}
.analysis-box h4{{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:#6b7280;margin-bottom:6px}}
.analysis-row{{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #f3f4f6}}
.analysis-row:last-child{{border-bottom:none}}
.analysis-key{{color:#6b7280}}.analysis-val{{font-weight:600}}
.recom-list li{{margin-bottom:5px;font-size:.8rem;padding-left:4px}}
.footer{{text-align:center;padding:18px;color:#9ca3af;font-size:.68rem;border-top:1px solid #e5e7eb;margin-top:10px}}
.btn-print{{position:fixed;bottom:20px;right:20px;background:#1e3a5f;color:#fff;border:none;padding:10px 18px;border-radius:7px;cursor:pointer;font-size:.8rem;font-weight:700;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:999;transition:background .15s}}
.btn-print:hover{{background:#2563eb}}
.conf-note{{font-size:.68rem;color:#9ca3af;text-align:right;margin-bottom:4px}}
</style></head><body>
<button class="btn-print no-print" onclick="window.print()">&#128424; Print / Save as PDF</button>

<div class="cover">
  <div style="font-size:.62rem;opacity:.55;text-transform:uppercase;letter-spacing:.12em;margin-bottom:6px">OFFICIAL INSPECTION REPORT &mdash; CONFIDENTIAL</div>
  <h1>EPM Industrial Monitoring Report</h1>
  <div class="sub">EdgeAI Predictive Maintenance System &mdash; {esc(_FACTORY_NAME)}</div>
  <div class="cover-meta">
    <div class="cover-stat"><strong>{now_str}</strong>Generated</div>
    <div class="cover-stat"><strong>{esc(sat_name) if sat_name else str(len(sats)) + " machine" + ("s" if len(sats)!=1 else "")}</strong>Scope</div>
    <div class="cover-stat"><strong>{fmt_up(int(now_ts - _SERVER_START_T))}</strong>Gateway Uptime</div>
    <div class="cover-stat"><strong>{len(scope_alerts)}</strong>Alert Transitions Logged</div>
    <div class="cover-stat"><strong>{tot_fault_evts}</strong>Total Fault Frames</div>
  </div>
</div>
<div class="body">''')

    # ── EXECUTIVE SUMMARY ─────────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#9881; Executive Summary</h2>')
    W(f'<div class="risk-box" style="background:{risk_bg};border-color:{risk_c}">')
    W(f'<div class="risk-icon">{"🚨" if fault_n>0 else "⚠" if warn_n>0 else "✅"}</div>')
    W(f'<div><div class="risk-label" style="color:{risk_c}">FACTORY RISK LEVEL: {risk}</div>')
    if fault_n > 0:
        names = ', '.join(r['s'].name for r in sat_rows if r['s'].sent_alert==EPM_ALERT_FAULT)
        W(f'<div class="risk-note"><strong>{fault_n} machine(s) currently in FAULT:</strong> {esc(names)}.'
          f' Immediate inspection required. Do not operate affected machinery until cleared and signed off by a qualified technician.</div>')
    elif warn_n > 0:
        names = ', '.join(r['s'].name for r in sat_rows if r['s'].sent_alert==EPM_ALERT_WARN)
        W(f'<div class="risk-note"><strong>{warn_n} machine(s) showing elevated vibration (WARN):</strong> {esc(names)}.'
          f' Schedule inspection within 7 days. Monitor closely for escalation to FAULT.</div>')
    else:
        W('<div class="risk-note">All monitored machines are operating within normal vibration parameters. Continue routine monitoring and scheduled maintenance intervals.</div>')
    W('</div></div>')

    W('<div class="kpi-grid">')
    kpis = [
        ('Total Machines', len(sats), '#1e3a5f'),
        ('Healthy (OK)', ok_n, '#16a34a'),
        ('Warning', warn_n, '#d97706'),
        ('Fault', fault_n, '#dc2626'),
        ('Avg Health', f'{avg_h}%', '#16a34a' if avg_h>=75 else '#d97706' if avg_h>=50 else '#dc2626'),
        ('Fault Events', tot_fault_evts, '#dc2626' if tot_fault_evts>0 else '#374151'),
    ]
    for lbl, val, clr in kpis:
        W(f'<div class="kpi"><div class="kpi-lbl">{lbl}</div><div class="kpi-val" style="color:{clr}">{val}</div></div>')
    W('</div>')
    W('</div>')  # /section

    # ── MACHINE STATUS TABLE ──────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#128202; Machine Status at a Glance</h2>')
    W('<table><thead><tr><th>Machine</th><th>Status</th><th>Health</th><th>Kurtosis</th>'
      '<th>RUL Estimate</th><th>Fault Events</th><th>Last Fault</th><th>Last Maintenance</th></tr></thead><tbody>')
    for r in sat_rows:
        s, ml = r['s'], r['ml']
        al = ['OK','WARN','FAULT'][min(int(s.sent_alert),2)]
        k  = (r['m'].get('mic_kurtosis',0))
        kc = '#dc2626' if k>=K_FAULT else '#d97706' if k>=K_WARN else '#16a34a'
        fc = '#dc2626' if s.fault_frames>0 else '#374151'
        W(f'<tr><td><strong>{esc(s.name)}</strong><br>'
          f'<span style="font-family:monospace;font-size:.68rem;color:#9ca3af">{esc(s.mac_hex)}</span></td>'
          f'<td>{status_badge(al)}</td>'
          f'<td>{health_bar(r["health"])}</td>'
          f'<td style="font-family:monospace;color:{kc};font-weight:700">{k:.2f}</td>'
          f'<td style="font-size:.78rem">{fmt_rul(r["rul_days"])}</td>'
          f'<td style="text-align:center;font-weight:700;color:{fc}">{s.fault_frames}</td>'
          f'<td style="font-size:.76rem">{fmt_dt(s.last_fault_t)}</td>'
          f'<td style="font-size:.76rem">{esc(ml.get("last_date","— not logged"))}'
          + ('<br><span style="color:#9ca3af;font-size:.7rem">' + esc(ml['technician']) + '</span>' if ml.get('technician') else '')
          + '</td></tr>')
    W('</tbody></table>')
    W('</div>')

    # ── PER-MACHINE DETAIL ────────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#128295; Machine Detail Reports</h2>')
    for r in sat_rows:
        s, ml, m = r['s'], r['ml'], r['m']
        al = ['OK','WARN','FAULT'][min(int(s.sent_alert),2)]
        mc = al.lower()
        mic_k  = m.get('mic_kurtosis', 0)
        mic_cf = m.get('mic_crest', 0)
        mic_rms = m.get('mic_rms', 0)
        hb_pct = m.get('high_band_ratio', 0) * 100
        z = s.last_z
        fault_rate = (s.fault_frames / max(s.frame_count, 1)) * 100
        warn_rate  = (s.warn_frames  / max(s.frame_count, 1)) * 100
        kc = '#dc2626' if mic_k>=K_FAULT else '#d97706' if mic_k>=K_WARN else '#16a34a'
        zc = '#dc2626' if z>3 else '#d97706' if z>1.5 else '#16a34a'

        W(f'<div class="sat-card">')
        W(f'<div class="sat-card-head">'
          f'<h3>{esc(s.name)}</h3>{status_badge(al)}'
          f'&nbsp;&nbsp;<span style="font-family:monospace;font-size:.68rem;color:#9ca3af">{esc(s.mac_hex)}</span>'
          f'&nbsp;·&nbsp;<span style="font-size:.7rem;color:#9ca3af">FW {s.fw_major}.{s.fw_minor}</span>'
          f'&nbsp;·&nbsp;<span style="font-size:.7rem;color:#9ca3af">{"✓ Calibrated" if s.calibrated else "⧖ Calibrating"}</span>'
          f'</div>')
        W('<div class="sat-card-body">')

        # Metrics grid
        W('<div class="met-grid">')
        mets = [
            ('Kurtosis', f'{mic_k:.2f}', kc),
            ('Crest Factor', f'{mic_cf:.2f}', None),
            ('High-Band %', f'{hb_pct:.1f}%', None),
            ('Mic RMS', f'{mic_rms:.5f}', None),
            ('Z-Score', f'{z:.2f}', zc),
            ('Frame Rate', f'{s.fps:.1f} fps', None),
        ]
        for lbl, val, clr in mets:
            cs = f'style="color:{clr}"' if clr else ''
            W(f'<div class="met"><div class="met-lbl">{lbl}</div><div class="met-val" {cs}>{val}</div></div>')
        W('</div>')

        # Health
        W(f'<div style="margin-bottom:10px">{health_bar(r["health"])}&nbsp;&nbsp;'
          f'<span style="font-size:.78rem;color:#6b7280">{esc(r["maint_str"])}</span></div>')

        # Condition box
        ft = s.fault_type or 'Normal'
        ft_color = ('#dc2626' if 'Fault' in ft or 'Severe' in ft
                    else '#d97706' if ft != 'Normal'
                    else '#16a34a')
        W(f'<div class="rec {mc}">')
        W(f'<strong>Condition:</strong> {esc(r["maint_str"])}')
        W(f'<br><strong>Fault Analysis:</strong> '
          f'<span style="color:{ft_color};font-weight:700">{esc(ft)}</span>')
        if r['rul_days'] is not None:
            W(f'<br><strong>Estimated RUL:</strong> {fmt_rul(r["rul_days"])}')
        W('</div>')

        # Analysis box
        W('<div class="analysis-box"><h4>Session Analysis</h4>')
        trend = 'Worsening' if fault_rate > 5 else 'Warning trend' if warn_rate > 15 else 'Stable'
        trend_c = '#dc2626' if 'Worsening' in trend else '#d97706' if 'Warning' in trend else '#16a34a'
        W(f'<div class="analysis-row"><span class="analysis-key">Frames Analyzed</span><span class="analysis-val">{s.frame_count:,}</span></div>')
        W(f'<div class="analysis-row"><span class="analysis-key">Fault Rate</span><span class="analysis-val" style="color:{"#dc2626" if fault_rate>5 else "#374151"}">{fault_rate:.1f}%  ({s.fault_frames} frames)</span></div>')
        W(f'<div class="analysis-row"><span class="analysis-key">Warn Rate</span><span class="analysis-val" style="color:{"#d97706" if warn_rate>10 else "#374151"}">{warn_rate:.1f}%  ({s.warn_frames} frames)</span></div>')
        W(f'<div class="analysis-row"><span class="analysis-key">Vibration Trend</span><span class="analysis-val" style="color:{trend_c}">{trend}</span></div>')
        W(f'<div class="analysis-row"><span class="analysis-key">Alert Transitions Logged</span><span class="analysis-val">{len(r["alerts"])}</span></div>')
        W('</div>')

        # Alert history (mini)
        W(f'<div class="sub-h">Alert History — {len(r["alerts"])} transitions</div>')
        if r['alerts']:
            W('<table><thead><tr><th>Time</th><th>From</th><th>To</th><th>Kurtosis</th><th>Crest</th><th>Z</th></tr></thead><tbody>')
            for ev in r['alerts'][:12]:
                dt_s = datetime.datetime.fromtimestamp(ev['time']).strftime('%b %d  %H:%M:%S')
                tc = 'fault' if ev['alert']=='FAULT' else 'warn' if ev['alert']=='WARN' else 'ok'
                W(f'<tr><td style="font-family:monospace;font-size:.72rem">{dt_s}</td>'
                  f'<td><span class="tag t-ok">{ev["prev"]}</span></td>'
                  f'<td><span class="tag t-{tc}">{ev["alert"]}</span></td>'
                  f'<td style="font-family:monospace">{ev["kurtosis"]:.2f}</td>'
                  f'<td style="font-family:monospace">{ev["crest"]:.2f}</td>'
                  f'<td style="font-family:monospace">{ev["z_score"]:.1f}</td></tr>')
            if len(r['alerts']) > 12:
                W(f'<tr><td colspan="6" style="text-align:center;color:#9ca3af;font-size:.72rem">… {len(r["alerts"])-12} more events in Full Alert Log below</td></tr>')
            W('</tbody></table>')
        else:
            W('<p class="no-data">No alert transitions recorded — machine has been stable this session.</p>')

        # Maintenance
        W('<div class="sub-h">Maintenance Record</div>')
        if ml and ml.get('last_date'):
            today = datetime.datetime.now().strftime('%Y-%m-%d')
            overdue = ml.get('next_date','') and ml['next_date'] < today
            W('<table><thead><tr><th>Last Service</th><th>Technician</th><th>Service Type</th>'
              '<th>Next Scheduled</th><th>Record Updated</th></tr></thead><tbody>')
            W(f'<tr><td>{esc(ml.get("last_date","—"))}</td>'
              f'<td>{esc(ml.get("technician","—"))}</td>'
              f'<td>{esc(ml.get("maint_type","—"))}</td>'
              f'<td style="{"color:#dc2626;font-weight:700" if overdue else ""}">'
              f'{esc(ml.get("next_date","—"))}{"  ⚠ OVERDUE" if overdue else ""}</td>'
              f'<td style="font-size:.74rem;color:#9ca3af">{fmt_dt(ml.get("updated_at",0))}</td></tr>')
            W('</tbody></table>')
            if ml.get('notes'):
                W(f'<p style="margin-top:6px;font-size:.78rem"><strong>Notes:</strong> {esc(ml["notes"])}</p>')
        else:
            W('<p class="no-data">⚠ No maintenance record found. Log a maintenance entry via the dashboard to satisfy compliance and insurance requirements.</p>')

        # Recommendations
        W('<div class="sub-h">Recommendations</div><ul class="recom-list">')
        if al == 'FAULT':
            W('<li><strong style="color:#dc2626">🚨 IMMEDIATE ACTION:</strong> Machine is in FAULT state. Halt operation, perform bearing inspection, replace if necessary.</li>')
        elif al == 'WARN':
            W('<li><strong style="color:#d97706">⚠ SCHEDULE INSPECTION:</strong> Elevated vibration detected. Perform bearing inspection within 7 days.</li>')
        if fault_rate > 10:
            W(f'<li>High fault event rate ({fault_rate:.0f}%). Recommend vibration analysis, lubrication check, and alignment verification.</li>')
        if not ml.get('last_date'):
            W('<li>No maintenance record on file. Log service history to establish compliance baseline and enable predictive scheduling.</li>')
        if r['rul_days'] is not None and r['rul_days'] < 30:
            W(f'<li>Estimated RUL is {fmt_rul(r["rul_days"])}. Order replacement bearings and schedule planned downtime before failure occurs.</li>')
        if al == 'OK' and not fault_rate:
            W('<li>Machine is operating normally. Continue routine maintenance schedule and log service dates after each inspection.</li>')
        W('</ul>')

        W('</div></div>')  # /sat-card-body, /sat-card

    W('</div>')  # /section

    # ── FULL ALERT AUDIT TRAIL ────────────────────────────────────────────────
    W('<div class="section page-break">')
    W(f'<h2>&#128203; Full Alert Audit Trail ({len(scope_alerts)} events)</h2>')
    if scope_alerts:
        W('<table><thead><tr><th>#</th><th>Timestamp (local)</th><th>Machine</th>'
          '<th>From</th><th>To</th><th>Kurtosis</th><th>Crest</th><th>Z-Score</th><th>MAC</th></tr></thead><tbody>')
        for i, ev in enumerate(scope_alerts, 1):
            dt_s = datetime.datetime.fromtimestamp(ev['time']).strftime('%Y-%m-%d  %H:%M:%S')
            tc = 'fault' if ev['alert']=='FAULT' else 'warn' if ev['alert']=='WARN' else 'ok'
            W(f'<tr><td style="color:#9ca3af;font-size:.72rem">{i}</td>'
              f'<td style="font-family:monospace;font-size:.72rem">{dt_s}</td>'
              f'<td style="font-weight:700">{esc(ev["satellite"])}</td>'
              f'<td><span class="tag t-ok">{ev["prev"]}</span></td>'
              f'<td><span class="tag t-{tc}">{ev["alert"]}</span></td>'
              f'<td style="font-family:monospace">{ev["kurtosis"]:.2f}</td>'
              f'<td style="font-family:monospace">{ev["crest"]:.2f}</td>'
              f'<td style="font-family:monospace">{ev["z_score"]:.1f}</td>'
              f'<td style="font-family:monospace;font-size:.68rem;color:#9ca3af">{esc(ev.get("mac","—"))}</td></tr>')
        W('</tbody></table>')
    else:
        W('<p class="no-data">No alert transitions recorded this session. System has remained stable since gateway startup.</p>')
    W('<p style="font-size:.7rem;color:#9ca3af;margin-top:6px">'
      'This audit trail captures every machine state change with epoch-accurate timestamps keyed to hardware MAC address. '
      'Events are listed newest-first. For permanent archival export this report or use the Alert Log JSON export from the dashboard.</p>')
    W('</div>')

    # ── MAINTENANCE SUMMARY ───────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#128295; Maintenance Log Summary</h2>')
    maint_rows = [(mac, rec) for mac, rec in maint.items()
                  if not sat_name or any(s.mac_hex == mac for s in sats)]
    if maint_rows:
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        W('<table><thead><tr><th>Machine</th><th>MAC</th><th>Last Service</th>'
          '<th>Technician</th><th>Service Type</th><th>Next Scheduled</th><th>Notes</th></tr></thead><tbody>')
        for mac, rec in maint_rows:
            name = next((s.name for s in sats if s.mac_hex == mac), mac)
            overdue = rec.get('next_date','') and rec['next_date'] < today
            W(f'<tr><td><strong>{esc(name)}</strong></td>'
              f'<td style="font-family:monospace;font-size:.7rem;color:#9ca3af">{esc(mac)}</td>'
              f'<td>{esc(rec.get("last_date","—"))}</td>'
              f'<td>{esc(rec.get("technician","—"))}</td>'
              f'<td>{esc(rec.get("maint_type","—"))}</td>'
              f'<td style="{"color:#dc2626;font-weight:700" if overdue else ""}">'
              f'{esc(rec.get("next_date","—"))}{"  ⚠ OVERDUE" if overdue else ""}</td>'
              f'<td style="font-size:.75rem">{esc(rec.get("notes",""))}</td></tr>')
        W('</tbody></table>')
    else:
        W('<p class="no-data">⚠ No maintenance records on file. Log maintenance via the dashboard Maintenance tab to meet compliance requirements.</p>')
    W('</div>')

    # ── COMPLIANCE SUMMARY ────────────────────────────────────────────────────
    W('<div class="section">')
    W('<h2>&#9989; Compliance Summary</h2>')
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    checks = [
        ('All machines connected',           all(s.connected for s in sats) and bool(sats)),
        ('No active FAULT conditions',        fault_n == 0),
        ('No active WARN conditions',         warn_n  == 0),
        ('All sensors calibrated',            all(s.calibrated for s in sats) and bool(sats)),
        ('All machines have maintenance log', all(bool(maint.get(s.mac_hex, {}).get('last_date')) for s in sats) and bool(sats)),
        ('No overdue maintenance scheduled',  all(not (maint.get(s.mac_hex,{}).get('next_date','') and maint.get(s.mac_hex,{})['next_date'] < today) for s in sats)),
        ('Audit trail active',                True),
    ]
    W('<table><thead><tr><th>Requirement</th><th>Status</th></tr></thead><tbody>')
    for label, ok in checks:
        icon, clr = ('✅ PASS', '#16a34a') if ok else ('❌ FAIL', '#dc2626')
        W(f'<tr><td>{label}</td><td style="color:{clr};font-weight:700">{icon}</td></tr>')
    W('</tbody></table>')
    W('</div>')

    # ── FOOTER ────────────────────────────────────────────────────────────────
    W(f'''<div class="footer">
  EPM Industrial Monitoring Report &mdash; {esc(_FACTORY_NAME)}<br>
  Generated: {now_str} &mdash; Confidential &mdash; For authorized personnel only<br>
  Scope: {"Machine: " + esc(sat_name) if sat_name else "All " + str(len(sats)) + " monitored machines"}
  &mdash; EdgeAI Predictive Maintenance System
</div>
</div></body></html>''')

    return '\n'.join(h)


class _DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _auth_ok(self):
        if _AUTH_PASS is None:
            return True
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Basic '):
            return False
        try:
            user, pw = base64.b64decode(auth[6:]).decode().split(':', 1)
            return user == (_AUTH_USER or 'admin') and pw == _AUTH_PASS
        except Exception:
            return False

    def _require_auth(self):
        if self._auth_ok():
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="EPM Dashboard"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    # ── Response helpers ──────────────────────────────────────────────────────
    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype, download_name=None):
        with open(path, 'rb') as f:
            data = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Access-Control-Allow-Origin', '*')
        if download_name:
            self.send_header('Content-Disposition',
                             f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(data)

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        if not self._require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        qs     = urllib.parse.parse_qs(parsed.query)

        if path in ('/', '/index.html'):
            self._send(200, 'text/html; charset=utf-8', _DASHBOARD_HTML)

        elif path == '/api/status':
            try:
                self._send(200, 'application/json', _build_status_json())
            except Exception as exc:
                fallback = json.dumps({
                    'error': str(exc), 'satellites': [],
                    'satellite_count': 0, 'total_faults_today': 0,
                    'factory_name': _FACTORY_NAME, 'server_uptime_s': 0,
                    'notify_active': False,
                    'thresholds': {'k_warn': K_WARN, 'k_fault': K_FAULT,
                                   'cf_warn': CREST_WARN, 'cf_fault': CREST_FAULT},
                })
                self._send(200, 'application/json', fallback)
                import traceback
                print(f'[dash] /api/status error: {exc}\n{traceback.format_exc()}')

        elif path == '/api/alerts':
            n = int(qs.get('n', ['200'])[0])
            with _ALERT_HISTORY_LOCK:
                data = list(_ALERT_HISTORY)[:n]
            self._send(200, 'application/json', json.dumps(data))

        elif path == '/api/maintenance':
            with _MAINT_LOG_LOCK:
                self._send(200, 'application/json', json.dumps(_MAINT_LOG))

        elif path == '/api/export':
            name    = qs.get('name', [''])[0]
            log_dir = os.path.join(os.path.dirname(__file__), 'logs')
            pattern = f'epm_{name}_*.csv' if name else 'epm_*.csv'
            files   = sorted(glob.glob(os.path.join(log_dir, pattern)), reverse=True)
            if files:
                self._send_file(files[0], 'text/csv', os.path.basename(files[0]))
            else:
                self._send(404, 'text/plain', 'No CSV data found for this satellite')

        elif path == '/api/report':
            sat_name   = qs.get('name', [''])[0] or None
            report_html = _generate_report_html(sat_name)
            self._send(200, 'text/html; charset=utf-8', report_html)

        elif path == '/manifest.json':
            manifest = json.dumps({
                'name': f'EPM Monitor — {_FACTORY_NAME}',
                'short_name': 'EPM',
                'description': 'EdgeAI Predictive Maintenance Dashboard',
                'start_url': '/',
                'display': 'standalone',
                'background_color': '#07111e',
                'theme_color': '#07111e',
                'icons': [
                    {'src': 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">⚙</text></svg>',
                     'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any maskable'}
                ],
            })
            self._send(200, 'application/manifest+json', manifest)

        else:
            self._send(404, 'text/plain', 'Not found')

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        if not self._require_auth():
            return
        path   = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)

        if path == '/api/maintenance':
            try:
                data = json.loads(body)
                mac  = data.get('mac', '').strip()
                if not mac:
                    self._send(400, 'application/json', '{"error":"mac required"}')
                    return
                record = {
                    'last_date':  data.get('last_date', ''),
                    'technician': data.get('technician', ''),
                    'maint_type': data.get('maint_type', 'Routine Inspection'),
                    'notes':      data.get('notes', ''),
                    'next_date':  data.get('next_date', ''),
                    'updated_at': time.time(),
                }
                with _MAINT_LOG_LOCK:
                    _MAINT_LOG[mac] = record
                _save_maint_log()
                # Reset baseline so the satellite re-calibrates on known-good
                # post-service data rather than pre-service degraded data.
                with _sat_lock:
                    sat = _satellites.get(mac)
                    if sat:
                        sat.calibrated = False
                        sat._cal_buf   = []
                        sat.bl_mean    = None
                        sat.bl_std     = None
                        sat.fault_type = "Normal"
                print(f'[maint] Record updated: {mac}  by {record["technician"]}')
                self._send(200, 'application/json', '{"ok":true}')
            except Exception as e:
                self._send(400, 'application/json', json.dumps({'error': str(e)}))
        else:
            self._send(404, 'text/plain', 'Not found')


def start_dashboard(port=8080):
    srv = HTTPServer(('0.0.0.0', port), _DashHandler)
    threading.Thread(target=srv.serve_forever, daemon=True, name='dashboard').start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        lan_ip = 'localhost'
    auth_note = f'  auth: {_AUTH_USER or "admin"} / [password]' if _AUTH_PASS else '  (no auth — set --auth user:pass)'
    notify_note = f'  notifications: webhook active' if _NOTIFY_WEBHOOK else (
                  f'  notifications: email active' if _NOTIFY_EMAIL_CFG else
                  f'  notifications: OFF (use --notify-webhook or --notify-email)')
    print(f"[dashboard] http://localhost:{port}/  ← this machine")
    print(f"[dashboard] http://{lan_ip}:{port}/  ← phone / LAN")
    print(f"[dashboard]{auth_note}")
    print(f"[dashboard]{notify_note}")
    print(f"[dashboard] Firewall (elevated PowerShell, run once):")
    print(f"            New-NetFirewallRule -DisplayName EPM-Dash -Direction Inbound "
          f"-Protocol TCP -LocalPort {port} -Action Allow")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    global CREST_WARN, CREST_FAULT, _NOTIFY_WEBHOOK, _NOTIFY_EMAIL_CFG
    global _AUTH_USER, _AUTH_PASS, _FACTORY_NAME
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
    args = parser.parse_args()

    if args.crest_warn is not None:
        CREST_WARN = args.crest_warn
    if args.crest_fault is not None:
        CREST_FAULT = args.crest_fault

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

    # ── Load maintenance log from disk ────────────────────────────────────────
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    _load_maint_log(os.path.join(log_dir, 'maintenance_log.json'))

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
    print("Firewall rule for TCP receiver (elevated PowerShell, run once):")
    print(f"  New-NetFirewallRule -DisplayName EPM-{args.port} -Direction Inbound "
          f"-Protocol TCP -LocalPort {args.port} -Action Allow")

    start_dashboard(args.dashboard_port)

    threading.Thread(
        target=accept_loop,
        args=(args.listen_ip, args.port, args.fft_mic_n, args.fft_imu_n),
        daemon=True,
    ).start()

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
