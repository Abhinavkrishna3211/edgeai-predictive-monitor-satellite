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

import numpy as np

from generate_and_play import (
    ideal_sine_stats,
    select_defect_frequency,
    _exp_cos_integral,
    ringdown_burst_stats,
    synthesize_ringdown_burst_train,
    synthesize_broadband_burst_train,
    broadband_stats_numeric,
    band_ratios_from_samples,
    _IMBALANCE_PRESETS,
)
from capture_and_compare import (
    is_locked,
    compute_noise_floor_db,
    band_ratios_from_wire_bins,
    expected_freq_hz,
)
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


def _direct_sample_stats(x):
    """Reference statistics computed directly from samples, independent of
    generate_and_play's own formulas -- used to numerically cross-check the
    closed-form ringdown_burst_stats()/broadband_stats_numeric() math."""
    x = np.asarray(x, dtype=np.float64)
    mean = x.mean()
    variance = np.mean((x - mean) ** 2)
    centered3 = np.mean((x - mean) ** 3)
    centered4 = np.mean((x - mean) ** 4)
    std = math.sqrt(variance)
    rms = math.sqrt(np.mean(x ** 2))
    peak = np.max(np.abs(x))
    return {
        "mean": mean,
        "rms": rms,
        "peak": peak,
        "crest_factor": peak / rms,
        "kurtosis_excess": centered4 / variance ** 2 - 3.0,
        "skewness": centered3 / std ** 3,
    }


class TestExpCosIntegral(unittest.TestCase):
    def test_zero_length_is_zero(self):
        self.assertEqual(_exp_cos_integral(a=5.0, b=3.0, length_s=0.0), 0.0)

    def test_pure_exponential_matches_closed_form(self):
        # b=0: integral_0^L exp(-a t) dt = (1 - exp(-a L)) / a
        a, length_s = 4.0, 0.5
        expected = (1.0 - math.exp(-a * length_s)) / a
        self.assertAlmostEqual(_exp_cos_integral(a, 0.0, length_s), expected, places=9)

    def test_infinite_length_limit_matches_laplace_transform(self):
        # As L -> inf (a>0), integral_0^inf exp(-at)cos(bt)dt = a/(a^2+b^2)
        a, b = 50.0, 30.0
        long_l = 5.0  # exp(-50*5) is negligible
        expected = a / (a * a + b * b)
        self.assertAlmostEqual(_exp_cos_integral(a, b, long_l), expected, places=9)

    def test_matches_numeric_quadrature(self):
        a, b, length_s = 7.0, 40.0, 0.3
        n = 200_000
        t = np.linspace(0, length_s, n)
        numeric = np.trapz(np.exp(-a * t) * np.cos(b * t), t)
        self.assertAlmostEqual(_exp_cos_integral(a, b, length_s), numeric, places=6)


