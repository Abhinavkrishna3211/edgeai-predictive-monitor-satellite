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
import sys
import threading
import time
import urllib.parse
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
HISTORY_LEN    = 60
WATERFALL_ROWS = 80   # time rows in the mic FFT waterfall (~36 s at 2.2 fps)

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


def _high_band_ratio(mic_fft_db):
    """Fraction of mic FFT power in the 2-8 kHz band (bearing resonance zone).
    mic_fft_db: 512-element dBFS array, bins 0..8kHz at 15.625 Hz/bin."""
    power   = 10.0 ** (mic_fft_db / 10.0)
    n       = len(power)
    hz_per  = MIC_FS_HZ / 2.0 / n          # 15.625 Hz/bin
    lo_bin  = max(1, int(2000 / hz_per))    # 2 kHz
    total   = power[1:].sum() + 1e-10       # skip DC
    high    = power[lo_bin:].sum()
    return float(high / total)


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


def compute_alert(sat, frame, warn_streak, ok_streak, sent_alert):
    """Compute per-frame alert level and z-score.

    Streak counters are passed in and returned so this function has no
    side-effects on sat.  All sat mutations happen in satellite_thread under
    _sat_lock, eliminating data races with the dashboard HTTP reader thread.

    Returns (alert_byte, z_score, high_band_ratio,
             new_warn_streak, new_ok_streak, new_sent_alert).
    """
    mic_kurtosis = frame['mic_kurtosis']
    mic_crest    = frame['mic_crest']
    imu_crest    = frame['imu_crest']
    mic_rms      = frame['mic_rms']
    mic_fft      = frame['mic_fft']

    _sat_update_baseline(sat, mic_rms, mic_kurtosis)

    # ── Z-score (active after calibration) ───────────────────────────────────
    z_score = 0.0
    if sat.calibrated:
        features = np.array([mic_rms, mic_kurtosis], dtype=np.float32)
        z_scores = np.abs(features - sat.bl_mean) / sat.bl_std
        z_score  = float(z_scores.max())

    # ── High-band energy ratio (computed once, reused for filter + logging) ──
    hb = _high_band_ratio(mic_fft)

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

    return final, z_score, hb, warn_streak, ok_streak, final


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

            alert, z_score, hb, warn_streak, ok_streak, sent_alert = \
                compute_alert(sat, frame, warn_streak, ok_streak, sent_alert)

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

            with _sat_lock:
                sat.frame_count  += 1
                sat.fps           = fps
                sat.last_t        = now
                sat.last_frame    = frame
                sat.alert         = alert
                sat.warn_streak   = warn_streak
                sat.ok_streak     = ok_streak
                sat.sent_alert    = sent_alert
                # Dashboard history
                sat.last_z = z_score
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

    crest_mic  = collections.deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
    crest_imu  = collections.deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
    kurt_mic   = collections.deque([3.0] * HISTORY_LEN, maxlen=HISTORY_LEN)

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
    lc_mic,  = ax_cr.plot(xc, list(crest_mic), lw=1.0, color='cyan',    label='MIC crest')
    lc_imu,  = ax_cr.plot(xc, list(crest_imu), lw=1.0, color='#ff7f0e', label='IMU crest')
    lc_kurt, = ax_cr.plot(xc, list(kurt_mic),  lw=1.2, color='#aa44ff', label='MIC kurtosis/3')
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
    last_sat = None

    while plt.fignum_exists(fig.number):
        _display.wait(timeout=0.3)
        frame, satname = _display.get()
        if frame is None or frame['frame_id'] == last_id:
            plt.pause(0.05)
            continue
        last_id = frame['frame_id']

        if satname != last_sat:
            crest_mic.clear(); crest_mic.extend([0.0] * HISTORY_LEN)
            crest_imu.clear(); crest_imu.extend([0.0] * HISTORY_LEN)
            kurt_mic.clear();  kurt_mic.extend([3.0] * HISTORY_LEN)
            wf_buf[:] = -120.0
            last_sat = satname

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
        hb = _high_band_ratio(frame['mic_fft'])
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
<title>EPM · Predictive Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2128;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--ok:#3fb950;--warn:#d29922;--fault:#f85149;--blue:#58a6ff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-height:100vh}
header{display:flex;align-items:center;justify-content:space-between;padding:12px 22px;background:var(--card);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:99;gap:12px}
.logo{font-size:1.1rem;font-weight:700}
.logo em{color:var(--blue);font-style:normal}
.logo small{display:block;font-weight:400;color:var(--muted);font-size:.7rem;margin-top:2px}
.hdr-r{display:flex;gap:18px;align-items:center;font-size:.78rem;color:var(--muted);flex-wrap:wrap}
.hdr-r strong{color:var(--text)}
.live{display:inline-flex;align-items:center;gap:5px}
.ldot{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:bok 2s infinite}
@keyframes bok  {0%,100%{opacity:1}50%{opacity:.2}}
@keyframes bwarn{0%,100%{opacity:1}30%,70%{opacity:.1}}
@keyframes bflt {0%,10%,20%,30%,40%,50%,60%,70%,80%,90%,100%{opacity:1}5%,15%,25%,35%,45%,55%,65%,75%,85%,95%{opacity:.05}}
.banner{display:none;padding:9px 22px;text-align:center;font-size:.83rem;font-weight:600}
.banner.fault{display:block;background:rgba(248,81,73,.12);color:var(--fault);border-bottom:2px solid rgba(248,81,73,.4)}
.banner.warn {display:block;background:rgba(210,153,34,.1); color:var(--warn); border-bottom:2px solid rgba(210,153,34,.35)}
.sum{display:flex;flex-wrap:wrap;gap:10px;padding:14px 22px}
.st{flex:1;min-width:110px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:11px 15px}
.stl{font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.stv{font-size:1.7rem;font-weight:700;margin-top:3px;line-height:1}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;padding:0 22px 30px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:box-shadow .15s,transform .15s}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 28px rgba(0,0,0,.4)}
.card.ok   {border-color:rgba(63,185,80,.3)}
.card.warn {border-color:rgba(210,153,34,.6);box-shadow:0 0 0 1px rgba(210,153,34,.15)}
.card.fault{border-color:rgba(248,81,73,.75);box-shadow:0 0 0 2px rgba(248,81,73,.18),0 0 22px rgba(248,81,73,.09)}
.card.offline{opacity:.45}
.ch{display:flex;align-items:flex-start;justify-content:space-between;padding:13px 16px 10px;border-bottom:1px solid var(--border)}
.cn{font-size:1.05rem;font-weight:700}
.cm{font-size:.67rem;color:var(--muted);font-family:monospace;margin-top:2px}
.csr{display:flex;align-items:center;gap:7px}
.sdot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.sdot.ok   {background:var(--ok);  animation:bok 2s infinite}
.sdot.warn {background:var(--warn);animation:bwarn 1s infinite}
.sdot.fault{background:var(--fault);animation:bflt .35s infinite}
.badge{padding:3px 9px;border-radius:10px;font-size:.7rem;font-weight:700;letter-spacing:.03em}
.badge.ok   {background:rgba(63,185,80,.12); color:var(--ok)}
.badge.warn {background:rgba(210,153,34,.12);color:var(--warn)}
.badge.fault{background:rgba(248,81,73,.12); color:var(--fault)}
.mg{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border)}
.met{background:var(--card);padding:8px 12px}
.ml{font-size:.6rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.mv{font-size:1rem;font-weight:600;font-family:monospace;margin-top:2px}
.hr{display:flex;align-items:center;gap:12px;padding:10px 16px}
.hbw{flex:1}
.hbl{font-size:.65rem;color:var(--muted);margin-bottom:4px}
.hb{height:5px;border-radius:3px;background:var(--border);overflow:hidden}
.hf{height:100%;border-radius:3px;transition:width .6s,background .6s}
.hsc{font-size:1.25rem;font-weight:700;min-width:48px;text-align:right}
.mnt{margin:0 15px 10px;padding:9px 12px;border-radius:7px;font-size:.77rem}
.mnt.ok   {background:rgba(63,185,80,.07); border:1px solid rgba(63,185,80,.2)}
.mnt.warn {background:rgba(210,153,34,.07);border:1px solid rgba(210,153,34,.25)}
.mnt.fault{background:rgba(248,81,73,.07); border:1px solid rgba(248,81,73,.35)}
.mntt{font-weight:700;margin-bottom:3px}
.mnts{color:var(--muted);font-size:.69rem;margin-top:3px;line-height:1.45}
.cwrap{padding:2px 14px 11px}
.cf{display:flex;justify-content:space-between;padding:7px 15px;background:var(--card2);border-top:1px solid var(--border);font-size:.67rem;color:var(--muted)}
.nosats{text-align:center;padding:70px 20px;color:var(--muted)}
.nosats h2{font-size:1rem;margin-bottom:8px;color:var(--text)}
footer{text-align:center;padding:12px;color:var(--muted);font-size:.7rem;border-top:1px solid var(--border)}
@media(max-width:480px){.hdr-r span{display:none}.hdr-r .live{display:inline-flex}}
</style>
</head>
<body>

<header>
  <div class="logo">
    <em>⚙ EPM</em> Predictive Monitor
    <small>EdgeAI Industrial Bearing Fault Detection System</small>
  </div>
  <div class="hdr-r">
    <span>Uptime&nbsp;<strong id="up">—</strong></span>
    <span><strong id="sc" style="color:var(--blue)">0</strong>&nbsp;satellites</span>
    <span class="live"><span class="ldot"></span><span id="ts">—</span></span>
  </div>
</header>

<div id="banner" class="banner"></div>

<div class="sum">
  <div class="st"><div class="stl">Connected</div><div class="stv" id="Ss" style="color:var(--blue)">0</div></div>
  <div class="st"><div class="stl">Healthy</div><div class="stv" id="So" style="color:var(--ok)">0</div></div>
  <div class="st"><div class="stl">Warning</div><div class="stv" id="Sw" style="color:var(--warn)">0</div></div>
  <div class="st"><div class="stl">Fault</div><div class="stv" id="Sf" style="color:var(--fault)">0</div></div>
  <div class="st"><div class="stl">Fault Events</div><div class="stv" id="Se" style="color:var(--fault)">0</div></div>
  <div class="st"><div class="stl">Avg Health</div><div class="stv" id="Sh">—</div></div>
</div>

<div id="grid"><div class="nosats"><h2>Waiting for satellites…</h2><p>Start firmware on XIAO ESP32-S3 or run satellite_sim.py</p></div></div>

<footer id="foot">EPM Dashboard · Auto-refreshes every 2 s</footer>

<script>
const CH = {};
let TH = {k_warn:6,k_fault:12,cf_warn:5,cf_fault:10};
let lastKey = '';

const HC  = s => s>=75?'#3fb950':s>=50?'#d29922':'#f85149';
const MC  = d => d===0?'fault':d<=30?'warn':'ok';
const rulColor = d => d===null?'var(--ok)':d>90?'var(--ok)':d>30?'var(--warn)':d>7?'#ff8800':'var(--fault)';
function fmtRul(d){
  if(d===null||d===undefined) return '✓ Stable (no rising trend)';
  if(d<0.04) return '⚠ Fault threshold reached';
  if(d<1)    return '⚠ <1 day remaining';
  if(d<7)    return `⚠ ~${Math.round(d)} day${Math.round(d)!==1?'s':''} remaining`;
  if(d<90)   return `~${Math.round(d)} days remaining`;
  return `~${Math.round(d)} days remaining (stable)`;
}

function fmtUp(s){
  if(s<60) return s+'s';
  if(s<3600) return Math.floor(s/60)+'m '+(s%60)+'s';
  return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}
function fmtDt(ts){ return ts?new Date(ts*1000).toLocaleString():'never'; }
function fmtFuture(days){
  if(!days) return '';
  return new Date(Date.now()+days*864e5).toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'});
}
function kColor(k){ return k>TH.k_fault?'#f85149':k>TH.k_warn?'#d29922':'#3fb950'; }

function cardHTML(s){
  const al=s.alert.toLowerCase(), m=s.metrics||{};
  const hc=HC(s.health_score), mc=MC(s.maintenance_days);
  const fd=fmtFuture(s.maintenance_days);
  const due=fd?` · Due: <strong>${fd}</strong>`:'';
  return `<div class="card ${al}${s.connected?'':' offline'}" id="C_${s.name}">
<div class="ch">
  <div><div class="cn">${s.name}</div><div class="cm">${s.mac} · FW ${s.fw}</div></div>
  <div class="csr"><div class="sdot ${al}"></div><span class="badge ${al}">${s.alert}</span></div>
</div>
<div class="mg">
  <div class="met"><div class="ml">Kurtosis</div><div class="mv" style="color:${kColor(m.mic_kurtosis||0)}" id="K_${s.name}">${(m.mic_kurtosis||0).toFixed(2)}</div></div>
  <div class="met"><div class="ml">Crest Factor</div><div class="mv" id="CF_${s.name}">${(m.mic_crest||0).toFixed(2)}</div></div>
  <div class="met"><div class="ml">High-Band %</div><div class="mv" id="HB_${s.name}">${((m.high_band_ratio||0)*100).toFixed(1)}%</div></div>
  <div class="met"><div class="ml">Mic RMS</div><div class="mv" id="RMS_${s.name}">${(m.mic_rms||0).toFixed(5)}</div></div>
  <div class="met"><div class="ml">Z-Score</div><div class="mv" style="color:${s.z_score>3?'#f85149':s.z_score>1.5?'#d29922':'inherit'}" id="Z_${s.name}">${s.z_score.toFixed(1)}</div></div>
  <div class="met"><div class="ml">Frame Rate</div><div class="mv" id="FPS_${s.name}">${s.fps.toFixed(1)} fps</div></div>
  <div class="met" style="grid-column:span 3"><div class="ml">Est. Remaining Life</div><div class="mv" id="RUL_${s.name}" style="color:${rulColor(s.rul_days)};font-size:.85rem">${fmtRul(s.rul_days)}</div></div>
</div>
<div class="hr">
  <div class="hbw"><div class="hbl">Machine Health Score</div><div class="hb"><div class="hf" id="HF_${s.name}" style="width:${s.health_score}%;background:${hc}"></div></div></div>
  <div class="hsc" id="HS_${s.name}" style="color:${hc}">${s.health_score}%</div>
</div>
<div class="mnt ${mc}" id="MNT_${s.name}">
  <div class="mntt">🔧 ${s.maintenance}${due}</div>
  <div class="mnts">Warn events: ${s.warn_frames} · Fault events: ${s.fault_frames}${s.last_fault_t?' · Last fault: '+fmtDt(s.last_fault_t):''}</div>
</div>
<div class="cwrap"><canvas id="CH_${s.name}" height="70"></canvas></div>
<div class="cf">
  <span>Frames: ${s.frame_count.toLocaleString()}</span>
  <span>${s.calibrated?'✓ Calibrated':'⏳ Calibrating…'}</span>
  <span>Up ${fmtUp(s.uptime_s)}</span>
  <span>${s.connected?'🟢 Online':'🔴 Offline'}</span>
</div>
</div>`;
}

function buildChart(s){
  const el=document.getElementById('CH_'+s.name); if(!el) return;
  const h=s.history||{kurtosis:[],crest:[]};
  const n=h.kurtosis.length;
  const wl=Array(n).fill(TH.k_warn), fl=Array(n).fill(TH.k_fault);
  if(CH[s.name]){
    const c=CH[s.name];
    c.data.datasets[0].data=h.kurtosis;
    c.data.datasets[1].data=h.crest;
    c.data.datasets[2].data=wl;
    c.data.datasets[3].data=fl;
    c.update('none'); return;
  }
  CH[s.name]=new Chart(el,{type:'line',data:{labels:Array.from({length:n},(_,i)=>i),datasets:[
    {label:'Kurtosis', data:h.kurtosis,borderColor:'rgba(88,166,255,.9)',  backgroundColor:'rgba(88,166,255,.08)',borderWidth:1.5,pointRadius:0,tension:.3,fill:true},
    {label:'Crest CF', data:h.crest,   borderColor:'rgba(255,127,14,.85)', backgroundColor:'rgba(255,127,14,.05)',borderWidth:1.5,pointRadius:0,tension:.3},
    {label:'Warn',     data:wl,         borderColor:'rgba(210,153,34,.55)',borderWidth:1,  pointRadius:0,borderDash:[5,4]},
    {label:'Fault',    data:fl,         borderColor:'rgba(248,81,73,.55)', borderWidth:1,  pointRadius:0,borderDash:[5,4]},
  ]},options:{responsive:true,animation:false,plugins:{
    legend:{display:true,position:'top',labels:{color:'#8b949e',font:{size:9},boxWidth:10,padding:7}}
  },scales:{
    x:{display:false},
    y:{min:0,max:Math.max(TH.k_fault+3,16),ticks:{color:'#8b949e',font:{size:8}},grid:{color:'rgba(255,255,255,.05)'},border:{color:'#30363d'}}
  }}});
}

function upCard(s){
  const card=document.getElementById('C_'+s.name); if(!card) return;
  const al=s.alert.toLowerCase(), m=s.metrics||{};
  const hc=HC(s.health_score), mc=MC(s.maintenance_days);
  const fd=fmtFuture(s.maintenance_days), due=fd?` · Due: <strong>${fd}</strong>`:'';
  card.className='card '+al+(s.connected?'':' offline');
  card.querySelector('.sdot').className='sdot '+al;
  const b=card.querySelector('.badge'); b.className='badge '+al; b.textContent=s.alert;
  const g=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};
  const gs=(id,p,v)=>{const e=document.getElementById(id);if(e)e.style[p]=v;};
  g(`K_${s.name}`,(m.mic_kurtosis||0).toFixed(2));    gs(`K_${s.name}`,'color',kColor(m.mic_kurtosis||0));
  g(`CF_${s.name}`,(m.mic_crest||0).toFixed(2));
  g(`HB_${s.name}`,((m.high_band_ratio||0)*100).toFixed(1)+'%');
  g(`RMS_${s.name}`,(m.mic_rms||0).toFixed(5));
  g(`Z_${s.name}`,s.z_score.toFixed(1));              gs(`Z_${s.name}`,'color',s.z_score>3?'#f85149':s.z_score>1.5?'#d29922':'');
  g(`FPS_${s.name}`,s.fps.toFixed(1)+' fps');
  const rul_el=document.getElementById(`RUL_${s.name}`);
  if(rul_el){rul_el.textContent=fmtRul(s.rul_days);rul_el.style.color=rulColor(s.rul_days);}
  const hf=document.getElementById(`HF_${s.name}`); if(hf){hf.style.width=s.health_score+'%';hf.style.background=hc;}
  g(`HS_${s.name}`,s.health_score+'%'); gs(`HS_${s.name}`,'color',hc);
  const mnt=document.getElementById(`MNT_${s.name}`);
  if(mnt){mnt.className='mnt '+mc;mnt.innerHTML=`<div class="mntt">🔧 ${s.maintenance}${due}</div><div class="mnts">Warn events: ${s.warn_frames} · Fault events: ${s.fault_frames}${s.last_fault_t?' · Last fault: '+fmtDt(s.last_fault_t):''}</div>`;}
  const f=card.querySelector('.cf').children;
  if(f[0])f[0].textContent='Frames: '+s.frame_count.toLocaleString();
  if(f[1])f[1].textContent=s.calibrated?'✓ Calibrated':'⏳ Calibrating…';
  if(f[2])f[2].textContent='Up '+fmtUp(s.uptime_s);
  if(f[3])f[3].textContent=s.connected?'🟢 Online':'🔴 Offline';
}

