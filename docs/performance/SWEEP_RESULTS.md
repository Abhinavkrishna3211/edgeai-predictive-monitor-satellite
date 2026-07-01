# Simulation Sweep Results -- Phases 2-4

# HST Hyperparameter Sweep Results

Protocol: healthy_frames=300, fault_frames=1200, evolution_seconds=900.0, fault_type=outer, 1 seed per cell (OVAT), then 3 seeds for winner.

## n_trees Sweep (height=15, window=250)

|n_trees|Cohen's d|False positives|Detect frame|CPU µs/frame|Peak RSS MB|
|---|---|---|---|---|---|
|10|2.403|0|248|3003.0|171.63|
|25|2.165|0|248|7991.2|400.64|
|50|2.192|0|248|16506.4|828.77|
|100|2.301|0|248|33469.9|1678.45|

## height Sweep (n_trees=best, window=250)

|height|Cohen's d|False positives|Detect frame|CPU µs/frame|Peak RSS MB|
|---|---|---|---|---|---|
|8|2.139|0|248|294.3|0.00|
|12|2.225|0|248|505.3|0.27|
|15|2.403|0|248|1897.5|138.80|
|20|2.861|0|248|121645.8|5439.28|

## window Sweep (n_trees=best, height=best)

|window|Cohen's d|False positives|Detect frame|CPU µs/frame|Peak RSS MB|
|---|---|---|---|---|---|
|100|2.431|0|248|145053.9|5363.18|
|250|2.861|0|248|100257.1|5338.24|
|500|2.485|0|248|103994.0|5348.93|

## Winner vs Current (3-seed average)

|Config|Cohen's d|False positives|Detect frame|Recall|
|---|---|---|---|---|
|Current (25/15/250)|2.453|0|248|0.793|
|Winner (10/20/250)|2.547|0|248|0.793|

**Verdict**: Best config (10/20/250) improves Cohen's d by 3.8% over current (25/15/250). **Within 10% threshold — current values retained.**

---

# Bayesian Fusion Sweep Results

Protocol: healthy_frames=300, fault_frames=1200, evolution_seconds=900.0.
Cost ratio assumed: 10:1 (missed fault vs false alarm) — industrial bearing
failure risks catastrophic machine damage; false alarm costs one inspection visit.

## 3a — Prior Sensitivity

|prior|FP rate|Detect frame|Fault p_fusion mean|Cohen's d|
|---|---|---|---|---|
|0.001|0.0000|248|0.7032|1.801|
|0.01|0.0000|248|0.7603|2.165|
|0.05|0.0000|248|0.8138|2.560|
|0.1|0.0000|248|0.8379|2.751|

## 3b — z_mid Sensitivity

|z_mid|FP rate|Detect frame|Fault p_fusion mean|Cohen's d|
|---|---|---|---|---|
|2.0|0.0000|248|0.8564|2.910|
|3.0|0.0000|248|0.7603|2.165|
|4.0|0.0000|248|0.6913|1.738|

## 3b — temperature Sensitivity

|temperature|FP rate|Detect frame|Fault p_fusion mean|Cohen's d|
|---|---|---|---|---|
|0.5|0.0000|248|0.8382|2.596|
|1.0|0.0000|248|0.7603|2.165|
|2.0|0.0000|248|0.6787|1.739|

## 3c — Calibration Curve (P_FUSION_WARN=0.70, P_FUSION_FAULT=0.95)

Fraction of frames in each p_fusion bucket that were true faults (severity > 0.05):

|p_fusion bucket centre|Actual fault fraction|
|---|---|
|0.10|0.575|
|0.30|0.939|
|0.50|0.898|
|0.70|0.979|
|0.91|1.000|

> A well-calibrated model: bucket centre ≈ actual fraction.
> Deviation indicates over/under-confidence. Erring toward false alarms
> is preferred in this industrial context (asymmetric cost ratio 10:1).

---

# EMA Alpha Sweep Results

Protocol: healthy_frames=300, fault_frames=1200.

## Nominal fault (evolution_seconds=900)

|EMA alpha|Half-life (frames)|FP rate|Detect frame|Cohen's d|
|---|---|---|---|---|
|5e-05|13863|0.0000|241|2.810|
|0.0005|1386|0.0000|248|2.165|
|0.001|693|0.0000|257|1.906|
|0.005|139|0.0000|784|0.798|

## Contamination resistance (slow evolution_seconds=3600)

|EMA alpha|FP rate|Detect frame (contamination scenario)|
|---|---|---|
|5e-05|0.0000|968|
|0.0005|0.0000|1164|
|0.001|0.0000|-1|
|0.005|0.0000|-1|

> **Tradeoff**: lower alpha tracks baseline drift more slowly
> but resists contamination from misclassified fault frames.
> Higher alpha adapts faster but risks shifting the baseline toward fault.

---