class TestRingdownBurstStats(unittest.TestCase):
    """Cross-checks the exact closed-form ringdown_burst_stats() against
    direct-sample statistics of a densely-oversampled render, for the actual
    parameter presets shipped in the CLI (bearing default, both imbalance
    presets) plus one extra arbitrary point -- matching ideal_sine_stats()'s
    rigor bar via numerical verification rather than a second hand-derivation."""

    def _check(self, amplitude, carrier_hz, tau_s, burst_dur_s, period_s, rel_tol=5e-3):
        closed = ringdown_burst_stats(amplitude, carrier_hz, tau_s, burst_dur_s, period_s)
        x = synthesize_ringdown_burst_train(
            carrier_hz, tau_s, burst_dur_s, period_s,
            duration_s=period_s * 1000, amplitude=amplitude, sample_rate=200_000,
        )
        numeric = _direct_sample_stats(x)
        self.assertAlmostEqual(closed["peak"], numeric["peak"], places=5)
        for key in ("rms", "crest_factor", "kurtosis_excess"):
            self.assertAlmostEqual(
                closed[key] / numeric[key], 1.0, delta=rel_tol,
                msg=f"{key}: closed={closed[key]!r} numeric={numeric[key]!r}",
            )
        # skewness can be near zero (e.g. a near-symmetric envelope), where a
        # relative-ratio check blows up on tiny denominators -- use absolute
        # tolerance instead, matching how ideal_sine_stats' skewness=0.0 is
        # checked with assertEqual rather than a ratio.
        self.assertAlmostEqual(closed["skewness"], numeric["skewness"], delta=0.02,
                                msg=f"skewness: closed={closed['skewness']!r} numeric={numeric['skewness']!r}")

    def test_bearing_default_preset(self):
        # bearing=6205 @ 1500RPM/bpfo, resonance=3500Hz, tau=1.5ms -- from
        # cmd_bearing()'s CLI defaults.
        bf = BearingFreqs.from_rpm(1500.0, BEARING_6205)
        period_s = 1.0 / bf.bpfo
        tau_s = 1.5e-3
        burst_dur_s = min(4 * tau_s, 0.5 * period_s)
        self._check(0.8, 3500.0, tau_s, burst_dur_s, period_s)

    def test_imbalance_safe_preset(self):
        period_s = 1.0 / 25.0
        tau_s = _IMBALANCE_PRESETS["safe"]["tau_ms"] / 1000.0
        self._check(0.8, 150.0, tau_s, period_s, period_s)

    def test_imbalance_threshold_preset(self):
        period_s = 1.0 / 25.0
        tau_s = _IMBALANCE_PRESETS["threshold"]["tau_ms"] / 1000.0
        self._check(0.8, 150.0, tau_s, period_s, period_s)

    def test_arbitrary_parameter_point(self):
        self._check(amplitude=0.5, carrier_hz=1200.0, tau_s=0.004, burst_dur_s=0.012, period_s=0.05)

    def test_peak_is_exactly_amplitude(self):
        # By construction (cos carrier, envelope max at t=0) peak == amplitude
        # exactly, with no numeric search -- verify for several amplitudes.
        for amplitude in (0.1, 0.5, 0.8, 1.0):
            stats = ringdown_burst_stats(amplitude, 1000.0, 0.002, 0.008, 0.02)
            self.assertEqual(stats["peak"], amplitude)

    def test_crest_factor_equals_peak_over_rms(self):
        stats = ringdown_burst_stats(0.8, 3500.0, 0.0015, 0.006, 1.0 / 82.4)
        self.assertAlmostEqual(stats["crest_factor"], stats["peak"] / stats["rms"], places=9)

    def test_imbalance_threshold_preset_crosses_real_gate(self):
        # The whole point of the "threshold" preset: it must land inside the
        # real Mechanical Imbalance gate (crest>=5.0 and kurtosis<8.4), unlike
        # "safe" which deliberately stays well clear of it.
        period_s = 1.0 / 25.0
        tau_s = _IMBALANCE_PRESETS["threshold"]["tau_ms"] / 1000.0
        stats = ringdown_burst_stats(0.8, 150.0, tau_s, period_s, period_s)
        self.assertGreaterEqual(stats["crest_factor"], 5.0)
        self.assertLess(stats["kurtosis_excess"], 8.4)

    def test_imbalance_safe_preset_stays_clear_of_gate(self):
        period_s = 1.0 / 25.0
        tau_s = _IMBALANCE_PRESETS["safe"]["tau_ms"] / 1000.0
        stats = ringdown_burst_stats(0.8, 150.0, tau_s, period_s, period_s)
        self.assertLess(stats["crest_factor"], 5.0)


