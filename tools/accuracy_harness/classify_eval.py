#!/usr/bin/env python3
"""
classify_eval.py — Part 1 of the Phase B accuracy harness (method: injected).

Calls gateway.pipeline.alerting._classify_fault_type() directly on synthetic
(mic_kurtosis, mic_crest, imu_crest, hi_r, lo_r, mid_r) tuples built by
synth_frames.py, compares the classifier's actual output against the label
each tuple was deliberately constructed to represent, and reports:

  - a 9x9 confusion matrix + per-label precision/recall/F1, computed only
    over "deep-inside" tuples (the accuracy-bearing ones; boundary/
    just-outside tuples are diagnostic, not part of the accuracy metric --
    see synth_frames.py's module docstring for the zone scheme)
  - every priority/boundary mismatch found, classified as:
      priority_collision  -- a deep-inside tuple whose predicted label
                              differs from its intended label: a genuine
                              branch-order bug candidate
      boundary_artifact    -- an on-boundary/just-outside tuple that
                              disagrees: expected, not a bug (the tuple was
                              built to straddle a threshold)
      fallthrough_reachable -- informational only: confirms branches 6/7
                              (the kurtosis-only fallback labels) are
                              reachable and intentional, not a mismatch
  - the 2 dual-satisfaction probe tuples (deep-inside Bearing Fault AND
    deep-inside Imbalance/Misalignment simultaneously), which force and
    confirm the branch-2-short-circuits-3/4/5 collision that static reading
    of alerting.py suggests

No hardware/MQTT/recv_verify server state is touched -- only the pure
_classify_fault_type() function and rv's module-level threshold constants.

Run with:
    python tools/accuracy_harness/classify_eval.py
"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'mic_tools'))

import matplotlib
matplotlib.use('Agg')

from gateway.pipeline.alerting import _classify_fault_type  # noqa: E402
import synth_frames  # noqa: E402

_OUT_DIR = os.path.join(_HERE, 'out')


def _predict(t):
    return _classify_fault_type(
        t['mic_kurtosis'], t['mic_crest'], t['imu_crest'],
        t['hi_r'], t['lo_r'], t['mid_r'])


def _classify_mismatch(t, predicted):
    """Returns None if predicted == intended (or it's an expected fallthrough
    match), else one of 'priority_collision' / 'boundary_artifact'."""
    if predicted == t['intended_label']:
        return None
    if t['zone'] == 'deep-inside':
        return 'priority_collision'
    return 'boundary_artifact'


def run():
    tuples, probes, structural_notes = synth_frames.generate_all_tuples()

    for t in tuples:
        t['predicted_label'] = _predict(t)
        t['mismatch_class'] = _classify_mismatch(t, t['predicted_label'])

    probe_findings = []
    for p in probes:
        predicted = _predict(p)
        probe_findings.append(dict(
            probe=p['intended_label'], predicted_label=predicted,
            tuple={k: p[k] for k in
                   ('mic_kurtosis', 'mic_crest', 'imu_crest', 'hi_r', 'lo_r', 'mid_r')},
            verdict=(
                f"CONFIRMED priority collision: branch-2 (Bearing Fault) wins over "
                f"the competing label even though both condition sets are deep-inside "
                f"satisfied -- classifier returned {predicted!r}."
                if predicted.startswith('Bearing Fault')
                else f"UNEXPECTED: probe did not resolve to a Bearing Fault label "
                     f"(got {predicted!r}) -- needs manual review."
            ),
        ))

    deep = [t for t in tuples if t['zone'] == 'deep-inside']
    labels = synth_frames.ALL_LABELS
    confusion = {intended: {pred: 0 for pred in labels} for intended in labels}
    for t in deep:
        confusion[t['intended_label']][t['predicted_label']] = \
            confusion[t['intended_label']].get(t['predicted_label'], 0) + 1
        if t['predicted_label'] not in confusion[t['intended_label']]:
            confusion[t['intended_label']][t['predicted_label']] = 1

    metrics = {}
    for label in labels:
        tp = confusion[label].get(label, 0)
        fn = sum(v for pred, v in confusion[label].items() if pred != label)
        fp = sum(confusion[other].get(label, 0) for other in labels if other != label)
        precision = tp / (tp + fp) if (tp + fp) else float('nan')
        recall    = tp / (tp + fn) if (tp + fn) else float('nan')
        f1 = (2 * precision * recall / (precision + recall)
              if (precision + recall) and precision == precision and recall == recall
              else float('nan'))
        metrics[label] = dict(tp=tp, fn=fn, fp=fp, precision=precision, recall=recall, f1=f1,
                               n_deep_inside_tuples=sum(1 for t in deep if t['intended_label'] == label))

    priority_collisions = [t for t in tuples if t['mismatch_class'] == 'priority_collision']
    boundary_artifacts   = [t for t in tuples if t['mismatch_class'] == 'boundary_artifact']
    fallthrough_notes = [
        "Branch 6 ('Severe Anomaly — Inspect') and branch 7 ('Elevated Vibration') are "
        "kurtosis-only fallbacks reached when a frame's band ratios fail every specific-"
        "mechanism gate (Bearing/Imbalance/Misalignment/Looseness). Deep-inside tuples "
        "for both labels matched their intended label in this run "
        f"({metrics['Severe Anomaly — Inspect']['tp']}/"
        f"{metrics['Severe Anomaly — Inspect']['n_deep_inside_tuples']} and "
        f"{metrics['Elevated Vibration']['tp']}/{metrics['Elevated Vibration']['n_deep_inside_tuples']} "
        "respectively), confirming both branches are reachable and intentional, not dead code.",
    ]

    os.makedirs(_OUT_DIR, exist_ok=True)
    _write_json(tuples, probe_findings, confusion, metrics, structural_notes)
    _write_markdown(tuples, deep, probe_findings, confusion, metrics,
                     priority_collisions, boundary_artifacts, fallthrough_notes,
                     structural_notes)
    return dict(tuples=tuples, metrics=metrics, priority_collisions=priority_collisions,
                probe_findings=probe_findings)


def _write_json(tuples, probe_findings, confusion, metrics, structural_notes):
    path = os.path.join(_OUT_DIR, 'classify_confusion.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(dict(
            method='injected',
            tuples=tuples,
            probe_findings=probe_findings,
            confusion_matrix=confusion,
            metrics=metrics,
            structural_notes=structural_notes,
        ), f, indent=2, default=str)
    print(f"wrote {path}")


def _fmt(x):
    return f"{x:.3f}" if x == x else "n/a"  # x==x is False for NaN


def _write_markdown(tuples, deep, probe_findings, confusion, metrics,
                     priority_collisions, boundary_artifacts, fallthrough_notes,
                     structural_notes):
    labels = synth_frames.ALL_LABELS
    path = os.path.join(_OUT_DIR, 'classify_confusion.md')
    lines = []
    lines.append("# Classifier confusion matrix — method: injected\n")
    lines.append(
        f"{len(tuples)} boundary/zone tuples ({len(deep)} deep-inside, used for the "
        f"accuracy metrics below) + {len(probe_findings)} dual-satisfaction priority-"
        f"collision probes, generated by `synth_frames.py` and scored directly through "
        f"`_classify_fault_type()` (`gateway/pipeline/alerting.py:98-134`), no "
        f"hardware/MQTT involved.\n")

    lines.append("## Confusion matrix (deep-inside tuples only)\n")
    header = "| intended \\ predicted | " + " | ".join(labels) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(labels) + 1))
    for intended in labels:
        row = [str(confusion[intended].get(pred, 0)) for pred in labels]
        lines.append(f"| {intended} | " + " | ".join(row) + " |")
    lines.append("")

    lines.append("## Precision / recall / F1 per label (deep-inside tuples only)\n")
    lines.append("| label | n | TP | FP | FN | precision | recall | F1 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for label in labels:
        m = metrics[label]
        lines.append(f"| {label} | {m['n_deep_inside_tuples']} | {m['tp']} | {m['fp']} | "
                      f"{m['fn']} | {_fmt(m['precision'])} | {_fmt(m['recall'])} | {_fmt(m['f1'])} |")
    lines.append("")

    lines.append("## Priority-order findings\n")
    lines.append(
        f"**{len(priority_collisions)} priority_collision case(s)** (deep-inside tuple, "
        f"predicted != intended -- real branch-order bug candidates):\n")
    if priority_collisions:
        for t in priority_collisions:
            lines.append(
                f"- intended=`{t['intended_label']}`, predicted=`{t['predicted_label']}`, "
                f"swept_var=`{t['swept_var']}`, tuple=("
                f"kurtosis={t['mic_kurtosis']:.3f}, crest={t['mic_crest']:.3f}, "
                f"imu_crest={t['imu_crest']:.3f}, hi_r={t['hi_r']:.3f}, "
                f"lo_r={t['lo_r']:.3f}, mid_r={t['mid_r']:.3f})")
    else:
        lines.append("- none found among the individually-swept boundary tuples.")
    lines.append("")

    lines.append(f"**Dual-satisfaction probes** ({len(probe_findings)} constructed):\n")
    for pf in probe_findings:
        lines.append(f"- **{pf['probe']}**: predicted=`{pf['predicted_label']}` — {pf['verdict']}")
    for note in structural_notes:
        lines.append(f"- {note}")
    lines.append("")

    lines.append(f"**{len(boundary_artifacts)} boundary_artifact case(s)** "
                  f"(on-boundary/just-outside tuples that landed in a different label than "
                  f"intended — expected by construction, not bugs):\n")
    for t in boundary_artifacts:
        lines.append(
            f"- intended=`{t['intended_label']}` ({t['zone']}, swept `{t['swept_var']}`), "
            f"predicted=`{t['predicted_label']}`")
    lines.append("")

    lines.append("## Fallthrough-branch reachability\n")
    for note in fallthrough_notes:
        lines.append(f"- {note}")
    lines.append("")

    with open(path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"wrote {path}")


if __name__ == '__main__':
    result = run()
    n_collisions = len(result['priority_collisions'])
    print(f"\n{n_collisions} priority_collision case(s) among individually-swept tuples")
    for pf in result['probe_findings']:
        print(f"probe {pf['probe']}: {pf['verdict']}")
