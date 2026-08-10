#!/usr/bin/env python3
"""
test_bearing_corroboration.py — Unit tests for
gateway/pipeline/bearing_corroboration.py (ADR-038).

corroborate_bearing_fault() is additive, out-of-band evidence for an
already-triggered "Bearing Fault" classification -- it never influences
_classify_fault_type() itself. These tests exercise it directly (not through
the live pipeline), same fixture (bearing '6205' @ 1500 RPM) that
test_bearing_math.py and test_envelope_bearing_markers.py already use.

Run with:
    python -m pytest tests/pipeline/test_bearing_corroboration.py -v
    python tests/pipeline/test_bearing_corroboration.py
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.pipeline.bearing_corroboration import corroborate_bearing_fault
from gateway.pipeline.bearing_math import BearingFreqs, COMMON_BEARINGS

MIC_FS_HZ = 16000.0
N_BINS = 2048   # hz_per_bin = 16000/2/2048 = 3.90625 Hz

BEARING_6205 = COMMON_BEARINGS['6205']
RPM_1500 = 1500.0
SHAFT_HZ_1500 = RPM_1500 / 60.0  # 25.0

NOISE_FLOOR_DB = -100.0
PEAK_DB = -10.0


def _mic_fft_with_peak_at(freq_hz: float, n_bins: int = N_BINS, fs_hz: float = MIC_FS_HZ) -> np.ndarray:
    """A dBFS mic-FFT array with the noise floor everywhere except a single
    dominant tone at freq_hz (nearest bin), matching what a real bearing
    defect's dominant resonance line looks like."""
    hz_per_bin = fs_hz / 2.0 / n_bins
    bins = np.full(n_bins, NOISE_FLOOR_DB, dtype=np.float64)
    k = int(round(freq_hz / hz_per_bin))
    k = max(1, min(n_bins - 1, k))   # never DC
    bins[k] = PEAK_DB
    return bins


class TestCorroborateBearingFault(unittest.TestCase):
    def setUp(self):
        self.bf = BearingFreqs.from_rpm(RPM_1500, BEARING_6205)

    def test_peak_at_bpfo_is_corroborated(self):
        fft = _mic_fft_with_peak_at(self.bf.bpfo)
        result = corroborate_bearing_fault(
            "Bearing Fault — Early", fft, MIC_FS_HZ, SHAFT_HZ_1500, BEARING_6205)
        self.assertIsNotNone(result)
        self.assertTrue(result['corroborated'])
        self.assertEqual(result['matched_marker'], 'BPFO')
        self.assertAlmostEqual(result['peak_hz'], self.bf.bpfo, delta=4.0)

    def test_peak_at_bpfi_is_corroborated_advanced_label(self):
        fft = _mic_fft_with_peak_at(self.bf.bpfi)
        result = corroborate_bearing_fault(
            "Bearing Fault — Advanced", fft, MIC_FS_HZ, SHAFT_HZ_1500, BEARING_6205)
        self.assertIsNotNone(result)
        self.assertTrue(result['corroborated'])
        self.assertEqual(result['matched_marker'], 'BPFI')

    def test_peak_far_from_any_marker_is_not_corroborated(self):
        # Nyquist is 8000 Hz; pick a frequency far from every 6205@1500rpm
        # marker (BPFO=82.4, BPFI=142.6, BSF=43.4, FTF=9.16, shaft=25,
        # 2x versions) -- 5000 Hz clears all of them by a wide margin.
        fft = _mic_fft_with_peak_at(5000.0)
        result = corroborate_bearing_fault(
            "Bearing Fault — Early", fft, MIC_FS_HZ, SHAFT_HZ_1500, BEARING_6205)
        self.assertIsNotNone(result)
        self.assertFalse(result['corroborated'])
        self.assertIsNone(result['matched_marker'])
        self.assertAlmostEqual(result['peak_hz'], 5000.0, delta=4.0)

    def test_normal_label_returns_none(self):
        fft = _mic_fft_with_peak_at(self.bf.bpfo)
        result = corroborate_bearing_fault(
            "Normal", fft, MIC_FS_HZ, SHAFT_HZ_1500, BEARING_6205)
        self.assertIsNone(result)

    def test_non_bearing_fault_label_returns_none(self):
        fft = _mic_fft_with_peak_at(self.bf.bpfo)
        result = corroborate_bearing_fault(
            "Mechanical Imbalance", fft, MIC_FS_HZ, SHAFT_HZ_1500, BEARING_6205)
        self.assertIsNone(result)

    def test_missing_shaft_hz_returns_none(self):
        fft = _mic_fft_with_peak_at(self.bf.bpfo)
        result = corroborate_bearing_fault(
            "Bearing Fault — Early", fft, MIC_FS_HZ, None, BEARING_6205)
        self.assertIsNone(result)

    def test_missing_geom_returns_none(self):
        fft = _mic_fft_with_peak_at(self.bf.bpfo)
        result = corroborate_bearing_fault(
            "Bearing Fault — Early", fft, MIC_FS_HZ, SHAFT_HZ_1500, None)
        self.assertIsNone(result)

    def test_missing_mic_fft_returns_none(self):
        result = corroborate_bearing_fault(
            "Bearing Fault — Early", None, MIC_FS_HZ, SHAFT_HZ_1500, BEARING_6205)
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