class TestBroadbandStatsNumeric(unittest.TestCase):
    """The Looseness broadband synthesis has no closed form (multi-carrier
    cross terms), so its ground truth is numeric by design -- these tests
    check internal convergence (stable across sample-count/duration) rather
    than against an independent closed-form reference."""

    _CARRIERS = (300.0, 800.0, 1500.0)
    _TAU_S = 0.004
    _BURST_S = 0.020
    _PERIOD_S = 1.0 / 30.0

    def test_converges_across_render_length(self):
        short = broadband_stats_numeric(
            self._CARRIERS, self._TAU_S, self._BURST_S, self._PERIOD_S,
            amplitude=0.8, sample_rate=48000, n_periods=300,
        )
        long = broadband_stats_numeric(
            self._CARRIERS, self._TAU_S, self._BURST_S, self._PERIOD_S,
            amplitude=0.8, sample_rate=48000, n_periods=3000,
        )
        for key in ("crest_factor", "kurtosis_excess"):
            self.assertAlmostEqual(short[key] / long[key], 1.0, delta=0.01,
                                    msg=f"{key} did not converge: short={short[key]!r} long={long[key]!r}")

    def test_peak_equals_amplitude(self):
        stats = broadband_stats_numeric(
            self._CARRIERS, self._TAU_S, self._BURST_S, self._PERIOD_S,
            amplitude=0.8, sample_rate=48000, n_periods=300,
        )
        self.assertAlmostEqual(stats["peak"], 0.8, places=5)

    def test_looseness_default_preset_crosses_real_gate(self):
        stats = broadband_stats_numeric(
            self._CARRIERS, self._TAU_S, self._BURST_S, self._PERIOD_S,
            amplitude=0.8, sample_rate=48000, n_periods=1000,
        )
        x = synthesize_broadband_burst_train(
            self._CARRIERS, self._TAU_S, self._BURST_S, self._PERIOD_S,
            duration_s=self._PERIOD_S * 800, amplitude=0.8, sample_rate=48000,
        )
        bands = band_ratios_from_samples(x, sample_rate=48000)
        self.assertGreaterEqual(stats["kurtosis_excess"], 6.0)
        self.assertLess(bands["hi_r"], 0.30)
        self.assertLess(bands["lo_r"], 0.55)
        self.assertGreater(bands["mid_r"], 0.20)


class TestBandRatiosFromSamples(unittest.TestCase):
    def test_low_carrier_concentrates_in_lo_band(self):
        # tau=18ms == burst_dur (fills the whole period, so this is a
        # continuous decay reset once per period, not a narrow isolated
        # pulse) -- spectral leakage puts real, non-negligible energy in the
        # mid band too. Measured lo_r~0.71 (imbalance "safe" preset); assert
        # dominance with margin, not near-total concentration.
        x = synthesize_ringdown_burst_train(150.0, 0.018, 0.04, 0.04, duration_s=0.04 * 500,
                                             amplitude=0.8, sample_rate=48000)
        bands = band_ratios_from_samples(x, sample_rate=48000)
        self.assertGreater(bands["lo_r"], 0.6)
        self.assertGreater(bands["lo_r"], bands["hi_r"])
        self.assertGreater(bands["lo_r"], bands["mid_r"])
        self.assertAlmostEqual(bands["hi_r"] + bands["lo_r"] + bands["mid_r"], 1.0, places=6)

    def test_high_carrier_concentrates_in_hi_band(self):
        x = synthesize_ringdown_burst_train(3500.0, 0.0015, 0.006, 1.0 / 82.4, duration_s=(1.0 / 82.4) * 500,
                                             amplitude=0.8, sample_rate=48000)
        bands = band_ratios_from_samples(x, sample_rate=48000)
        self.assertGreater(bands["hi_r"], 0.9)

    def test_bearing_default_crosses_real_gate(self):
        bf = BearingFreqs.from_rpm(1500.0, BEARING_6205)
        period_s = 1.0 / bf.bpfo
        tau_s = 1.5e-3
        burst_dur_s = min(4 * tau_s, 0.5 * period_s)
        x = synthesize_ringdown_burst_train(3500.0, tau_s, burst_dur_s, period_s,
                                             duration_s=period_s * 500, amplitude=0.8, sample_rate=48000)
        bands = band_ratios_from_samples(x, sample_rate=48000)
        stats = ringdown_burst_stats(0.8, 3500.0, tau_s, burst_dur_s, period_s)
        self.assertGreater(bands["hi_r"], 0.40)
        self.assertGreaterEqual(stats["kurtosis_excess"], 6.0)


def _samples_to_wire_bins_db(x, sample_rate=48000, n_bins=128):
    """Builds a device-shaped dB bin array (n_bins final buckets, one dB
    value each) the same way band_ratios_from_samples() pools a raw FFT
    internally, so its output can feed band_ratios_from_wire_bins() as a
    stand-in for a real captured ChannelSpectrum.bins tuple."""
    power_full = np.abs(np.fft.rfft(x)) ** 2
    nyquist = sample_rate / 2.0
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sample_rate)
    bucket_edges = np.linspace(0, nyquist, n_bins + 1)
    bucket_idx = np.clip(np.digitize(freqs, bucket_edges) - 1, 0, n_bins - 1)
    bucket_power = np.zeros(n_bins)
    for i in range(n_bins):
        bucket_power[i] = power_full[bucket_idx == i].sum()
    bucket_power = np.maximum(bucket_power, 1e-12)
    db = 10.0 * np.log10(bucket_power / bucket_power.max())
    return np.clip(db, -140.0, 0.0)


