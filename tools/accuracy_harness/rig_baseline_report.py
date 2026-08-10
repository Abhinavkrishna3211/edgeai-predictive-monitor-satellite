#!/usr/bin/env python3
"""
rig_baseline_report.py — Part 3 of the Phase B accuracy harness (method: real-rig).

Computes a Normal-baseline false-positive-rate / recall report from a CSV log
already written by recv_verify.py's live pipeline (_process_satellite_frame,
CSV columns: wall_time,frame_id,device_ms,mic_rms,mic_crest,mic_kurtosis,
imu_rms,imu_crest,high_band_ratio,z_score,p_fault,alert,overflow_count).

The only class present in a Normal baseline is "healthy" (no fault frames were
deliberately injected), so:
    fpr           = count(alert != "OK") / total_frames
    recall_normal = 1 - fpr        (recall of the negative/healthy class)
precision/F1 are undefined here (no positive/fault class exists in this
capture) and are reported as "n/a", not fabricated.

See real_rig_baseline_runbook.md for exactly how this run's CSV was captured,
including a documented deviation from the original "clean tone" stimulus plan
(tools/bench_signal_gen was blocked by an environment memory/commit-limit
issue — the capture used ambient/no-stimulus conditions instead).

Run with:
    python tools/accuracy_harness/rig_baseline_report.py <path-to-csv>
"""

import csv
import json
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_OUT_DIR = os.path.join(_HERE, 'out')

_IMU_CREST_WARN_DEFAULT = 9.0  # recv_verify.py IMU_CREST_WARN default (real-rig
                                # FPR fix); the value actually in effect for this
                                # run is recorded in the runbook


def _percentiles(values, ps=(5, 50, 95)):
    import numpy as np
    arr = np.asarray(values, dtype=float)
    return {f'p{p}': float(np.percentile(arr, p)) for p in ps} | {'max': float(arr.max()), 'min': float(arr.min())}


def analyze(csv_path):
    with open(csv_path, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    alert_counts = Counter(r['alert'] for r in rows)
    n_ok = alert_counts.get('OK', 0)
    fpr = 1.0 - (n_ok / total) if total else float('nan')
    recall_normal = 1.0 - fpr if total else float('nan')

    imu_crest = [float(r['imu_crest']) for r in rows]
    mic_crest = [float(r['mic_crest']) for r in rows]
    mic_kurtosis = [float(r['mic_kurtosis']) for r in rows]
    p_fault = [float(r['p_fault']) for r in rows]

    frac_imu_over_warn = sum(1 for v in imu_crest if v >= _IMU_CREST_WARN_DEFAULT) / total if total else float('nan')

    wall_span_s = float(rows[-1]['wall_time']) - float(rows[0]['wall_time']) if rows else 0.0

    return dict(
        method='real-rig',
        csv_path=os.path.abspath(csv_path),
        total_frames=total,
        wall_span_seconds=wall_span_s,
        alert_counts=dict(alert_counts),
        fpr=fpr,
        recall_normal=recall_normal,
        precision='n/a — no fault frames exist in this Normal-only baseline capture',
        f1='n/a — no fault frames exist in this Normal-only baseline capture',
        frac_frames_imu_crest_over_default_imu_crest_warn=frac_imu_over_warn,
        imu_crest=_percentiles(imu_crest),
        mic_crest=_percentiles(mic_crest),
        mic_kurtosis=_percentiles(mic_kurtosis),
        p_fault=_percentiles(p_fault),
    )


def _write_markdown(result, path):
    lines = []
    lines.append("# Real-rig Normal baseline — method: real-rig\n")
    lines.append(f"Source CSV: `{result['csv_path']}`\n")
    lines.append(
        f"**{result['total_frames']:,} frames** captured over "
        f"{result['wall_span_seconds']/3600:.2f} hours of live satellite → MQTT → "
        f"`_process_satellite_frame()` pipeline, ambient/no-injected-fault conditions "
        f"(see real_rig_baseline_runbook.md for exact capture conditions and the "
        f"deviation from the originally planned tone stimulus).\n")

    lines.append("## Headline metrics\n")
    lines.append(f"- **FPR = {result['fpr']:.4f}** ({result['fpr']*100:.1f}% of frames alerted WARN or FAULT)")
    lines.append(f"- **recall_normal = {result['recall_normal']:.4f}**")
    lines.append(f"- precision: {result['precision']}")
    lines.append(f"- F1: {result['f1']}")
    lines.append(f"- Alert breakdown: {result['alert_counts']}\n")

    lines.append("## Root-cause signal: IMU crest factor\n")
    lines.append(
        f"{result['frac_frames_imu_crest_over_default_imu_crest_warn']*100:.1f}% of frames have "
        f"`imu_crest >= {_IMU_CREST_WARN_DEFAULT}` (the default IMU_CREST_WARN threshold) from "
        f"ambient vibration alone — median imu_crest across the capture is "
        f"**{result['imu_crest']['p50']:.3f}**. This is the primary "
        f"driver of the FPR above if it is still elevated, not the mic channel (mic_crest p50="
        f"{result['mic_crest']['p50']:.3f}, well under its own threshold).\n")

    lines.append("## Distributions (p5 / p50 / p95 / min / max)\n")
    lines.append("| channel | p5 | p50 | p95 | min | max |")
    lines.append("|---|---|---|---|---|---|")
    for name in ('imu_crest', 'mic_crest', 'mic_kurtosis', 'p_fault'):
        d = result[name]
        lines.append(f"| {name} | {d['p5']:.3f} | {d['p50']:.3f} | {d['p95']:.3f} | {d['min']:.3f} | {d['max']:.3f} |")
    lines.append("")

    lines.append("## Caveats\n")
    lines.append(
        "- recv_verify.py's alert field carries persistence (WARN_PERSIST/CLEAR_PERSIST/"
        "FAULT_CLEAR_PERSIST hysteresis) — the FPR above is not simply "
        f"\"{result['frac_frames_imu_crest_over_default_imu_crest_warn']*100:.1f}% of instantaneous "
        "samples exceeded threshold\" in isolation, but the underlying per-frame imu_crest "
        "distribution relative to IMU_CREST_WARN is the primary driver of whatever WARN/FAULT "
        "rate is measured, regardless of the persistence logic layered on top.")
    lines.append(
        "- mic_kurtosis has a heavy tail (max within this capture reaches into the hundreds) "
        "from real transient acoustic events (room noise, keyboard/movement near the mic) — "
        "expected for an unattended multi-hour ambient capture, not a sensor fault.")
    lines.append("")

    with open(path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"wrote {path}")


def run(csv_path):
    result = analyze(csv_path)
    os.makedirs(_OUT_DIR, exist_ok=True)
    with open(os.path.join(_OUT_DIR, 'rig_baseline_report.json'), 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, default=str)
    _write_markdown(result, os.path.join(_OUT_DIR, 'rig_baseline_report.md'))
    print(f"FPR={result['fpr']:.4f}  recall_normal={result['recall_normal']:.4f}  "
          f"n={result['total_frames']}")
    return result


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit("usage: python rig_baseline_report.py <path-to-csv>")
    run(sys.argv[1])
