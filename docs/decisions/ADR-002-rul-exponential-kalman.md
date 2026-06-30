---
id: ADR-002
title: Remaining Useful Life (RUL) estimation via exponential model + Kalman filter
status: accepted
date: 2026-06-30
deciders: Abhinav Krishna N
---

## Context

The gateway must estimate when a monitored bearing will cross a failure threshold based on the kurtosis time series. Three degradation model classes were evaluated. The key engineering constraint is that the kurtosis growth curve follows Paris Law crack propagation kinetics, making the model choice a physics-grounded decision rather than a data-fitting exercise.

## Options considered

### Option A: Linear regression
**Evidence:** Least-squares linear fit to kurtosis history. Simple; O(N) update.
**Pros:** No hyperparameters; trivial to implement.
**Cons:** Kurtosis growth in bearing defects follows exponential crack propagation, not linear. Linear fit to an exponential signal underestimates the time to failure early in degradation and overestimates it as fault accelerates. Numeric example: at kurtosis K=5 (early defect), a linear model predicts 200 frames to K=40 (failure). The actual exponential trajectory (λ=0.015) reaches K=40 in 137 frames — a 46% underestimate of urgency. This could delay a maintenance action by 63 frames (~28 s), during which bearing damage is accelerating.

### Option B: Exponential curve fit (static, no uncertainty)
**Evidence:** Model K(t) = K₀ · exp(λ·t). RUL = ln(K_fail/K(t)) / λ.
K_fail = 40 per ISO 13381-1 (bearing fault: kurtosis > 6; critical: > 40).
**Pros:** Physically correct (Paris Law: da/dN = C·(ΔK)^m → exponential growth). Better long-range prediction than linear.
**Cons:** No uncertainty quantification; a single noisy measurement can flip the prediction wildly. No mechanism to handle measurement noise inherent in acoustic kurtosis.

### Option C: Exponential fit + Kalman filter on log-space state
**Evidence:** Lei et al., "Machinery Health Prognostics: A Systematic Review from Data Acquisition to RUL Prediction," Mech. Sys. Signal Process. 104 (2018), pp. 799–834.

State vector: x = [ln(K₀), λ]ᵀ. Transition: x_{t+1} = x_t + w (random walk in degradation rate). Measurement: z_t = ln(K_t) + v.
K_fail = 40 (ISO 13381-1). RUL = (ln(K_fail) - ln(K₀)) / λ.

The log-linearisation converts the exponential observation model into a linear one, making a standard Kalman filter applicable. Process noise Q tunes the model's responsiveness to accelerating degradation; measurement noise R is calibrated from the kurtosis variance during the HST warm-up window (first 250 frames).

**Pros:** Optimal (minimum variance) estimator for the linear-Gaussian case. Provides confidence intervals for RUL. Adapts to unexpected acceleration in degradation rate via the random-walk transition model.
**Cons:** Requires two tuning parameters (Q, R). Log-linearisation is only exact if K > 0 always (guaranteed by kurtosis being a ratio of moments).

## Decision
**Chosen: Option C — Exponential fit + Kalman filter**

**Justification:** The exponential model is physically correct per Paris Law crack propagation (da/dN = C·(ΔK)^m). The Kalman filter on the log-space state [ln(K₀), λ] provides optimal noise rejection and RUL uncertainty bounds. Lei et al. (2018) validates this class of approach across 14 industrial datasets. K_fail=40 is the ISO 13381-1 bearing fault threshold.

RUL formula: RUL = ln(K_fail / K_now) / λ_hat

where K_now is the current kurtosis and λ_hat is the Kalman-filtered growth rate.

## Consequences
**Positive:**
- RUL estimates have confidence intervals, enabling risk-based maintenance scheduling
- Kalman filter noise rejection prevents false urgency from transient kurtosis spikes
- Exponential model matches the physical crack propagation mechanism

**Negative / trade-offs:**
- Q and R require calibration; mis-tuning R too low makes the filter slow to respond to accelerating degradation
- Assumes the degradation mode is a single dominant crack; does not model multi-fault scenarios

**Metrics to watch:**
- RUL prediction error at fault confirmation (target: < 15% of actual remaining life)
- Kalman gain convergence time (expected: < 50 frames for λ to stabilise)
- P(K > K_fail | λ_hat, P_cov) exceedance probability as an early warning metric

## Validation
`mic_tools/fault_models.py` — `make_severity_fn()` implements the exponential severity ramp K(t) = K₀·exp(λ·t). `test_simulator.py::test_exponential_kurtosis_growth` verifies the ramp crosses expected thresholds within the correct frame window.