async function refresh(){
  try{
    const r=await fetch('/api/status'); if(!r.ok) return;
    const d=await r.json();
    TH=d.thresholds||TH;

    document.getElementById('up').textContent=fmtUp(d.server_uptime_s);
    document.getElementById('sc').textContent=d.satellite_count;
    document.getElementById('ts').textContent=new Date().toLocaleTimeString();
    document.getElementById('foot').textContent=
      `EPM Gateway · Auto-refreshes every 2 s · Thresholds: K≥${TH.k_warn} WARN / K≥${TH.k_fault} FAULT · CF≥${TH.cf_warn} WARN / CF≥${TH.cf_fault} FAULT`;

    const sats=d.satellites;
    const ok_n=sats.filter(s=>s.alert==='OK').length;
    const wn_n=sats.filter(s=>s.alert==='WARN').length;
    const ft_n=sats.filter(s=>s.alert==='FAULT').length;
    const avg=sats.length?Math.round(sats.reduce((a,s)=>a+s.health_score,0)/sats.length):null;
    document.getElementById('Ss').textContent=d.satellite_count;
    document.getElementById('So').textContent=ok_n;
    document.getElementById('Sw').textContent=wn_n;
    document.getElementById('Sf').textContent=ft_n;
    document.getElementById('Se').textContent=d.total_faults_today;
    const sh=document.getElementById('Sh');
    sh.textContent=avg!==null?avg+'%':'—';
    sh.style.color=avg>=75?'var(--ok)':avg>=50?'var(--warn)':'var(--fault)';

    const bn=document.getElementById('banner');
    if(ft_n>0){bn.className='banner fault';bn.textContent='⚠ FAULT — '+sats.filter(s=>s.alert==='FAULT').map(s=>s.name).join(', ')+' — Immediate inspection required';}
    else if(wn_n>0){bn.className='banner warn';bn.textContent='⚡ WARNING — '+sats.filter(s=>s.alert==='WARN').map(s=>s.name).join(', ')+' — Elevated vibration detected';}
    else bn.className='banner';

    const key=sats.map(s=>s.name).sort().join(',');
    const grid=document.getElementById('grid');
    if(key!==lastKey){
      Object.values(CH).forEach(c=>c.destroy()); for(const k in CH) delete CH[k];
      grid.innerHTML=sats.length?sats.map(cardHTML).join(''):'<div class="nosats"><h2>Waiting for satellites…</h2><p>Start firmware or satellite_sim.py</p></div>';
      lastKey=key;
    } else {
      sats.forEach(upCard);
    }
    sats.forEach(buildChart);
  } catch(e){console.warn(e);}
}

