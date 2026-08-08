#!/usr/bin/env python3
"""
test_autoencoder_fs_dependency.py — Regression guard for
gateway/pipeline/autoencoder.py's spectral feature window.

make_feature_vector() compresses the mic FFT into 32 bands covering
0-SPEC_FEATURE_MAX_HZ (see that module's docstring). Which raw FFT bin
reaches SPEC_FEATURE_MAX_HZ depends on MIC_FS_HZ: the bug this file guards
against is a *hardcoded* bin-count window that only happened to be correct
at MIC_FS_HZ == 16000 — if MIC_FS_HZ ever changes without the window
following it, the compressed bands silently start covering the wrong real
Hz range (e.g. a bin that used to sit at 6 kHz now sits at 12 kHz once the
sample rate doubles, but a hardcoded window would still fold it into the
"0-8 kHz" feature as if nothing had changed).

Setup mirrors tests/pipeline/test_alerting.py: recv_verify.py must be
importable for make_feature_vector()'s lazy `import recv_verify as _rv`
(via _mic_fs_hz()) to reach the live MIC_FS_HZ global.

Run with:
    python -m pytest tests/pipeline/test_autoencoder_fs_dependency.py -v
    python tests/pipeline/test_autoencoder_fs_dependency.py
"""

import os
import sys
import unittest

import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'mic_tools'))

import matplotlib
matplotlib.use('Agg')  # headless — recv_verify imports matplotlib.pyplot at module level

import recv_verify as rv
from gateway.pipeline.autoencoder import (
    make_feature_vector, SPEC_BANDS, SPEC_FEATURE_MAX_HZ,
)


def _peaked_fft(n, peak_bin, peak_db=0.0, floor_db=-140.0):
    """n-bin dBFS array, quiet everywhere except one strong bin."""
    arr = np.full(n, floor_db, dtype=np.float32)
    arr[peak_bin] = peak_db
    return arr


def _band_of_bin(bin_idx, n_used):
    """Which of the SPEC_BANDS compressed bands a raw bin index falls into,
    given n_used total bins were folded into SPEC_BANDS bands (same
    bins_per_band = n_used // SPEC_BANDS math make_feature_vector uses)."""
    bins_per_band = n_used // SPEC_BANDS
    return bin_idx // bins_per_band


class TestFeatureWindowTracksSampleRate(unittest.TestCase):
    """The core regression guard: a hardcoded bin-count window would keep
    reading the same raw bins regardless of MIC_FS_HZ. A correct,
    Fs-derived window must stop including bins that have moved beyond
    SPEC_FEATURE_MAX_HZ once the sample rate increases."""

    def setUp(self):
        self._orig_fs = rv.MIC_FS_HZ

    def tearDown(self):
        rv.MIC_FS_HZ = self._orig_fs  # module-level global — don't leak into other tests

    def test_peak_beyond_window_is_excluded_after_fs_increase(self):
        n_total = 128
        peak_bin = 100   # only meaningful relative to MIC_FS_HZ, see below

        # At MIC_FS_HZ=16000: hz_per=62.5, SPEC_FEATURE_MAX_HZ=8000 -> whole
        # 128-bin array is inside the window (n used == n_total), so the
        # peak at bin 100 (~6.25 kHz) lands squarely inside band 25.
        rv.MIC_FS_HZ = 16000
        hz_per_16k = rv.MIC_FS_HZ / 2.0 / n_total
        self.assertLess(peak_bin * hz_per_16k, SPEC_FEATURE_MAX_HZ,
                        "test setup assumption: peak must be inside the 16kHz window")

        fft_arr = _peaked_fft(n_total, peak_bin)
        frame = dict(mic_rms=0.1, mic_crest=3.0, mic_kurtosis=3.2,
                    imu_rms=0.05, imu_crest=2.0, high_band_ratio=0.2,
                    z_score=1.0, mic_fft=fft_arr)
        bands_16k = make_feature_vector(frame)[9:41]
        hot_band = _band_of_bin(peak_bin, n_total)
        self.assertGreater(bands_16k[hot_band], bands_16k.mean() + 0.3,
                            "peak should be visible in its band at MIC_FS_HZ=16000")

        # At MIC_FS_HZ=32000, the SAME raw bin now represents twice the
        # frequency (~12.5 kHz) -- beyond SPEC_FEATURE_MAX_HZ. A correct
        # implementation narrows the used window (n < n_total) so bin 100
        # is no longer folded into any band at all.
        rv.MIC_FS_HZ = 32000
        hz_per_32k = rv.MIC_FS_HZ / 2.0 / n_total
        self.assertGreaterEqual(peak_bin * hz_per_32k, SPEC_FEATURE_MAX_HZ,
                                "test setup assumption: peak must fall outside the 32kHz window")

        bands_32k = make_feature_vector(frame)[9:41]
        self.assertLess(
            bands_32k[hot_band], bands_16k[hot_band] - 0.3,
            "a hardcoded (Fs-independent) feature window would still show the "
            "peak here even though it no longer represents a real frequency "
            "inside SPEC_FEATURE_MAX_HZ -- the window must shrink with MIC_FS_HZ")
        # With the peak's bin excluded entirely, every remaining band should
        # be reading pure floor noise -- confirms the window actually
        # narrowed rather than just attenuating the one band.
        self.assertLess(bands_32k.max(), bands_16k.mean() + 0.3)


