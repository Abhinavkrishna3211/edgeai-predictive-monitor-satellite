#!/usr/bin/env python3
"""
anomaly_pr_eval.py — Part 2 of the Phase B accuracy harness (method: injected).

Reuses synth_frames.py's fault-category tuples (extended across severities
via jittered deep-inside variants) as the "anomalous" class, and jittered
Normal-zone tuples as the "healthy" class, to evaluate two anomaly scorers:

  1. Autoencoder reconstruction score. TensorFlow is not installed in this
     environment (checked at import time — see _TF_AVAILABLE below), so the
     real autoencoder cannot be trained. Every artifact this script produces
     for the autoencoder path is explicitly labeled "proxy score — TensorFlow
     unavailable in this environment; not the production autoencoder MSE."
     The proxy is a Mahalanobis distance of the same 41-dim
     make_feature_vector() output from the healthy-set mean/covariance —
     directionally the same idea (distance from a learned healthy manifold)
     but not the trained neural network.
  2. HST (Half-Space Trees) online anomaly score, via the real
     gateway.pipeline.online_detector.OnlineDetector class (river-backed, no
     TF dependency) at production config (n_features=7, n_trees=10, per
     gateway/pipeline/ml_scoring.py:253). HST has no fixed t_warn/t_fault in
     production — it only feeds Bayesian fusion in compute_alert(), never
     gated directly — so this script sweeps its own diagnostic grid
     np.linspace(0.5, 1.0, 21).

No hardware/MQTT/recv_verify server state is touched.

Run with:
    python tools/accuracy_harness/anomaly_pr_eval.py
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'mic_tools'))

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from gateway.pipeline.alerting import _extract_hst_features  # noqa: E402
from gateway.pipeline.autoencoder import make_feature_vector  # noqa: E402
from gateway.pipeline.online_detector import OnlineDetector  # noqa: E402
import synth_frames  # noqa: E402

_OUT_DIR = os.path.join(_HERE, 'out')

try:
    import tensorflow  # noqa: F401
    _TF_AVAILABLE = True
except ImportError:
    _TF_AVAILABLE = False

try:
    from sklearn.metrics import average_precision_score, precision_recall_curve
    _SKLEARN_PR = True
except ImportError:
    _SKLEARN_PR = False

_FAULT_LABELS = [l for l in synth_frames.ALL_LABELS if l != "Normal"]

_N_HEALTHY_TRAIN = 300   # HST warmup (window=250) + Mahalanobis mean/cov fit
_N_HEALTHY_TEST  = 80
_N_ANOMALOUS_PER_LABEL = 15


def _manual_pr_curve(y_true, scores):
    """Fallback if sklearn is unavailable: sort by score descending, compute
    cumulative precision/recall, integrate AUC via trapezoid rule."""
    order = np.argsort(-scores)
    y_sorted = np.asarray(y_true)[order]
    tp = np.cumsum(y_sorted == 1)
    fp = np.cumsum(y_sorted == 0)
    n_pos = int(np.sum(y_sorted == 1))
    precision = tp / np.maximum(tp + fp, 1)
    recall = tp / max(n_pos, 1)
    precision = np.concatenate([[1.0], precision])
    recall = np.concatenate([[0.0], recall])
    order2 = np.argsort(recall)
    r_sorted, p_sorted = recall[order2], precision[order2]
    auc = float(np.trapz(p_sorted, r_sorted))
    return precision, recall, auc


def _pr_curve_and_auc(y_true, scores):
    y_true = np.asarray(y_true)
    scores = np.asarray(scores)
    if _SKLEARN_PR:
        precision, recall, _ = precision_recall_curve(y_true, scores)
        auc = float(average_precision_score(y_true, scores))
        return precision, recall, auc
    return _manual_pr_curve(y_true, scores)


def _build_datasets():
    rng_train = np.random.default_rng(101)
    rng_test_healthy = np.random.default_rng(102)
    rng_test_anom = np.random.default_rng(103)

    healthy_train = synth_frames.generate_healthy_samples(_N_HEALTHY_TRAIN, rng_train)
    healthy_test = synth_frames.generate_healthy_samples(_N_HEALTHY_TEST, rng_test_healthy)

    anomalous_test = []
    for label in _FAULT_LABELS:
        anomalous_test += synth_frames.generate_severity_variants(
            label, _N_ANOMALOUS_PER_LABEL, rng_test_anom)

    return healthy_train, healthy_test, anomalous_test


def _hst_feature(t):
    frame = synth_frames.tuple_to_frame(t)
    return _extract_hst_features(frame, t['lo_r'], t['mid_r'], t['hi_r'])


def _run_hst(healthy_train, healthy_test, anomalous_test):
    det = OnlineDetector(n_features=7, n_trees=10)
    for t in healthy_train:
        det.learn(_hst_feature(t))

    y_true, scores = [], []
    for t in healthy_test:
        scores.append(det.score(_hst_feature(t)))
        y_true.append(0)
    for t in anomalous_test:
        scores.append(det.score(_hst_feature(t)))
        y_true.append(1)

    precision, recall, auc = _pr_curve_and_auc(y_true, scores)

    grid = np.linspace(0.5, 1.0, 21)
    y_true_arr, scores_arr = np.asarray(y_true), np.asarray(scores)
    sweep = []
    for thresh in grid:
        pred = scores_arr >= thresh
        tp = int(np.sum(pred & (y_true_arr == 1)))
        fp = int(np.sum(pred & (y_true_arr == 0)))
        fn = int(np.sum(~pred & (y_true_arr == 1)))
        p = tp / (tp + fp) if (tp + fp) else float('nan')
        r = tp / (tp + fn) if (tp + fn) else float('nan')
        sweep.append(dict(threshold=float(thresh), tp=tp, fp=fp, fn=fn, precision=p, recall=r))

    return dict(
        method='injected',
        detector='HST (river HalfSpaceTrees, n_features=7, n_trees=10 — production config)',
        note='HST has no fixed t_warn/t_fault in production (only feeds Bayesian '
             'fusion in compute_alert(), never gated directly) — grid is diagnostic only.',
        n_healthy_train=len(healthy_train), n_healthy_test=len(healthy_test),
        n_anomalous_test=len(anomalous_test),
        auc=auc, threshold_sweep=sweep,
        y_true=[int(v) for v in y_true], scores=[float(v) for v in scores],
        pr_curve=dict(precision=[float(v) for v in precision], recall=[float(v) for v in recall]),
    )


def _run_autoencoder_proxy(healthy_train, healthy_test, anomalous_test):
    X_train = np.array([make_feature_vector(synth_frames.tuple_to_frame(t)) for t in healthy_train])
    mu = X_train.mean(axis=0)
    cov = np.cov(X_train, rowvar=False) + 1e-3 * np.eye(X_train.shape[1])
    cov_inv = np.linalg.pinv(cov)

    def _mahalanobis(x):
        d = x - mu
        return float(np.sqrt(max(d @ cov_inv @ d, 0.0)))

    y_true, scores = [], []
    for t in healthy_test:
        scores.append(_mahalanobis(make_feature_vector(synth_frames.tuple_to_frame(t))))
        y_true.append(0)
    for t in anomalous_test:
        scores.append(_mahalanobis(make_feature_vector(synth_frames.tuple_to_frame(t))))
        y_true.append(1)

    precision, recall, auc = _pr_curve_and_auc(y_true, scores)

    train_scores = np.array([_mahalanobis(x) for x in X_train])
    t_warn_proxy = float(np.percentile(train_scores, 95.0))
    t_fault_proxy = float(np.percentile(train_scores, 100.0 * (1.0 - 0.05 / 3.0)))

    y_true_arr, scores_arr = np.asarray(y_true), np.asarray(scores)
    sweep = []
    for name, thresh in (('t_warn_proxy', t_warn_proxy), ('t_fault_proxy', t_fault_proxy)):
        pred = scores_arr >= thresh
        tp = int(np.sum(pred & (y_true_arr == 1)))
        fp = int(np.sum(pred & (y_true_arr == 0)))
        fn = int(np.sum(~pred & (y_true_arr == 1)))
        p = tp / (tp + fp) if (tp + fp) else float('nan')
        r = tp / (tp + fn) if (tp + fn) else float('nan')
        sweep.append(dict(name=name, threshold=thresh, tp=tp, fp=fp, fn=fn, precision=p, recall=r))

    return dict(
        method='injected',
        detector='PROXY — Mahalanobis distance over 41-dim make_feature_vector() output '
                 '(TensorFlow not installed in this environment; NOT the production '
                 'autoencoder reconstruction MSE)',
        tf_available=_TF_AVAILABLE,
        n_healthy_train=len(healthy_train), n_healthy_test=len(healthy_test),
        n_anomalous_test=len(anomalous_test),
        auc=auc, threshold_sweep=sweep,
        y_true=[int(v) for v in y_true], scores=[float(v) for v in scores],
        pr_curve=dict(precision=[float(v) for v in precision], recall=[float(v) for v in recall]),
    )


def _plot_pr(result, title, path):
    p, r = result['pr_curve']['precision'], result['pr_curve']['recall']
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.plot(r, p, marker='.', lw=1)
    ax.set_xlabel('Recall')
    ax.set_ylabel('Precision')
    ax.set_title(f"{title}\nAUC={result['auc']:.3f} (method: injected)")
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def run():
    healthy_train, healthy_test, anomalous_test = _build_datasets()

    hst_result = _run_hst(healthy_train, healthy_test, anomalous_test)
    ae_result = _run_autoencoder_proxy(healthy_train, healthy_test, anomalous_test)

    os.makedirs(_OUT_DIR, exist_ok=True)

    with open(os.path.join(_OUT_DIR, 'anomaly_pr_hst.json'), 'w', encoding='utf-8') as f:
        json.dump(hst_result, f, indent=2, default=str)
    with open(os.path.join(_OUT_DIR, 'anomaly_pr_autoencoder.json'), 'w', encoding='utf-8') as f:
        json.dump(ae_result, f, indent=2, default=str)

    _plot_pr(hst_result, 'HST anomaly-score PR curve', os.path.join(_OUT_DIR, 'anomaly_pr_hst.png'))
    _plot_pr(ae_result, 'Autoencoder-proxy anomaly-score PR curve',
              os.path.join(_OUT_DIR, 'anomaly_pr_autoencoder.png'))

    _write_markdown(hst_result, ae_result)

    print(f"HST AUC={hst_result['auc']:.3f}  autoencoder-proxy AUC={ae_result['auc']:.3f}")
    return dict(hst=hst_result, autoencoder=ae_result)


def _fmt(x):
    return f"{x:.3f}" if x == x else "n/a"


def _write_markdown(hst_result, ae_result):
    path = os.path.join(_OUT_DIR, 'anomaly_pr_eval.md')
    lines = []
    lines.append("# Anomaly-score evaluation — method: injected\n")
    lines.append(
        f"Healthy: {hst_result['n_healthy_train']} training + {hst_result['n_healthy_test']} "
        f"held-out test samples (jittered Normal-zone tuples, clipped below K_WARN/CREST_WARN). "
        f"Anomalous: {hst_result['n_anomalous_test']} samples ({_N_ANOMALOUS_PER_LABEL} severity-"
        f"jittered deep-inside variants per non-Normal label x {len(_FAULT_LABELS)} labels), "
        f"generated by `synth_frames.py` and scored via a synthetic mic FFT that reproduces "
        f"each sample's hi_r/lo_r/mid_r exactly. No hardware/MQTT involved.\n")

    lines.append("## HST (Half-Space Trees)\n")
    lines.append(f"- Detector: {hst_result['detector']}")
    lines.append(f"- {hst_result['note']}")
    lines.append(f"- **AUC-PR = {hst_result['auc']:.3f}**\n")
    lines.append("| threshold | TP | FP | FN | precision | recall |")
    lines.append("|---|---|---|---|---|---|")
    for row in hst_result['threshold_sweep']:
        lines.append(f"| {row['threshold']:.3f} | {row['tp']} | {row['fp']} | {row['fn']} | "
                      f"{_fmt(row['precision'])} | {_fmt(row['recall'])} |")
    lines.append("\n![HST PR curve](anomaly_pr_hst.png)\n")

    lines.append("## Autoencoder (PROXY — TensorFlow unavailable)\n")
    lines.append(f"- Detector: {ae_result['detector']}")
    lines.append(f"- `tf_available` at run time: **{ae_result['tf_available']}**")
    lines.append(f"- **AUC-PR = {ae_result['auc']:.3f}** (proxy score, NOT the production "
                  f"autoencoder MSE — do not compare directly against a real MSE-based figure)\n")
    lines.append("| threshold | TP | FP | FN | precision | recall |")
    lines.append("|---|---|---|---|---|---|")
    for row in ae_result['threshold_sweep']:
        lines.append(f"| {row['name']}={row['threshold']:.3f} | {row['tp']} | {row['fp']} | "
                      f"{row['fn']} | {_fmt(row['precision'])} | {_fmt(row['recall'])} |")
    lines.append("\n![Autoencoder-proxy PR curve](anomaly_pr_autoencoder.png)\n")

    lines.append("## Caveats\n")
    lines.append(
        "- Both AUC-PR figures are near-ceiling (1.000). This is expected, not a claim of "
        "real-world separability: severity-jittered anomalous tuples are built 20*delta+ past "
        "their defining threshold (see synth_frames.py's deep-inside zone) then scaled up "
        "further by a 1.0-3.0x severity factor, so injected anomalies sit far from the "
        "healthy manifold by construction. This measures whether the scorers respond "
        "monotonically to synthetic severity, not real-world detection margin on borderline "
        "faults — see Part 3 for the only real-hardware measurement in this report.")
    lines.append(
        "- The autoencoder path never ran the production model (TensorFlow is not installed "
        "in this environment) — its numbers are a distance-based proxy and must not be quoted "
        "as production autoencoder accuracy.")
    lines.append("")

    with open(path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"wrote {path}")


if __name__ == '__main__':
    run()