refresh();
setInterval(refresh,2000);
</script>
</body>
</html>"""


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
                'mic_rms':          round(float(s.last_frame.get('mic_rms',  0)), 6),
                'mic_kurtosis':     round(float(s.last_frame.get('mic_kurtosis', 0)), 2),
                'mic_crest':        round(float(s.last_frame.get('mic_crest', 0)), 2),
                'imu_rms':          round(float(s.last_frame.get('imu_rms',  0)), 5),
                'imu_crest':        round(float(s.last_frame.get('imu_crest', 0)), 2),
                'high_band_ratio':  round(_high_band_ratio(s.last_frame.get('mic_fft', [])), 3),
            }
        sat_list.append({
            'name':           s.name,
            'mac':            s.mac_hex,
            'fw':             f"{s.fw_major}.{s.fw_minor}",
            'alert':          ['OK', 'WARN', 'FAULT'][min(int(s.sent_alert), 2)],
            'connected':      s.connected,
            'uptime_s':       int(now - s.connect_t),
            'frame_count':    s.frame_count,
            'fps':            round(s.fps, 1),
            'calibrated':     s.calibrated,
            'health_score':   health,
            'maintenance':    maint,
            'maintenance_days': maint_days,
            'rul_days':       rul_days,
            'warn_frames':    s.warn_frames,
            'fault_frames':   s.fault_frames,
            'last_fault_t':   s.last_fault_t,
            'z_score':        round(s.last_z, 2),
            'metrics':        m,
            'history': {
                'alerts':    list(s.history_alerts),
                'kurtosis':  [round(v, 2) for v in s.history_kurtosis],
                'crest':     [round(v, 2) for v in s.history_crest],
            },
        })

    return json.dumps({
        'server_uptime_s':    int(now - _SERVER_START_T),
        'timestamp':          now,
        'satellite_count':    sum(1 for s in sat_list if s['connected']),
        'total_faults_today': sum(s['fault_frames'] for s in sat_list),
        'thresholds': {
            'k_warn':  K_WARN,  'k_fault':  K_FAULT,
            'cf_warn': CREST_WARN, 'cf_fault': CREST_FAULT,
        },
        'satellites': sat_list,
    })


class _DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass   # suppress per-request log noise

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._send(200, 'text/html; charset=utf-8', _DASHBOARD_HTML)
        elif path == '/api/status':
            self._send(200, 'application/json', _build_status_json())
        else:
            self._send(404, 'text/plain', 'Not found')


def start_dashboard(port=8080):
    srv = HTTPServer(('0.0.0.0', port), _DashHandler)
    threading.Thread(target=srv.serve_forever, daemon=True, name='dashboard').start()
    # Determine a human-friendly LAN address to show the user
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError:
        lan_ip = 'localhost'
    print(f"[dashboard] http://localhost:{port}/  ← open on this machine")
    print(f"[dashboard] http://{lan_ip}:{port}/  ← open on phone / any LAN device")
    print(f"[dashboard] Firewall rule (run once in elevated PowerShell):")
    print(f"            New-NetFirewallRule -DisplayName EPM-Dash -Direction Inbound "
          f"-Protocol TCP -LocalPort {port} -Action Allow")


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    global CREST_WARN, CREST_FAULT
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
    args = parser.parse_args()

    if args.crest_warn is not None:
        CREST_WARN = args.crest_warn
    if args.crest_fault is not None:
        CREST_FAULT = args.crest_fault

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

    print("EPM gateway — multi-satellite predictive maintenance receiver")
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
