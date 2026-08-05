#!/usr/bin/env python3
"""
test_bearing_math.py — Unit tests for mic_tools/bearing_math.py.

bearing_math.py had zero test coverage prior to this file (unlike
rul_estimator.py, bayesian_fusion.py, and adaptive_baseline.py, which already
have test_rul.py/test_fusion.py/test_baseline.py). It is pure math (stdlib
`math` only, no I/O, no hardware) so these tests need no mocking/fixtures.

Fixture: bearing '6205' (n=9, D=38.5mm, d=10.3mm, alpha=0deg) at 1500 RPM --
the same example used in bearing_math.py's own module docstring and
recv_verify.py's `--shaft-rpm 1500 --bearing 6205` CLI example.

These tests must PASS against the current, unmodified bearing_math.py -- no
known bug in this module (unlike the firmware side's Hann-normalisation
issue).

Run with:
    python -m pytest mic_tools/test_bearing_math.py -v
    python mic_tools/test_bearing_math.py
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.pipeline.bearing_math import BearingGeometry, BearingFreqs, COMMON_BEARINGS, parse_bearing_arg

MIC_FS_HZ = 16000
IMU_FS_HZ = 25600

BEARING_6205 = COMMON_BEARINGS['6205']
RPM_1500 = 1500.0
SHAFT_HZ_1500 = RPM_1500 / 60.0  # 25.0

# Independently computed reference values for 6205 @ 1500 RPM (shaft_hz=25.0,
# r = (10.3/38.5)*cos(0) = 0.26753246753246757):
BPFO_REF = 82.40259740259741
BPFI_REF = 142.5974025974026
BSF_REF = 43.37914512671794
FTF_REF = 9.155844155844155


class TestFrequencyFormulas(unittest.TestCase):
    def test_bpfo_bpfi_bsf_ftf_6205_1500rpm(self):
        bf = BearingFreqs.from_rpm(RPM_1500, BEARING_6205)
        self.assertAlmostEqual(bf.bpfo, BPFO_REF, places=6)
        self.assertAlmostEqual(bf.bpfi, BPFI_REF, places=6)
        self.assertAlmostEqual(bf.bsf, BSF_REF, places=6)
        self.assertAlmostEqual(bf.ftf, FTF_REF, places=6)

    def test_from_rpm_matches_from_shaft_hz(self):
        by_rpm = BearingFreqs.from_rpm(RPM_1500, BEARING_6205)
        by_hz = BearingFreqs.from_shaft_hz(SHAFT_HZ_1500, BEARING_6205)
        self.assertEqual(by_rpm, by_hz)

    def test_bpfo_plus_bpfi_invariant_all_bearings(self):
        # Algebraically exact regardless of geometry: the r term cancels, so
        # bpfo + bpfi == n_balls * shaft_hz for every bearing/rpm combo, not
        # just the one hardcoded fixture above.
        for geom in COMMON_BEARINGS.values():
            for rpm in (900.0, 1500.0, 3000.0):
                bf = BearingFreqs.from_rpm(rpm, geom)
                shaft_hz = rpm / 60.0
                self.assertAlmostEqual(bf.bpfo + bf.bpfi, geom.n_balls * shaft_hz, places=6)

    def test_ftf_equals_bpfo_over_n_balls_invariant(self):
        # Also algebraically exact from the formulas: ftf == bpfo / n_balls.
        for geom in COMMON_BEARINGS.values():
            for rpm in (900.0, 1500.0, 3000.0):
                bf = BearingFreqs.from_rpm(rpm, geom)
                self.assertAlmostEqual(bf.ftf, bf.bpfo / geom.n_balls, places=6)


class TestMarkers(unittest.TestCase):
    def setUp(self):
        self.bf = BearingFreqs.from_rpm(RPM_1500, BEARING_6205)

    def test_markers_mic_fs_16000hz(self):
        markers = self.bf.markers(MIC_FS_HZ)
        expected_keys = {'shaft', '2×sh', 'BPFO', '2×BPFO', 'BPFI', '2×BPFI', 'BSF', 'FTF'}
        self.assertEqual(set(markers.keys()), expected_keys)
        self.assertAlmostEqual(markers['shaft'], SHAFT_HZ_1500, places=6)
        self.assertAlmostEqual(markers['BPFO'], BPFO_REF, places=6)

    def test_markers_imu_fs_25600hz(self):
        markers = self.bf.markers(IMU_FS_HZ)
        expected_keys = {'shaft', '2×sh', 'BPFO', '2×BPFO', 'BPFI', '2×BPFI', 'BSF', 'FTF'}
        self.assertEqual(set(markers.keys()), expected_keys)

    def test_markers_nyquist_filtering_excludes_high_freqs(self):
        # fs_hz=60 -> nyquist=30. Only shaft(25.0) and FTF(9.16) survive;
        # everything else (2xsh=50, BPFO=82.4, BPFI=142.6, BSF=43.4, and
        # both 2nd harmonics) is excluded -- exercises the filter itself,
        # not just the "everything fits" happy path above.
        markers = self.bf.markers(60.0)
        self.assertEqual(set(markers.keys()), {'shaft', 'FTF'})


class TestParseBearingArg(unittest.TestCase):
    def test_known_key(self):
        self.assertEqual(parse_bearing_arg('6205'), BEARING_6205)

    def test_custom_3_field(self):
        geom = parse_bearing_arg('9,38.5,10.3')
        self.assertEqual(geom, BearingGeometry('custom(9,38.5,10.3)', 9, 38.5, 10.3, 0.0))

    def test_custom_4_field(self):
        geom = parse_bearing_arg('8,33.5,9.5,5.0')
        self.assertEqual(geom, BearingGeometry('custom(8,33.5,9.5,5.0)', 8, 33.5, 9.5, 5.0))

    def test_invalid_returns_none(self):
        for bad in ('bogus', '1,2', 'x,y,z', '1,2,3,4,5', ''):
            self.assertIsNone(parse_bearing_arg(bad), msg=f"expected None for {bad!r}")


if __name__ == '__main__':
    unittest.main()
