"""gateway/pipeline/ml_scoring.py — ML model loading, scoring, per-satellite
auto-training, and HST detector state persistence, extracted from
recv_verify.py (Phase 8b2 task 3).

Every function does a lazy `import recv_verify as _rv` inside its own body,
for the same reason given in gateway/api/notifications.py's module
docstring: recv_verify.py owns this state and recv_verify.py imports this
module at its own module-load time, so a top-level `import recv_verify`
here would try to import a module that is still mid-initialization; a
top-level `from recv_verify import NAME` would also snapshot values before
main() applies CLI overrides.

_load_ml_model() sets `_rv._ML_MODEL = {...}` (attribute assignment) rather
than declaring its own `global _ML_MODEL`, so recv_verify.py's own read of
`_ML_MODEL` (and this module's own `_ml_score()`) keep seeing the live
object — same rationale as notifications.py's `_load_maint_log`.

`os.path.dirname(__file__)` in the moved functions pointed at mic_tools/
(where recv_verify.py lives); now that this code lives in gateway/pipeline/,
every such lookup is replaced with `_rv._BASE_DIR`, which recv_verify.py
sets to its own directory precisely so moved modules can still resolve
mic_tools/model/ and mic_tools/logs/ correctly (same fix already applied to
dashboard.py's `/api/export` route in Phase 8b2 task 1).

`_HST_AVAILABLE`/`OnlineDetector` get their own independent optional import
here (mirroring recv_verify.py's own try/except ImportError pattern) rather
than being read via `_rv.`: `main()` never reassigns them after import-time,
so there is no CLI-mutation to track, and duplicating the availability
check costs nothing since both modules derive it from the same underlying
`river` package check — same reasoning gateway/pipeline/alerting.py already
applies to `AdaptiveBaseline`/`_AB_AVAILABLE`.

HST-lifecycle split: _try_load_hst_state/_save_hst_state live here, in
ml_scoring.py, rather than in gateway/registry/baselines.py alongside the
similarly-named _load_baselines/_save_baselines/_load_rul_state. Those
registry functions restore saved state into fields of an *existing*
SatelliteState; _try_load_hst_state instead constructs a brand-new
OnlineDetector (with n_features/n_trees hyperparameters), and on a corrupt
or unreadable pickle it discards and recreates that detector rather than
erroring — closer in shape to model loading than to plain state restore, so
it belongs with the rest of this file's model lifecycle management instead.
"""

import datetime
import json
import os
import threading

import numpy as np

from gateway.registry.satellite_state import _sat_lock

try:
    from gateway.pipeline.online_detector import OnlineDetector
    _HST_AVAILABLE = True
except ImportError:
    OnlineDetector = None  # type: ignore[assignment,misc]
    _HST_AVAILABLE = False


_TRAIN_FEATS = ['mic_rms', 'mic_crest', 'mic_kurtosis',
                'imu_rms', 'imu_crest', 'high_band_ratio', 'z_score']


def _load_ml_model(model_prefix: str):
    """
    Load the IsolationForest model produced by ml_trainer.py.
    Sets recv_verify._ML_MODEL so compute_alert() can use it.
    Silently skips if joblib/scikit-learn is not installed.
    """
    import recv_verify as _rv
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
        _rv._ML_MODEL = {
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
        print(f'[ml] Thresholds -- WARN <= {_rv._ML_MODEL["t_warn"]:.4f}   '
              f'FAULT <= {_rv._ML_MODEL["t_fault"]:.4f}')
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
    import recv_verify as _rv
    if _rv._ML_MODEL is None:
        return None
    return _ml_score_with(frame, _rv._ML_MODEL)


def _ml_score_with(frame: dict, model: dict) -> int | None:
    """Run ML inference using an explicit model dict (per-satellite or global).

    Routes to TFLite neural autoencoder (NPU) when available, otherwise falls
    back to scikit-learn IsolationForest (CPU).
    """
    import math
    import recv_verify as _rv
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
            return _rv.EPM_ALERT_FAULT
        if score <= model['t_warn']:
            return _rv.EPM_ALERT_WARN
        return _rv.EPM_ALERT_OK
    except Exception as e:
        print(f'[ml] IsolationForest scoring failed: {e}')
        return None


def _ml_score_tflite(frame: dict, model: dict) -> int | None:
    """Score one frame using the TFLite neural autoencoder (NPU path)."""
    import math
    import recv_verify as _rv
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
        feat = np.array(stats + [0.0] * 32, dtype=np.float32)
    try:
        mse = model['inferencer'].infer(feat)
        if mse >= model['t_fault']:
            return _rv.EPM_ALERT_FAULT
        if mse >= model['t_warn']:
            return _rv.EPM_ALERT_WARN
        return _rv.EPM_ALERT_OK
    except Exception as e:
        print(f'[ml] TFLite scoring failed: {e}')
        return None


def _try_load_sat_model(sat):
    """Load a per-satellite model from disk into _sat_models (silent if missing).

    Priority:
      1. TFLite neural autoencoder (NPU-ready) — <name>_autoencoder.tflite
      2. IsolationForest joblib bundle          — <name>_iso.joblib  (legacy)
    """
    import recv_verify as _rv
    model_dir = os.path.join(_rv._BASE_DIR, 'model')
    mac_slug  = sat.mac_hex.replace(':', '')

    # ── 1. Try TFLite autoencoder (NPU path) ─────────────────────────────────
    try:
        from gateway.pipeline.autoencoder import load_npu_model
        for stem in (sat.name, mac_slug):
            model_dict = load_npu_model(os.path.join(model_dir, stem))
            if model_dict is None:
                continue
            with _rv._sat_models_lock:
                _rv._sat_models[sat.mac_hex] = model_dict
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
            with _rv._sat_models_lock:
                _rv._sat_models[sat.mac_hex] = {
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


def _try_load_hst_state(sat):
    """Load HST detector state from pickle; create a fresh detector if absent."""
    import recv_verify as _rv
    if not _HST_AVAILABLE:
        return
    log_dir  = os.path.join(_rv._BASE_DIR, 'logs')
    pkl_path = os.path.join(log_dir, f'hst_state_{sat.name}.pkl')
    det = OnlineDetector(n_features=_rv.FEATURE_DIM, n_trees=10)  # Phase 2 sweep: n_trees=10 optimal
    if os.path.exists(pkl_path):
        try:
            det.load(pkl_path)
            print(f'[hst] [{sat.name}] Resumed detector from {pkl_path}  '
                  f'(n={det._n} frames learned)')
        except Exception as e:
            print(f'[hst] [{sat.name}] Could not resume {pkl_path}: {e} — starting fresh')
            det = OnlineDetector(n_features=_rv.FEATURE_DIM, n_trees=10)
    sat.hst_detector = det


def _save_hst_state(sat):
    """Persist HST detector state to disk (called every 500 frames)."""
    import recv_verify as _rv
    if sat.hst_detector is None:
        return
    log_dir  = os.path.join(_rv._BASE_DIR, 'logs')
    pkl_path = os.path.join(log_dir, f'hst_state_{sat.name}.pkl')
    try:
        sat.hst_detector.save(pkl_path)
    except Exception as e:
        print(f'[hst] [{sat.name}] State save failed: {e}')


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
    import recv_verify as _rv
    model_dir  = os.path.join(_rv._BASE_DIR, 'model')
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
            with _rv._sat_models_lock:
                _rv._sat_models[sat.mac_hex] = model_dict
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

        with _rv._sat_models_lock:
            _rv._sat_models[sat.mac_hex] = {
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
