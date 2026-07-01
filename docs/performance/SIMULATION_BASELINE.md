# Simulation Baseline — EPM Detection Pipeline

Three-seed average, fault_type=outer, evolution_seconds=1800.0, healthy_frames=300, fault_frames=3700.

## Distribution Summary

|Metric|Seed 1|Seed 2|Seed 3|Average|
|---|---|---|---|---|
|Cohen's d (p_fusion)|2.531|2.552|2.559|2.547|
|Healthy p_fusion mean|0.0050|0.0072|0.0142|0.0088|
|Fault p_fusion mean|0.8441|0.8466|0.8475|0.8461|
|Healthy HST score mean|0.4624|0.4586|0.4660|0.4623|
|Fault HST score mean|0.8933|0.8882|0.8899|0.8905|
|False positives (healthy phase)|0|0|0|0|
|Detection frame (1st WARN)|512|512|512|512|
|Fault recall (WARN+FAULT / fault_frames)|0.862|0.862|0.862|0.862|
|CPU µs/frame|5109.3|5228.5|5063.0|5133.6|
|Peak RSS delta (MB)|428.62|405.55|399.77|411.31|

## RUL Accuracy

|Checkpoint|Seed 1 error %|Seed 2 error %|Seed 3 error %|Average %|
|---|---|---|---|---|
|25% through fault|4945.2|4841.5|4789.4|4858.7|
|50% through fault|1561.6|1568.7|1559.4|1563.2|
|75% through fault|1082.6|1073.7|1081.5|1079.3|

## Calibration Curve (p_fusion buckets vs actual fault fraction)

Note: 3-seed average. A perfectly calibrated model would show bucket centre ≈ actual fraction.

|p_fusion bucket centre|Actual fault fraction (avg seed 1)|
|---|---|
|0.10|0.559|
|0.30|0.906|
|0.50|0.939|
|0.70|1.000|
|0.91|0.999|

> **Statistical note**: 3 seeds is sufficient for directional findings but insufficient
> for production sign-off (recommend 10+ seeds for that).

---

## Post-Sweep Combined Config — 2026-07-01

Config: **n_trees=10, z_mid=2.0, ema_alpha=5e-05** (all three Phase 2-4 recommendations combined).
Three-seed average, fault_type=outer, evolution_seconds=1800.0, healthy_frames=300, fault_frames=3700.

> This section supersedes the individual-sweep single-change results for production use.
> The original Phase 1 numbers above are retained for historical reference.

|Metric|Seed 1|Seed 2|Seed 3|Average|vs Phase-1 baseline|
|---|---|---|---|---|---|
|Cohen's d (p_fusion)|3.586|3.595|3.994|3.725|+1.178|
|Healthy p_fusion mean|0.0472|0.0320|0.0466|0.0420|—|
|Fault p_fusion mean|0.9177|0.9171|0.9342|0.9230|—|
|False positives (healthy phase)|0|0|0|0|PASS|
|Detection frame (1st WARN)|482|482|482|482|—|
|Fault recall (WARN+FAULT / fault_frames)|0.870|0.870|0.870|0.870|—|
|CPU us/frame|1473.9|1473.9|1417.3|1455.0|—|

|Checkpoint|Seed 1 error %|Seed 2 error %|Seed 3 error %|Average %|
|---|---|---|---|---|
|25% through fault|4945.2|4841.5|4789.4|4858.7|
|50% through fault|1561.6|1568.7|1559.4|1563.2|
|75% through fault|1082.6|1073.7|1081.5|1079.3|

> Regression check: cohen_d 3.725 vs 2.547 baseline -> PASS (no regression). fp_count=0 -> PASS.
