#!/usr/bin/env python3
"""
ml_infer.py — Run ML inference on EPM CSV logs and generate a fault report.

Loads the IsolationForest model produced by ml_trainer.py and evaluates it
against historical CSV data, producing a per-frame anomaly score, ML-based
alert classification, and comparison with the original threshold-based alerts.

This tool is useful for:
  - Validating the trained model against known-good and known-bad periods
  - Tuning contamination rate before deploying the model live
  - Generating maintenance reports from historical sensor data
  - Understanding which machines are most at risk

Usage:
  python ml_infer.py                                  # default model + all logs
  python ml_infer.py --model model/epm_model          # explicit model prefix
  python ml_infer.py --satellite SAT-A3B4             # one satellite only
  python ml_infer.py --export predictions.csv         # save frame-level output
  python ml_infer.py --top-anomalies 20               # show 20 worst frames
"""

import argparse
import glob
import json
import os
import sys

import numpy as np

try:
    import pandas as pd
except ImportError:
    sys.exit('pandas not installed.  Run: pip install pandas scikit-learn joblib')

try:
    import joblib
    from sklearn.metrics import classification_report
except ImportError:
    sys.exit('scikit-learn / joblib not installed.  Run: pip install scikit-learn joblib')

LOG_DIR   = os.path.join(os.path.dirname(__file__), 'logs')
MODEL_DIR = os.path.join(os.path.dirname(__file__), 'model')

ALERT_LABELS = {0: 'OK', 1: 'WARN', 2: 'FAULT'}
ALERT_INT    = {'OK': 0, 'WARN': 1, 'FAULT': 2,
                'ok': 0, 'warn': 1, 'fault': 2}


def _load_model(prefix: str) -> tuple:
    meta_p  = prefix + '_meta.json'
    model_p = prefix + '_iso.joblib'
    for p in (meta_p, model_p):
        if not os.path.exists(p):
            sys.exit(f'Model file not found: {p}\n'
                     f'Run ml_trainer.py first to train a model.')
    with open(meta_p) as f:
        meta = json.load(f)
    bundle = joblib.load(model_p)
    print(f'[infer] Model loaded')
    print(f'  trained : {meta["trained_at"]}')
    print(f'  on      : {meta.get("n_samples", "?")} samples  '
          f'({meta.get("satellite", "all")} satellites)')
    print(f'  contamination: {meta.get("contamination", "?"):.0%}')
    return bundle['scaler'], bundle['model'], meta


def _load_csvs(satellite: str | None, log_dir: str) -> 'pd.DataFrame':
    pattern = f'epm_{satellite}_*.csv' if satellite else 'epm_*.csv'
    files   = sorted(glob.glob(os.path.join(log_dir, pattern)))
    if not files:
        sys.exit(f'No CSV files in {log_dir}.  Run recv_verify.py first.')
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f)
            df['_source'] = os.path.basename(f)
            dfs.append(df)
        except Exception as e:
            print(f'  WARNING: skipping {os.path.basename(f)}: {e}')
    return pd.concat(dfs, ignore_index=True)


def _run_inference(scaler, iso, meta: dict,
                   df: 'pd.DataFrame') -> tuple:
    feat_cols = meta.get('feature_cols', meta.get('base_features', []))
    avail     = [c for c in feat_cols if c in df.columns]
    missing   = [c for c in feat_cols if c not in df.columns]

    if missing:
        print(f'  WARNING: {len(missing)} feature columns missing '
              f'({missing}) — filling with 0')
        for c in missing:
            df[c] = 0.0

    X   = df[feat_cols].replace([float('inf'), float('-inf')],
                                 float('nan')).fillna(0).values
    X_s = scaler.transform(X)

    scores    = iso.decision_function(X_s)
    t_warn    = meta['threshold_warn']
    t_fault   = meta['threshold_fault']
    ml_alert  = np.where(scores <= t_fault, 2,
                np.where(scores <= t_warn,  1, 0))
    return scores, ml_alert


def main():
    ap = argparse.ArgumentParser(
        description='EPM ML inference on historical CSV logs',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--model',
                    default=os.path.join(MODEL_DIR, 'epm_model'),
                    help='Model prefix from ml_trainer.py (no extension)')
    ap.add_argument('--satellite',
                    default=None,
                    help='Analyse one satellite only (default: all)')
    ap.add_argument('--log-dir',
                    default=LOG_DIR)
    ap.add_argument('--export',
                    default=None,
                    help='Export per-frame predictions to this CSV path')
    ap.add_argument('--top-anomalies',
                    type=int, default=10,
                    help='Print the N most anomalous frames (default 10)')
    args = ap.parse_args()

    scaler, iso, meta = _load_model(args.model)

    print(f'\n[infer] Loading CSV data from {args.log_dir}…')
    df = _load_csvs(args.satellite, args.log_dir)
    print(f'  {len(df):,} rows loaded')

    scores, ml_alert = _run_inference(scaler, iso, meta, df)
    df['ml_score'] = scores
    df['ml_alert'] = [ALERT_LABELS[a] for a in ml_alert]

    # ── Fleet summary ──────────────────────────────────────────────────────────
    SEP = '─' * 58
    print(f'\n{SEP}')
    print(f'  ML Inference Summary')
    print(SEP)
    for label in ('OK', 'WARN', 'FAULT'):
        n   = int((df['ml_alert'] == label).sum())
        pct = n / len(df) * 100
        bar = '█' * int(pct / 2)
        print(f'  {label:<6} {n:>6} frames  ({pct:5.1f}%)  {bar}')
    print(f'  Total  {len(df):>6} frames')
    print(SEP)

    # ── Per-satellite breakdown ────────────────────────────────────────────────
    if '_source' in df.columns:
        print(f'\n  Per-satellite anomaly rate:')
        for src, grp in df.groupby('_source'):
            n_fault = int((grp['ml_alert'] == 'FAULT').sum())
            n_warn  = int((grp['ml_alert'] == 'WARN').sum())
            pct_f   = n_fault / len(grp) * 100
            pct_w   = n_warn  / len(grp) * 100
            status  = ('FAULT' if pct_f > 5 else
                       'WARN'  if pct_w > 10 else 'OK')
            print(f'  {src:<35}  {status:<5}  '
                  f'FAULT {pct_f:4.1f}%  WARN {pct_w:4.1f}%')

    # ── Comparison with threshold-based alerts (if 'alert' column present) ─────
    if 'alert' in df.columns:
        orig = df['alert'].map(ALERT_INT).fillna(0).astype(int)
        agree_pct = float((orig.values == ml_alert).mean()) * 100
        print(f'\n  Agreement with original threshold-based alerts: {agree_pct:.1f}%')
        try:
            print()
            print(classification_report(orig, ml_alert,
                  target_names=['OK', 'WARN', 'FAULT'], zero_division=0))
        except Exception:
            pass

    # ── Top anomalies ─────────────────────────────────────────────────────────
    top = df.nsmallest(args.top_anomalies, 'ml_score')
    print(f'\n  Top {args.top_anomalies} most anomalous frames:')
    show = ['_source', 'wall_time', 'ml_score', 'ml_alert',
            'mic_kurtosis', 'mic_crest', 'z_score']
    show = [c for c in show if c in top.columns]
    pd.set_option('display.max_colwidth', 35)
    pd.set_option('display.float_format', '{:.4f}'.format)
    print(top[show].to_string(index=False))

    # ── Export ────────────────────────────────────────────────────────────────
    if args.export:
        df.to_csv(args.export, index=False)
        print(f'\n[infer] Predictions exported → {args.export}')

    print()


if __name__ == '__main__':
    main()
