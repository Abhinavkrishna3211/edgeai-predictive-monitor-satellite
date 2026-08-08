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
from gateway.pipeline.bearing_math import BearingFreqs, COMMON_BEARINGS

BEARING_6205 = COMMON_BEARINGS["6205"]


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