class TestBandRatiosFromWireBins(unittest.TestCase):
    def test_matches_band_ratios_from_samples_on_same_signal(self):
        # Same physical signal, two representations: raw-sample ground truth
        # (band_ratios_from_samples) vs. a device-shaped pre-pooled dB bin
        # array (band_ratios_from_wire_bins) -- must agree, since a captured
        # ChannelSpectrum's bins are exactly this pre-pooled representation.
        x = synthesize_ringdown_burst_train(150.0, 0.018, 0.04, 0.04, duration_s=0.04 * 500,
                                             amplitude=0.8, sample_rate=48000)
        expected = band_ratios_from_samples(x, sample_rate=48000)
        wire_bins = _samples_to_wire_bins_db(x, sample_rate=48000)
        actual = band_ratios_from_wire_bins(wire_bins, fs_hz=48000.0)
        for key in ("hi_r", "lo_r", "mid_r"):
            self.assertAlmostEqual(actual[key], expected[key], places=3)

    def test_all_energy_in_low_bin_gives_lo_r_near_one(self):
        bins_db = np.full(128, -140.0)
        bins_db[1] = 0.0  # bin 1: (1*Nyquist/128, 2*Nyquist/128] = (187.5, 375]Hz -- inside lo band
        bands = band_ratios_from_wire_bins(bins_db, fs_hz=48000.0)
        self.assertGreater(bands["lo_r"], 0.99)
        self.assertLess(bands["hi_r"], 0.01)
        self.assertLess(bands["mid_r"], 0.01)

    def test_all_energy_in_high_bin_gives_hi_r_near_one(self):
        bins_db = np.full(128, -140.0)
        bins_db[100] = 0.0  # 100*187.5Hz = 18750Hz -- inside hi band
        bands = band_ratios_from_wire_bins(bins_db, fs_hz=48000.0)
        self.assertGreater(bands["hi_r"], 0.99)

    def test_dc_bin_excluded_from_total(self):
        bins_db = np.full(128, -140.0)
        bins_db[0] = 0.0  # DC bin -- must not dominate the normalization
        bins_db[1] = 0.0  # tie it with a real low-band bin
        bands = band_ratios_from_wire_bins(bins_db, fs_hz=48000.0)
        # If DC were included in the total, lo_r would be ~0.5 (split with bin 0).
        self.assertGreater(bands["lo_r"], 0.99)

    def test_ratios_sum_to_one(self):
        rng = np.random.default_rng(1234)
        bins_db = rng.uniform(-140.0, 0.0, size=128)
        bands = band_ratios_from_wire_bins(bins_db, fs_hz=48000.0)
        self.assertAlmostEqual(bands["hi_r"] + bands["lo_r"] + bands["mid_r"], 1.0, places=6)


class TestExpectedFreqHz(unittest.TestCase):
    def test_tone_mode(self):
        self.assertEqual(expected_freq_hz({"mode": "tone", "freq_hz": 1000.0}), 1000.0)

    def test_fault_mode(self):
        self.assertEqual(expected_freq_hz({"mode": "fault", "sideband_hz": 82.4}), 82.4)

    def test_bearing_mode_uses_resonance_hz(self):
        self.assertEqual(expected_freq_hz({"mode": "bearing", "resonance_hz": 3500.0}), 3500.0)

    def test_imbalance_mode_uses_resonance_hz(self):
        self.assertEqual(expected_freq_hz({"mode": "imbalance", "resonance_hz": 150.0}), 150.0)

    def test_looseness_mode_has_no_single_expected_freq(self):
        self.assertIsNone(expected_freq_hz({"mode": "looseness", "carriers_hz": [300.0, 800.0, 1500.0]}))


if __name__ == "__main__":
    unittest.main()
