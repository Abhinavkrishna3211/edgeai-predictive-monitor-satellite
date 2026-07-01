# Numerical Stability Audit

## 5a — dBFS Floor Analysis

Minimum non-zero power value across 2000 frames (healthy + progressive fault): **1.000e-12**

Minimum fft_db value seen: **-120.0 dBFS**

`to_dbfs` uses `20*log10(|pwr| + 1e-6)`. The minimum observed power is 1.000e-12, which is 0.0× larger than the floor (1e-6). This means the floor is never actually hit in normal operation — **1e-6 floor is safe but conservative by several orders of magnitude.**

## 5b — float32 vs float64 Kalman Precision

Ran 5000 Kalman update steps with progressive fault kurtosis.

Mean absolute divergence in λ_hat over last 100 steps: **0.000e+00**

The ExponentialRUL Kalman filter uses float64 internally (numpy default), not float32. Divergence is negligible — no precision issue found.

## 5c — Edge-Case NaN/Inf Propagation

|Input scenario|Pipeline output status|
|---|---|
|all_zero_fft|OK|
|single_spike_fft|OK|
|clipped_fft|OK|
|neg_inf_fft|OK|

> "OK" = no NaN or Inf reached alert/p_fusion/hst/hb outputs.
> The -140 dBFS clip in band_ratios() and spectral_centroid() prevents
> -inf propagation from zero-power inputs.