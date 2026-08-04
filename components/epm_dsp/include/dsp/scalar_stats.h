/*
 * scalar_stats.h — Pure-math time-domain block statistics.
 *
 * The SIMD reductions feeding these (dsps_dotprod_f32, dsps_mul_f32) stay in
 * the task file; this covers only the pure-C ratio/peak math built on top of
 * their outputs.
 */

#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/** RMS from a precomputed sum-of-squares (e.g. dsps_dotprod_f32(x,x)). */
float epm_dsp_rms_from_sum_sq(float sum_sq, int n);

/** Peak absolute value over x[0..n). */
float epm_dsp_peak_abs(const float *x, int n);

/** Crest factor = peak/rms, or 0 if rms is too small to divide by. */
float epm_dsp_crest_factor(float peak, float rms);

/**
 * Kurtosis = (sum4/n) / (sum_sq/n)^2, or `fallback` if the variance is too
 * small to divide by (matches the mic_task.c convention of leaving the
 * previous value in place rather than dividing by ~0).
 */
float epm_dsp_kurtosis_from_sums(float sum_sq, float sum4, int n, float fallback);

#ifdef __cplusplus
}
#endif
