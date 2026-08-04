#include "dsp/scalar_stats.h"

#include <math.h>

float epm_dsp_rms_from_sum_sq(float sum_sq, int n)
{
    return sqrtf(sum_sq / (float)n);
}

float epm_dsp_peak_abs(const float *x, int n)
{
    float peak = 0.0f;
    for (int i = 0; i < n; i++) {
        float a = fabsf(x[i]);
        if (a > peak) peak = a;
    }
    return peak;
}

float epm_dsp_crest_factor(float peak, float rms)
{
    return (rms > 1e-8f) ? (peak / rms) : 0.0f;
}

float epm_dsp_kurtosis_from_sums(float sum_sq, float sum4, int n, float fallback)
{
    float var = sum_sq / (float)n;
    if (var > 1e-12f) {
        return (sum4 / (float)n) / (var * var) - 3.0f; /* excess/Fisher, ADR-018 */
    }
    return fallback;
}
