---
id: ADR-004
title: ADWIN adaptive windowing for concept drift detection
status: accepted
date: 2026-06-30
deciders: Abhinav Krishna N
---

## Context

The HST anomaly detector learns the machine's normal signature continuously. If the machine's operating conditions change permanently (new load profile, ambient temperature shift, component replacement), the historical baseline becomes invalid and the detector will generate chronic false positives. Three approaches were evaluated for detecting and responding to this concept drift.

## Options considered

### Option A: Scheduled retraining
**Evidence:** Reset HST model every N frames (e.g., N=1000, ~7.6 minutes at 2.2 fps).
**Pros:** Simple; guaranteed to eventually adapt to any drift.
**Cons:** Blind to when drift actually occurs — retrains during quiet periods (wasteful) and may not retrain quickly enough after a sudden shift. Does not distinguish between real drift and a transient fault: resetting during a fault would erase evidence of the fault and call it "new normal."

### Option B: EMA deviation threshold
**Evidence:** Trigger retrain when |score - EMA(score)| > threshold for K consecutive frames.
**Pros:** Responsive to sudden shifts.
**Cons:** Threshold requires manual calibration per machine. EMA lags real drift (exponential smoothing introduces a delay proportional to 1/α). No formal statistical guarantee on false drift alarm rate.

### Option C: ADWIN (ADaptive WINdowing)
**Evidence:** Bifet & Gavaldà, "Learning from Time-Changing Data with Adaptive Windowing," SIAM SDM 2007.

ADWIN maintains a variable-length window of recent observations. It detects drift when the mean in any two sub-windows differs by more than:
    ε = sqrt( 1/(2m) · ln(4n/δ) )
where m = harmonic mean of the two sub-window sizes, n = total observations, δ = confidence parameter = 0.002.

Memory: O(log n) — window is compressed as it grows, so long-term operation never causes memory growth.
False alarm rate: bounded by δ = 0.002 per detected change point, i.e., one false drift alarm every ~500 genuine detections on average.

EPM-specific rule: ADWIN is only updated with scores from frames in OK state (not WARN/FAULT). This prevents the model from learning fault signatures as "new normal" and calling them drift. The `check_drift()` method in `OnlineDetector` enforces this.

**Pros:** O(log n) memory; formal statistical guarantee on false alarm rate; no threshold tuning needed; state-preserving (`save()`/`load()` includes ADWIN state).
**Cons:** ADWIN can take several hundred frames to confidently detect slow gradual drift (the window must accumulate enough statistical power); not suitable for detecting single-frame step changes.

## Decision
**Chosen: Option C — ADWIN**

**Justification:** The formal guarantee ε = sqrt(1/(2m) · ln(4n/δ)) with δ=0.002 provides a statistically bounded false alarm rate (0.002 per detection, or approximately 1 false drift alarm per 500 genuine OK-state observations). O(log n) memory prevents unbounded growth during continuous deployment. The OK-only update rule is a critical correctness property: without it, a sustained fault could be learned as new normal and then treated as "drift" when the fault clears, causing an incorrect baseline refresh that forgets the evidence of the fault period.

## Consequences
**Positive:**
- Automatic adaptation to long-term operating condition changes without manual intervention
- O(log n) memory — safe for indefinite deployment
- False drift alarm rate formally bounded at δ=0.002

**Negative / trade-offs:**
- ADWIN detection latency for slow drift (λ << 0.01/frame) can exceed 500 frames (~3.8 minutes)
- OK-only update rule means drift detection is suspended during prolonged fault conditions; if a machine runs in WARN/FAULT state for hours, drift accumulates invisibly

**Metrics to watch:**
- `drift_detected` events in alert log (BASELINE_REFRESH rows)
- Time between consecutive drift detections (target: > 10 minutes on a stable machine)
- HST anomaly score mean before vs after a baseline refresh (should drop toward 0.5 if drift was genuine)

## Validation
`mic_tools/test_drift.py` — 7 tests: drift detection (artificial score stream with step shift), no-false-drift on stable signal, refresh resets ADWIN + Welford + EMA, OK-only policy (WARN/FAULT scores do not update ADWIN), save/load preserves drift state. All 7 pass.

## Open Issue: delta not derived from actual score variance (WP-05, 2026-06-30)

**Status**: Under investigation — deferred to KNOWN_ISSUES.md.

`delta=0.002` was taken from the river library documentation without derivation from this
system's actual HST score distribution. Phase 1 baseline (3 seeds) shows healthy HST scores
converge to mean≈0.5 with sigma≈0.03–0.05. The theoretically appropriate delta for a
desired false-alarm period of 2000 OK-state frames is:

    delta_theory = sigma_score^2 / n_false_alarm = 0.03^2 / 2000 = 4.5e-7

This is approximately 4000× smaller than the current 0.002, suggesting the current value
gives a much higher theoretical false-alarm rate than intended. However, ADWIN's actual
false-alarm rate depends on the autocorrelation structure of the score sequence, not just
the variance, so empirical measurement is required before changing delta.

**Resolution path**: Instrument `OnlineDetector` to count ADWIN detection events during
10,000 healthy frames (no induced drift). If detections per 1000 frames > 1, lower delta.
Document the measured false-alarm rate and the chosen delta derivation in this ADR.
See `docs/performance/KNOWN_ISSUES.md#wp-05` for details.
