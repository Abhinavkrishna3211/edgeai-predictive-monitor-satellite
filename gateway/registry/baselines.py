"""gateway/registry/baselines.py — adaptive-baseline and Kalman RUL state
persistence, and z-score baseline calibration (Phase 8b1 task 2, extracted
from recv_verify.py).

Every function here takes `base_dir`/`storage` as explicit parameters
instead of resolving them from `__file__` or a module global. This file no
longer lives next to recv_verify.py's `model/`/`logs/` directories, so
letting the caller supply its own base_dir keeps the on-disk JSON fallback
path unchanged (still mic_tools/model/baselines_*.json) without this module
needing to know where its caller lives.
"""
import json
import os
import time

import numpy as np


def _baselines_path(base_dir, sat) -> str:
    model_dir = os.path.join(base_dir, 'model')
    os.makedirs(model_dir, exist_ok=True)
    mac_clean = sat.mac_hex.replace(':', '')
    return os.path.join(model_dir, f'baselines_{mac_clean}.json')


def _save_baselines(base_dir, storage, sat) -> None:
    """Persist all four adaptive baselines — SQLite primary, JSON file fallback."""
    if sat.ab_kurtosis is None:
        return
    state = {
        'kurtosis': sat.ab_kurtosis.state_dict(),
        'crest':    sat.ab_crest.state_dict(),
        'rms':      sat.ab_rms.state_dict(),
        'hb':       sat.ab_hb.state_dict(),
        'saved_at': time.time(),
    }
    if storage is not None:
        try:
            storage.save_model_state(sat.name, 'baselines', state)
            return
        except Exception as e:
            print(f'[baseline] [{sat.name}] DB save failed: {e} — falling back to JSON')
    # Fallback: write to model/baselines_{mac}.json
    try:
        with open(_baselines_path(base_dir, sat), 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f'[baseline] [{sat.name}] Save failed: {e}')


def _load_baselines(base_dir, storage, sat) -> None:
    """Restore adaptive baselines — SQLite primary, JSON file migration fallback."""
    if sat.ab_kurtosis is None:
        return
    state = None
    if storage is not None:
        try:
            state = storage.load_model_state(sat.name, 'baselines')
        except Exception as e:
            print(f'[baseline] [{sat.name}] DB load failed: {e}')
    # Migration fallback: read legacy JSON file if DB has no entry yet
    if state is None:
        path = _baselines_path(base_dir, sat)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    state = json.load(f)
            except Exception:
                pass
    if state is None:
        return
    try:
        sat.ab_kurtosis.load_state_dict(state['kurtosis'])
        sat.ab_crest.load_state_dict(state['crest'])
        sat.ab_rms.load_state_dict(state['rms'])
        sat.ab_hb.load_state_dict(state['hb'])
        print(f'[baseline] [{sat.name}] Resumed adaptive baseline '
              f'(n={sat.ab_kurtosis.n_updates} OK frames, '
              f'kurt_mean={sat.ab_kurtosis.mean:.3f}, '
              f'kurt_std={sat.ab_kurtosis.std:.3f})')
    except Exception as e:
        print(f'[baseline] [{sat.name}] Could not load state: {e} — starting fresh')


def _save_rul_state(storage, sat) -> None:
    """Persist RUL Kalman filter state to SQLite (called every 500 frames)."""
    if storage is None or sat.rul_estimator is None:
        return
    try:
        storage.save_model_state(sat.name, 'rul', sat.rul_estimator.state_dict())
    except Exception as e:
        print(f'[rul] [{sat.name}] DB save failed: {e}')


def _load_rul_state(storage, sat) -> None:
    """Restore RUL Kalman filter state from SQLite if available."""
    if storage is None or sat.rul_estimator is None:
        return
    try:
        state = storage.load_model_state(sat.name, 'rul')
        if state is not None:
            sat.rul_estimator.load_state_dict(state)
            print(f'[rul] [{sat.name}] Resumed Kalman RUL '
                  f'(n={sat.rul_estimator.n_updates} frames, '
                  f'lam={sat.rul_estimator.x[1]:.6f})')
    except Exception as e:
        print(f'[rul] [{sat.name}] Could not load RUL state: {e} — starting fresh')


def _sat_update_baseline(sat, mic_rms, mic_kurtosis, k_warn, cal_frames) -> None:
    if sat.calibrated:
        return
    sat._cal_buf.append([mic_rms, mic_kurtosis])
    if len(sat._cal_buf) >= cal_frames:
        arr = np.array(sat._cal_buf, dtype=np.float32)
        sat.bl_mean = arr.mean(axis=0)
        sat.bl_std  = arr.std(axis=0) + 1e-6
        # WP-01 fix: detect pre-damaged machines during warm-up.
        # If the median calibration kurtosis already exceeds k_warn, the baseline
        # is being learned on a damaged bearing — flag it so operators are warned
        # and the adaptive path defers until a healthy period is observed.
        median_kurt = float(np.median(arr[:, 1]))
        if median_kurt >= k_warn:
            print(f"  [{sat.name}] WARNING: calibration kurtosis={median_kurt:.2f} "
                  f">= K_WARN={k_warn} — machine may be pre-damaged. "
                  "Adaptive thresholds deferred; using absolute thresholds only.")
            sat.pre_damaged = True
        sat.calibrated = True
        print(f"  [{sat.name}] Baseline ready: "
              f"rms_mean={sat.bl_mean[0]:.5f}  "
              f"kurt_mean={sat.bl_mean[1]:.2f}  "
              f"kurt_std={sat.bl_std[1]:.2f}")


def _recompute_z_baseline(sat, feat_buf) -> None:
    """Recompute z-score calibration baseline from recent OK-frame feature vectors.

    HST feature layout (from _extract_hst_features):
      feat[0] = mic_kurtosis,  feat[2] = mic_rms

    Baseline uses [mic_rms, mic_kurtosis] order to match _sat_update_baseline().
    Silently skips if the buffer is too small.
    """
    if len(feat_buf) < 10:
        return
    arr = np.array([[f[2], f[0]] for f in feat_buf], dtype=np.float32)
    sat.bl_mean = arr.mean(axis=0)
    sat.bl_std  = arr.std(axis=0) + 1e-6
    # Reset adaptive baselines so they re-learn from the post-drift regime
    for ab in (sat.ab_kurtosis, sat.ab_crest, sat.ab_rms, sat.ab_hb):
        if ab is not None:
            ab.reset()
