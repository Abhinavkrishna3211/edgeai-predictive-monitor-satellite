"""gateway/registry/satellite_state.py — per-satellite connection/alert state
and the satellite registry (Phase 8b1 task 2, extracted from recv_verify.py).

SatelliteState.__init__() and _sat_register() both do a lazy `import
recv_verify` (inside the function body, not at module level) rather than
importing it at the top of this file. recv_verify.py imports _sat_register /
_satellites / _sat_lock / SatelliteState *from this module* at its own
module-load time, so a top-level `import recv_verify` here would try to
import a module that is still mid-initialization. By the time any
SatelliteState is actually constructed (only ever from _sat_register(),
called from satellite_thread/MqttIngestor after recv_verify.py has finished
loading), the module is complete and the lazy import resolves normally.
"""
import collections
import threading
import time

from gateway.registry.baselines import _load_baselines, _load_rul_state

# Optional: per-machine adaptive baselines (pure Python — always available if file is present)
_AB_AVAILABLE = False
try:
    from gateway.pipeline.adaptive_baseline import AdaptiveBaseline
    _AB_AVAILABLE = True
except ImportError:
    AdaptiveBaseline = None  # type: ignore[assignment,misc]

# Optional: Kalman exponential RUL estimator (pure numpy — always available)
_RUL_AVAILABLE = False
try:
    from gateway.pipeline.rul_estimator import ExponentialRUL
    _RUL_AVAILABLE = True
except ImportError:
    ExponentialRUL = None  # type: ignore[assignment,misc]


class SatelliteState:
    def __init__(self, mac_hex, name, fw_major, fw_minor, addr):
        import recv_verify as _rv  # lazy: see module docstring

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
        self.alert       = _rv.EPM_ALERT_OK
        # Z-score adaptive baseline
        self._cal_buf    = []
        self.calibrated  = False
        self.bl_mean     = None
        self.bl_std      = None
        # Alert persistence / hysteresis
        self.warn_streak  = 0   # consecutive frames above threshold
        self.ok_streak    = 0   # consecutive frames below threshold
        self.sent_alert   = _rv.EPM_ALERT_OK  # last byte actually sent to satellite
        # Rolling FPS (last 10 frame timestamps)
        self._ts_buf     = collections.deque(maxlen=10)
        # Dashboard / maintenance tracking (cumulative — NOT reset on reconnect)
        self.warn_frames  = 0
        self.fault_frames = 0
        self.last_fault_t = None          # epoch of most recent FAULT frame
        self.last_z       = 0.0
        self.last_hb      = 0.0           # most recent high-band energy ratio
        self.fault_type   = "Normal"   # spectral fault classification label
        self.history_alerts   = collections.deque([0]   * _rv.HISTORY_LEN, maxlen=_rv.HISTORY_LEN)
        self.history_kurtosis = collections.deque([3.0] * _rv.HISTORY_LEN, maxlen=_rv.HISTORY_LEN)
        self.history_crest    = collections.deque([3.0] * _rv.HISTORY_LEN, maxlen=_rv.HISTORY_LEN)
        # Per-satellite ML auto-training state
        self.ml_buf        = []    # feature dicts (OK frames only) for auto-training
        self.ml_trained    = False
        self.ml_training   = False
        self.ml_trained_at = None  # ISO string or float epoch after training
        self.ml_backend    = 'none'  # 'Qualcomm Adreno 702 GPU', 'CPU (TFLite)', 'IsolationForest', 'none'
        # Online HST detector — river HalfSpaceTrees, fully on-device
        self.hst_detector  = None   # OnlineDetector, initialised in _try_load_hst_state
        self.hst_score     = 0.0    # last HST score for dashboard display
        # Concept-drift tracking — ADWIN updates only on OK-frame scores
        self.hst_feat_buf  = collections.deque(maxlen=500)  # recent OK-frame features
        self.drift_count   = 0      # number of baseline refreshes this session
        self.last_drift_t  = None   # epoch of most recent baseline refresh
        # Bayesian posterior fusion
        self.p_fault       = 0.0    # P(fault | all channels), updated every frame
        # Kalman exponential RUL estimator — one instance per satellite
        self.rul_estimator = None   # ExponentialRUL, created at registration
        self.rul_result    = None   # RULResult from last update (None until n_updates>=30)
        # Adaptive-sensing parameters currently commanded to this satellite (v2 protocol)
        self.adapt_overlap = 0     # last commanded fft_overlap_pct
        self.adapt_avg_n   = 4     # last commanded spec_avg_n
        # Per-feature adaptive baselines — one per scalar feature, OK-frames only
        if _AB_AVAILABLE:
            self.ab_kurtosis = AdaptiveBaseline(alpha=5e-05)  # Phase 4 sweep: alpha=5e-05 optimal
            self.ab_crest    = AdaptiveBaseline(alpha=5e-05)
            self.ab_rms      = AdaptiveBaseline(alpha=5e-05)
            self.ab_hb       = AdaptiveBaseline(alpha=5e-05)
        else:
            self.ab_kurtosis = self.ab_crest = self.ab_rms = self.ab_hb = None
        # Latest per-feature z-scores for feature attribution (updated every frame)
        self.feat_z: dict = {}
        # WP-01: set to True if calibration kurtosis suggests pre-damaged bearing
        self.pre_damaged: bool = False

    def fps_str(self):
        return f"{self.fps:.1f}" if self.connected else "—"

    def fw_str(self):
        # fw_major/fw_minor are None for satellites registered over MQTT,
        # which has no hello-equivalent to report a real version (ADR-027) --
        # rendering that as "0.0" would misread as a real, very old build.
        if self.fw_major is None or self.fw_minor is None:
            return "mqtt"
        return f"{self.fw_major}.{self.fw_minor}"

    def rolling_fps(self, now):
        self._ts_buf.append(now)
        if len(self._ts_buf) < 2:
            return 0.0
        return (len(self._ts_buf) - 1) / max(self._ts_buf[-1] - self._ts_buf[0], 1e-3)


_sat_lock   = threading.Lock()
_satellites = {}   # mac_hex → SatelliteState


def _sat_register(mac_hex, name, fw_major, fw_minor, addr):
    import recv_verify as _rv  # lazy: see module docstring

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
    _rv._try_load_sat_model(sat)
    _rv._try_load_hst_state(sat)
    _load_baselines(_rv._BASE_DIR, _rv._storage, sat)
    if _RUL_AVAILABLE and sat.rul_estimator is None:
        sat.rul_estimator = ExponentialRUL(k_fail=_rv.K_FAIL)
    _load_rul_state(_rv._storage, sat)
    # Register in DB and load cached maintenance record for this MAC
    if _rv._storage is not None:
        try:
            _rv._storage.upsert_satellite(sat.name, mac_hex)
            maint = _rv._storage.get_latest_maintenance(mac_hex)
            if maint:
                with _rv._MAINT_LOG_LOCK:
                    _rv._MAINT_LOG[mac_hex] = maint
        except Exception as e:
            print(f'[registry] [{sat.name}] DB register/maint-load failed: {e}')
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
    # VERIFY-FIX: use ASCII dash to avoid cp1252 UnicodeEncodeError on Windows.
    print(f"  {'-'*12} {'-'*17} {'-'*6} {'-'*6} {'-'*14}")
    for s in sats:
        status    = "CONNECTED" if s.connected else "disconnected"
        alert_str = ["OK", "WARN", "FAULT"][min(s.alert, 2)] if s.connected else "—"
        print(f"  {s.name:<12} {s.mac_hex:<17} "
              f"{s.fw_str():<6} {s.fps_str():<6} {status}  {alert_str}")
