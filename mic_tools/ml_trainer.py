#!/usr/bin/env python3
"""
ml_trainer.py — Train a bearing anomaly detection model from EPM gateway logs.

Trains an IsolationForest on CSV data collected by recv_verify.py so that
the gateway can use a learned model (via --model) instead of fixed thresholds.
The model captures the multi-dimensional signature of healthy operation and
scores each new frame by its distance from that learned distribution.

Workflow:
  1. Run recv_verify.py to collect CSV logs in mic_tools/logs/
  2. Train:   python ml_trainer.py
  3. Infer:   python recv_verify.py --model model/epm_model
              python ml_infer.py                       # offline analysis

Usage:
  python ml_trainer.py                            # all satellites, logs/
  python ml_trainer.py --satellite SAT-A3B4       # one satellite only
  python ml_trainer.py --contamination 0.05       # expected fault fraction
  python ml_trainer.py --output model/my_model    # custom output prefix
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np

try:
    import pandas as pd
except ImportError:
    sys.exit('pandas not installed.  Run: pip install pandas scikit-learn joblib')

try:
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
    import joblib
except ImportError:
    sys.exit('scikit-learn / joblib not installed.  Run: pip install scikit-learn joblib')

LOG_DIR   = os.path.join(os.path.dirname(__file__), 'logs')
MODEL_DIR = os.path.join(os.path.dirname(__file__), 'model')

# Feature columns produced by recv_verify.py CSV writer
BASE_FEATURES = ['mic_rms', 'mic_crest', 'mic_kurtosis',
                 'imu_rms', 'imu_crest', 'high_band_ratio', 'z_score']


def _load_csvs(satellite: str | None, log_dir: str) -> 'pd.DataFrame':
    pattern = f'epm_{satellite}_*.csv' if satellite else 'epm_*.csv'
    files   = sorted(glob.glob(os.path.join(log_dir, pattern)))
    if not files:
        sys.exit(
            f'No CSV files matching "{pattern}" in {log_dir}.\n'
            f'Run recv_verify.py first to collect training data.')
    print(f'[trainer] Loading {len(files)} CSV file(s)…')
    dfs = []
    for f in files:
        try:
            dfs.append(pd.read_csv(f))
        except Exception as e:
            print(f'  WARNING: skipping {os.path.basename(f)}: {e}')
    df = pd.concat(dfs, ignore_index=True)
    print(f'  Rows: {len(df):,}')
    return df


def _build_feature_matrix(df: 'pd.DataFrame') -> tuple:
    """
    Select and engineer features from the raw CSV.
    Returns (feature_matrix, feature_column_names).
    """
    avail   = [c for c in BASE_FEATURES if c in df.columns]
    missing = [c for c in BASE_FEATURES if c not in df.columns]
    if missing:
        print(f'  WARNING: missing columns {missing} — proceeding with {avail}')
    if not avail:
        sys.exit('No usable feature columns found in CSV.  '
                 f'Expected: {BASE_FEATURES}')

    feat = df[avail].copy()
    feat = feat.replace([float('inf'), float('-inf')], float('nan')).dropna()

    # Log-scale high-dynamic-range features to keep the scaler well-conditioned
    if 'mic_kurtosis' in feat.columns:
        feat['log_kurtosis'] = np.log1p(feat['mic_kurtosis'].clip(lower=0))
    if 'z_score' in feat.columns:
        feat['log_z'] = np.log1p(feat['z_score'].clip(lower=0))

    return feat.values, list(feat.columns)


def _train_isolation_forest(X: np.ndarray, contamination: float,
                             n_estimators: int) -> tuple:
    print(f'\n[trainer] Training IsolationForest  '
          f'n_samples={len(X):,}  contamination={contamination:.0%}  '
          f'n_estimators={n_estimators}')

    scaler = StandardScaler()
    X_s    = scaler.fit_transform(X)

    iso = IsolationForest(
        n_estimators = n_estimators,
        contamination= contamination,
        max_samples  = min(512, len(X)),
        random_state = 42,
        n_jobs       = -1,
    )
    iso.fit(X_s)

    scores = iso.decision_function(X_s)

    # Derive percentile-based decision thresholds from training scores.
    # Bottom contamination% → WARN; bottom contamination/3 % → FAULT.
    # Lower decision score = more anomalous.
    t_warn  = float(np.percentile(scores, contamination * 100))
    t_fault = float(np.percentile(scores, contamination / 3 * 100))

    n_anomaly = int(np.sum(iso.predict(X_s) == -1))
    print(f'  Training anomalies flagged: {n_anomaly} ({n_anomaly / len(X):.1%})')
    print(f'  Decision thresholds — WARN ≤ {t_warn:.4f}   FAULT ≤ {t_fault:.4f}')

    return scaler, iso, t_warn, t_fault


def _discover_satellites(log_dir: str) -> list[str]:
    """Return unique satellite names found in CSV filenames (epm_<name>_*.csv)."""
    files = glob.glob(os.path.join(log_dir, 'epm_*.csv'))
    names = set()
    for f in files:
        base = os.path.basename(f)
        # filename format: epm_<name>_YYYYMMDD.csv
        parts = base[4:].rsplit('_', 1)
        if len(parts) == 2 and parts[1].replace('.csv', '').isdigit():
            names.add(parts[0])
    return sorted(names)


def _train_and_save(satellite: str | None, log_dir: str, model_dir: str,
                    output_prefix: str | None,
                    contamination: float, n_estimators: int) -> None:
    df           = _load_csvs(satellite, log_dir)
    X, feat_cols = _build_feature_matrix(df)
    scaler, iso, tw, tf = _train_isolation_forest(X, contamination, n_estimators)

    if output_prefix:
        prefix = output_prefix
    elif satellite:
        prefix = os.path.join(model_dir, satellite)
    else:
        prefix = os.path.join(model_dir, 'epm_model')

    os.makedirs(os.path.dirname(prefix) or '.', exist_ok=True)
    out_model = prefix + '_iso.joblib'
    out_meta  = prefix + '_meta.json'

    joblib.dump({'scaler': scaler, 'model': iso}, out_model, compress=3)
    meta = {
        'trained_at':      datetime.now(timezone.utc).isoformat(),
        'satellite':       satellite or 'all',
        'n_samples':       int(len(X)),
        'contamination':   contamination,
        'n_estimators':    n_estimators,
        'feature_cols':    feat_cols,
        'base_features':   BASE_FEATURES,
        'threshold_warn':  tw,
        'threshold_fault': tf,
        'model_file':      os.path.basename(out_model),
    }
    with open(out_meta, 'w') as f:
        json.dump(meta, f, indent=2)
    print(f'  Saved: {out_model}')
    print(f'  Saved: {out_meta}')


def main():
    ap = argparse.ArgumentParser(
        description='Train EPM bearing anomaly detection model from CSV logs',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--satellite',
                    default=None,
                    help='Train on one satellite only.  '
                         'Default: train one model per satellite automatically '
                         '(reads satellite names from CSV filenames in --log-dir).')
    ap.add_argument('--all-in-one',
                    action='store_true',
                    help='Train a single global model across all satellites '
                         'instead of one model per satellite.')
    ap.add_argument('--contamination',
                    type=float, default=0.05,
                    help='Expected anomaly fraction 0.01–0.45 (default 0.05 = 5%%)')
    ap.add_argument('--n-estimators',
                    type=int, default=200,
                    help='IsolationForest tree count (default 200)')
    ap.add_argument('--output',
                    default=None,
                    help='Output path prefix (overrides default per-satellite naming). '
                         'Two files: <prefix>_iso.joblib and <prefix>_meta.json')
    ap.add_argument('--log-dir',
                    default=LOG_DIR,
                    help=f'CSV log directory (default {LOG_DIR})')
    ap.add_argument('--model-dir',
                    default=MODEL_DIR,
                    help=f'Directory to save trained models (default {MODEL_DIR})')
    args = ap.parse_args()

    if not (0.01 <= args.contamination <= 0.45):
        sys.exit('--contamination must be between 0.01 and 0.45')

    os.makedirs(args.model_dir, exist_ok=True)

    # ── Per-satellite mode (default) ─────────────────────────────────────────
    if args.satellite:
        print(f'\n[trainer] Training per-satellite model for: {args.satellite}')
        _train_and_save(args.satellite, args.log_dir, args.model_dir, args.output,
                        args.contamination, args.n_estimators)
        prefix = args.output or os.path.join(args.model_dir, args.satellite)
        print(f'\nTo enable for this satellite:')
        print(f'  python recv_verify.py  (model auto-loaded on satellite connect)')
        print(f'To run offline analysis:')
        print(f'  python ml_infer.py --model {prefix}')
        return

    if args.all_in_one:
        print('\n[trainer] Training single global model (all satellites combined)…')
        output_prefix = args.output or os.path.join(args.model_dir, 'epm_model')
        _train_and_save(None, args.log_dir, args.model_dir, output_prefix,
                        args.contamination, args.n_estimators)
        print(f'\nTo enable global ML alerting:')
        print(f'  python recv_verify.py --model {output_prefix}')
        return

    # ── Default: auto-discover satellites and train one model each ───────────
    satellites = _discover_satellites(args.log_dir)
    if not satellites:
        sys.exit(f'No CSV files found in {args.log_dir}.  '
                 f'Run recv_verify.py first to collect training data.')

    print(f'\n[trainer] Auto-discovered {len(satellites)} satellite(s): {satellites}')
    for sat in satellites:
        print(f'\n  ── {sat} ──')
        try:
            _train_and_save(sat, args.log_dir, args.model_dir, None,
                            args.contamination, args.n_estimators)
        except SystemExit as e:
            print(f'  Skipped {sat}: {e}')

    print(f'\n[trainer] Done.  Per-satellite models saved to {args.model_dir}/')
    print('  Models are auto-loaded by recv_verify.py when each satellite connects.')


if __name__ == '__main__':
    main()