class TestFeatureWindowBehaviorPreservedAtDefaultFs(unittest.TestCase):
    """At today's MIC_FS_HZ=16000 default, the dynamically-derived window
    must reproduce exactly what the old hardcoded `min(len, 512)` produced
    for every realistic bin count -- this refactor must not change
    production behavior."""

    def setUp(self):
        self._orig_fs = rv.MIC_FS_HZ
        rv.MIC_FS_HZ = 16000

    def tearDown(self):
        rv.MIC_FS_HZ = self._orig_fs

    @staticmethod
    def _old_bands(fft_arr):
        n = min(len(fft_arr), 512)
        if n < SPEC_BANDS:
            return np.zeros(SPEC_BANDS, dtype=np.float32)
        p = fft_arr[:n]
        power = 10.0 ** (np.clip(p, -120.0, 0.0) / 10.0)
        bins_per_band = n // SPEC_BANDS
        bands = power[:bins_per_band * SPEC_BANDS].reshape(
            SPEC_BANDS, bins_per_band).mean(axis=1)
        bands_db = 10.0 * np.log10(bands + 1e-12)
        return np.clip((bands_db + 60.0) / 60.0, -1.0, 1.0).astype(np.float32)

    def test_matches_legacy_hardcoded_window_for_realistic_bin_counts(self):
        rng = np.random.default_rng(1234)
        # 128 = today's real wire bin count (ADR-020 pooled spectra);
        # 512 = legacy native mic_bins (FFT_MIC_N/2); others exercise
        # divisibility edge cases.
        for n_bins in (128, 512, 100, 137, 33):
            fft_arr = rng.uniform(-100.0, -10.0, size=n_bins).astype(np.float32)
            frame = dict(mic_rms=0.1, mic_crest=3.0, mic_kurtosis=3.2,
                        imu_rms=0.05, imu_crest=2.0, high_band_ratio=0.2,
                        z_score=1.0, mic_fft=fft_arr)
            got = make_feature_vector(frame)[9:41]
            expected = self._old_bands(fft_arr)
            np.testing.assert_allclose(
                got, expected, atol=1e-6,
                err_msg=f"band mismatch vs legacy hardcoded window at n_bins={n_bins}")

    def test_missing_fft_still_zero_fills(self):
        frame = dict(mic_rms=0.1, mic_crest=3.0, mic_kurtosis=3.2,
                    imu_rms=0.05, imu_crest=2.0, high_band_ratio=0.2, z_score=1.0)
        feat = make_feature_vector(frame)
        np.testing.assert_array_equal(feat[9:41], np.zeros(32, dtype=np.float32))


if __name__ == '__main__':
    unittest.main()
