#!/usr/bin/env python3
"""
test_bench_signal_gen.py — Unit tests for generate_and_play.py's pure logic:
ideal_sine_stats() and select_defect_frequency(). No audio/hardware/MQTT
needed (numpy is generate_and_play.py's only hard import; sounddevice/scipy
stay lazy-imported inside play_samples()/save_wav()), same pattern as
tools/provisioning_label/test_generate_label.py.

Not wired into the repo's root tests/ pytest suite / CI -- this is bench
tooling, not something CI needs to install. Run manually:
    python -m pytest tools/bench_signal_gen/test_bench_signal_gen.py -v
    python tools/bench_signal_gen/test_bench_signal_gen.py
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from generate_and_play import ideal_sine_stats, select_defect_frequency
from capture_and_compare import is_locked, compute_noise_floor_db
from gateway.pipeline.bearing_math import BearingFreqs, COMMON_BEARINGS

BEARING_6205 = COMMON_BEARINGS["6205"]


class _FakeSpectrum:
    """Minimal stand-in for telemetry_frame.ChannelSpectrum -- is_locked()
    only touches .bins/.fs/.fft_size."""

    def __init__(self, bins, fs, fft_size):
        self.bins = bins
        self.fs = fs
        self.fft_size = fft_size


def _flat_noise_spectrum(n_bins=128, floor_db=-60.0):
    return [floor_db] * n_bins


class TestComputeNoiseFloorDb(unittest.TestCase):
    def test_median_ignores_excluded_and_resists_outliers(self):
        bins = [-60.0] * 10 + [0.0]  # one loud outlier bin
        floor = compute_noise_floor_db(bins, {10})
        self.assertEqual(floor, -60.0)


class TestIsLocked(unittest.TestCase):
    def test_locks_on_clean_peak_within_tolerance(self):
        # mic-like: fs=16000, fft_size=256 -> 62.5 Hz/bin, 128 bins
        bins = _flat_noise_spectrum(128, -60.0)
        bins[16] = -20.0  # bin 16 center = 16.5*62.5 = 1031.25 Hz
        spectrum = _FakeSpectrum(bins, fs=16000, fft_size=256)
        locked, info = is_locked(spectrum, expected_freq_hz=1000.0, tolerance_bins=1, min_snr_db=6.0)
        self.assertTrue(locked)
        self.assertAlmostEqual(info["peak_bin_freq_hz"], 1031.25)
        self.assertAlmostEqual(info["snr_db"], 40.0)

    def test_no_lock_when_peak_outside_tolerance(self):
        bins = _flat_noise_spectrum(128, -60.0)
        bins[50] = -20.0  # far from the 1000 Hz target
        spectrum = _FakeSpectrum(bins, fs=16000, fft_size=256)
        locked, info = is_locked(spectrum, expected_freq_hz=1000.0, tolerance_bins=1, min_snr_db=6.0)
        self.assertFalse(locked)

    def test_no_lock_when_snr_below_threshold(self):
        bins = _flat_noise_spectrum(128, -60.0)
        bins[16] = -55.0  # right bin, only 5 dB above floor
        spectrum = _FakeSpectrum(bins, fs=16000, fft_size=256)
        locked, info = is_locked(spectrum, expected_freq_hz=1000.0, tolerance_bins=1, min_snr_db=6.0)
        self.assertFalse(locked)
        self.assertAlmostEqual(info["snr_db"], 5.0)

    def test_lock_at_exact_tolerance_boundary(self):
        # bin 16 center = 1031.25 Hz, one bin width = 62.5 Hz -> boundary at 1093.75
        bins = _flat_noise_spectrum(128, -60.0)
        bins[16] = -20.0
        spectrum = _FakeSpectrum(bins, fs=16000, fft_size=256)
        locked, _ = is_locked(spectrum, expected_freq_hz=1093.75, tolerance_bins=1, min_snr_db=6.0)
        self.assertTrue(locked)

    def test_pure_noise_floor_never_locks(self):
        bins = _flat_noise_spectrum(128, -60.0)
        spectrum = _FakeSpectrum(bins, fs=16000, fft_size=256)
        locked, info = is_locked(spectrum, expected_freq_hz=1000.0, tolerance_bins=1, min_snr_db=6.0)
        self.assertFalse(locked)
        self.assertEqual(info["snr_db"], 0.0)

    def test_sub_bin_width_target_uses_presence_only_check(self):
        # accel-like: fs=25600, fft_size=256 -> 100 Hz/bin, target 60 Hz < bin width
        bins = _flat_noise_spectrum(128, -60.0)
        bins[0] = -20.0  # elevated energy in the lowest bin
        spectrum = _FakeSpectrum(bins, fs=25600, fft_size=256)
        locked, info = is_locked(spectrum, expected_freq_hz=60.0, tolerance_bins=1, min_snr_db=6.0)
        self.assertTrue(locked)
        self.assertAlmostEqual(info["snr_db"], 40.0)

    def test_sub_bin_width_target_no_presence_no_lock(self):
        bins = _flat_noise_spectrum(128, -60.0)
        bins[70] = -20.0  # loud, but not in the low 2 bins
        spectrum = _FakeSpectrum(bins, fs=25600, fft_size=256)
        locked, info = is_locked(spectrum, expected_freq_hz=60.0, tolerance_bins=1, min_snr_db=6.0)
        self.assertFalse(locked)

    def test_configurable_min_snr_db(self):
        bins = _flat_noise_spectrum(128, -60.0)
        bins[16] = -55.0  # 5 dB above floor
        spectrum = _FakeSpectrum(bins, fs=16000, fft_size=256)
        locked_strict, _ = is_locked(spectrum, expected_freq_hz=1000.0, tolerance_bins=1, min_snr_db=6.0)
        locked_loose, _ = is_locked(spectrum, expected_freq_hz=1000.0, tolerance_bins=1, min_snr_db=3.0)
        self.assertFalse(locked_strict)
        self.assertTrue(locked_loose)


class TestIdealSineStats(unittest.TestCase):
    def test_unit_amplitude(self):
        stats = ideal_sine_stats(1.0)
        self.assertAlmostEqual(stats["rms"], 1.0 / math.sqrt(2), places=9)
        self.assertEqual(stats["peak"], 1.0)
        self.assertAlmostEqual(stats["crest_factor"], math.sqrt(2), places=9)
        self.assertEqual(stats["kurtosis_excess"], -1.5)
        self.assertEqual(stats["skewness"], 0.0)

    def test_scales_linearly_with_amplitude(self):
        # rms and peak scale with amplitude; crest_factor/kurtosis/skewness
        # are shape-only and must stay constant regardless of amplitude.
        a = ideal_sine_stats(0.5)
        b = ideal_sine_stats(2.0)
        self.assertAlmostEqual(b["rms"] / a["rms"], 4.0, places=9)
        self.assertAlmostEqual(b["peak"] / a["peak"], 4.0, places=9)
        self.assertEqual(a["crest_factor"], b["crest_factor"])
        self.assertEqual(a["kurtosis_excess"], b["kurtosis_excess"])
        self.assertEqual(a["skewness"], b["skewness"])

    def test_crest_factor_is_peak_over_rms(self):
        # Internal consistency check: crest_factor as returned must equal
        # peak/rms for the same dict, not just a hardcoded constant that
        # happens to match today.
        stats = ideal_sine_stats(0.73)
        self.assertAlmostEqual(stats["crest_factor"], stats["peak"] / stats["rms"], places=9)

    def test_zero_amplitude(self):
        stats = ideal_sine_stats(0.0)
        self.assertEqual(stats["rms"], 0.0)
        self.assertEqual(stats["peak"], 0.0)
        # crest_factor is shape-derived (peak/rms's limit), stays sqrt(2)
        # even though 0/0 would be undefined computed the naive way.
        self.assertAlmostEqual(stats["crest_factor"], math.sqrt(2), places=9)


class TestSelectDefectFrequency(unittest.TestCase):
    def setUp(self):
        self.bf = BearingFreqs.from_rpm(1500.0, BEARING_6205)

    def test_bpfo(self):
        self.assertEqual(select_defect_frequency(self.bf, "bpfo"), self.bf.bpfo)

    def test_bpfi(self):
        self.assertEqual(select_defect_frequency(self.bf, "bpfi"), self.bf.bpfi)

    def test_bsf(self):
        self.assertEqual(select_defect_frequency(self.bf, "bsf"), self.bf.bsf)

    def test_ftf(self):
        self.assertEqual(select_defect_frequency(self.bf, "ftf"), self.bf.ftf)

    def test_unknown_defect_raises_value_error(self):
        with self.assertRaises(ValueError):
            select_defect_frequency(self.bf, "not-a-defect")


if __name__ == "__main__":
    unittest.main()
