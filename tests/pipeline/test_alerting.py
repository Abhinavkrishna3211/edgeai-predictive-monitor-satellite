#!/usr/bin/env python3
"""
test_alerting.py — Unit tests for gateway/pipeline/alerting.py.

alerting.py had zero direct test coverage prior to this file: the spectral
band analysis, fault-type classifier, and per-frame alert engine
(compute_alert) were only ever exercised indirectly by running the full
gateway against live/replayed hardware data.

Every function in alerting.py does a lazy `import recv_verify as _rv` inside
its own body (module docstring explains why), so exercising them here
requires recv_verify itself to be importable — same precondition as
tests/registry/test_satellite_state.py, and satisfied the same way (repo
root + mic_tools/ on sys.path, matplotlib forced to the Agg backend before
import).

compute_alert() tests use rv._sat_register() to build a real SatelliteState
(mirrors test_satellite_state.py) rather than hand-rolling a stand-in object,
so the adaptive-baseline/calibration/hysteresis fields it reads are always
present and consistent with what satellite_thread() actually hands it.

Run with:
    python -m pytest tests/pipeline/test_alerting.py -v
    python tests/pipeline/test_alerting.py
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
from gateway.pipeline.alerting import (
    _band_ratios, _high_band_ratio, _spectral_centroid,
    _extract_hst_features, _classify_fault_type, compute_alert,
)


def _dbfs_bins(n, loud_bins, loud_db=0.0, quiet_db=-140.0):
    """n-bin dBFS array with loud_db at the given bin indices, quiet_db elsewhere."""
    arr = np.full(n, quiet_db, dtype=np.float64)
    for i in loud_bins:
        arr[i] = loud_db
    return arr


class TestBandRatios(unittest.TestCase):
    def test_short_array_returns_zeros(self):
        self.assertEqual(_band_ratios(np.array([])), (0.0, 0.0, 0.0))
        self.assertEqual(_band_ratios(np.array([-10.0])), (0.0, 0.0, 0.0))

    def test_ratios_sum_to_one(self):
        arr = _dbfs_bins(512, [5, 100, 300])
        hi_r, lo_r, mid_r = _band_ratios(arr)
        self.assertAlmostEqual(hi_r + lo_r + mid_r, 1.0, places=6)

    def test_energy_concentrated_low_freq_favors_lo_r(self):
        # MIC_FS_HZ=16000, n=512 -> hz_per_bin=15.625, so bin 5 (~78Hz) is
        # deep in the 0-500Hz band.
        arr = _dbfs_bins(512, [5])
        hi_r, lo_r, mid_r = _band_ratios(arr)
        self.assertGreater(lo_r, 0.99)
        self.assertLess(hi_r, 0.01)
        self.assertLess(mid_r, 0.01)

    def test_energy_concentrated_high_freq_favors_hi_r(self):
        arr = _dbfs_bins(512, [500])  # near-Nyquist bin
        hi_r, lo_r, mid_r = _band_ratios(arr)
        self.assertGreater(hi_r, 0.99)
        self.assertLess(lo_r, 0.01)
        self.assertLess(mid_r, 0.01)


class TestHighBandRatio(unittest.TestCase):
    def test_matches_band_ratios_first_element(self):
        arr = _dbfs_bins(512, [5, 300, 500])
        expected_hi_r, _, _ = _band_ratios(arr)
        self.assertEqual(_high_band_ratio(arr), expected_hi_r)


class TestSpectralCentroid(unittest.TestCase):
    def test_short_array_returns_midpoint(self):
        self.assertEqual(_spectral_centroid(np.array([])), 0.5)
        self.assertEqual(_spectral_centroid(np.array([-10.0])), 0.5)

    def test_low_freq_energy_gives_low_centroid(self):
        arr = _dbfs_bins(512, [1])
        self.assertLess(_spectral_centroid(arr), 0.05)

    def test_high_freq_energy_gives_high_centroid(self):
        arr = _dbfs_bins(512, [510])
        self.assertGreater(_spectral_centroid(arr), 0.95)


class TestExtractHstFeatures(unittest.TestCase):
    def test_feature_vector_layout(self):
        frame = dict(mic_kurtosis=7.5, mic_crest=4.2, mic_rms=0.01, mic_fft=None)
        feats = _extract_hst_features(frame, lo_r=0.3, mid_r=0.2, hb=0.5)
        self.assertEqual(feats.shape, (7,))
        np.testing.assert_allclose(
            feats, [7.5, 4.2, 0.01, 0.5, 0.3, 0.2, 0.5])

    def test_centroid_computed_when_fft_present(self):
        arr = _dbfs_bins(512, [510])  # high-freq energy -> centroid near 1.0
        frame = dict(mic_kurtosis=1.0, mic_crest=1.0, mic_rms=1.0, mic_fft=arr)
        feats = _extract_hst_features(frame, lo_r=0.0, mid_r=0.0, hb=0.0)
        self.assertGreater(feats[3], 0.95)  # index 3 = spectral_centroid


class TestClassifyFaultType(unittest.TestCase):
    """Each case is chosen to fall through every earlier branch in
    _classify_fault_type() so only the branch under test can match --
    see alerting.py for the exact ordering."""

    def test_normal(self):
        label = _classify_fault_type(
            mic_kurtosis=3.0, mic_crest=2.0, imu_crest=2.0,
            hi_r=0.5, lo_r=0.5, mid_r=0.5)
        self.assertEqual(label, "Normal")

    def test_bearing_fault_early(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_WARN + 1.0, mic_crest=0.0, imu_crest=0.0,
            hi_r=0.5, lo_r=0.2, mid_r=0.3)
        self.assertEqual(label, "Bearing Fault — Early")

    def test_bearing_fault_advanced(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_FAULT + 3.0, mic_crest=0.0, imu_crest=0.0,
            hi_r=0.5, lo_r=0.2, mid_r=0.3)
        self.assertEqual(label, "Bearing Fault — Advanced")

    def test_mechanical_imbalance(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_WARN - 1.0, mic_crest=rv.CREST_WARN + 1.0,
            imu_crest=1.0, hi_r=0.2, lo_r=0.5, mid_r=0.2)
        self.assertEqual(label, "Mechanical Imbalance")

    def test_shaft_misalignment(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_WARN - 1.0, mic_crest=2.0,
            imu_crest=rv.IMU_CREST_WARN + 1.0, hi_r=0.2, lo_r=0.3, mid_r=0.4)
        self.assertEqual(label, "Shaft Misalignment")

    def test_imu_crest_between_mic_and_imu_warn_stays_normal(self):
        """Regression test for the real-rig FPR fix: imu_crest sitting above
        the old shared CREST_WARN (5.0) but below the new IMU-specific
        IMU_CREST_WARN (9.0) must not escalate past Normal -- this is exactly
        the ambient noise floor measured on the real rig (median imu_crest
        5.233, see tools/accuracy_harness/out/rig_baseline_report.md)."""
        self.assertGreater(rv.IMU_CREST_WARN, rv.CREST_WARN)
        probe_imu_crest = (rv.CREST_WARN + rv.IMU_CREST_WARN) / 2.0
        label = _classify_fault_type(
            mic_kurtosis=3.0, mic_crest=2.0, imu_crest=probe_imu_crest,
            hi_r=0.2, lo_r=0.3, mid_r=0.4)
        self.assertEqual(label, "Normal")

    def test_mechanical_looseness(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_WARN + 1.0, mic_crest=2.0, imu_crest=2.0,
            hi_r=0.2, lo_r=0.3, mid_r=0.3)
        self.assertEqual(label, "Mechanical Looseness")

    def test_severe_anomaly(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_FAULT + 3.0, mic_crest=2.0, imu_crest=2.0,
            hi_r=0.1, lo_r=0.8, mid_r=0.1)
        self.assertEqual(label, "Severe Anomaly — Inspect")

    def test_elevated_vibration(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_WARN + 2.0, mic_crest=2.0, imu_crest=2.0,
            hi_r=0.1, lo_r=0.8, mid_r=0.1)
        self.assertEqual(label, "Elevated Vibration")

    def test_anomalous_vibration_fallback(self):
        label = _classify_fault_type(
            mic_kurtosis=3.0, mic_crest=rv.CREST_WARN + 1.0, imu_crest=2.0,
            hi_r=0.1, lo_r=0.1, mid_r=0.1)
        self.assertEqual(label, "Anomalous Vibration")


class TestClassifyFaultTypePriorityScoring(unittest.TestCase):
    """Regression tests for the priority-collision fix: when a frame's
    numbers satisfy two fault categories' gates at once, the one with the
    stronger relative evidence must win -- not whichever was checked first
    in the old if/elif chain (Bearing Fault unconditionally, since it was
    branch 1). See _fault_candidate_scores() in alerting.py.

    Both cases below satisfy Bearing's gate (hi_r>0.40, kurtosis>=K_WARN)
    AND Mechanical Imbalance's gate (mic_crest>=CREST_WARN,
    kurtosis<K_WARN*1.4, lo_r>0.45) simultaneously -- only the relative
    margins differ, and the winner flips accordingly. Under the old
    branch-order code both cases would have returned a Bearing Fault label."""

    def test_overwhelming_imbalance_evidence_beats_barely_qualifying_bearing(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_WARN + 0.01, mic_crest=rv.CREST_WARN * 5,
            imu_crest=1.0, hi_r=0.41, lo_r=0.90, mid_r=0.0)
        self.assertEqual(label, "Mechanical Imbalance")

    def test_overwhelming_bearing_evidence_beats_barely_qualifying_imbalance(self):
        label = _classify_fault_type(
            mic_kurtosis=rv.K_WARN + 0.1, mic_crest=rv.CREST_WARN + 0.01,
            imu_crest=1.0, hi_r=0.95, lo_r=0.46, mid_r=0.0)
        self.assertEqual(label, "Bearing Fault — Early")


class ComputeAlertTestBase(unittest.TestCase):
    """Registers/unregisters a fresh SatelliteState per test via the same
    rv._sat_register() path satellite_thread() uses, so ab_kurtosis/bl_mean/
    calibrated/etc. are always populated exactly as production would."""

    def setUp(self):
        self._mac = 'AA:BB:CC:DD:EE:01'
        rv._satellites.pop(self._mac, None)
        self.sat = rv._sat_register(
            self._mac, 'TEST-ALERTING', 1, 0, ('127.0.0.1', 5100))

    def tearDown(self):
        rv._satellites.pop(self._mac, None)

    def _healthy_frame(self):
        return dict(mic_kurtosis=3.0, mic_crest=2.0, mic_rms=0.01, imu_crest=2.0)

    def _fault_frame(self):
        return dict(mic_kurtosis=rv.K_FAULT + 5.0, mic_crest=2.0,
                    mic_rms=0.01, imu_crest=2.0)


class TestComputeAlertCalibration(ComputeAlertTestBase):
    def test_calibrates_after_cal_frames_healthy_frames(self):
        self.assertFalse(self.sat.calibrated)
        warn_streak, ok_streak, sent_alert = 0, 0, rv.EPM_ALERT_OK
        for _ in range(rv.CAL_FRAMES):
            sent_alert, z, p, warn_streak, ok_streak = compute_alert(
                self.sat, self._healthy_frame(), warn_streak, ok_streak,
                sent_alert, hb=0.05)
        self.assertTrue(self.sat.calibrated)
        self.assertEqual(sent_alert, rv.EPM_ALERT_OK)


class TestComputeAlertImuCrestThreshold(ComputeAlertTestBase):
    """Regression coverage for the real-rig FPR fix: compute_alert() must
    gate mic_crest and imu_crest against their own separate thresholds, not
    max(mic_crest, imu_crest) against one shared CREST_WARN/CREST_FAULT."""

    def test_imu_crest_between_thresholds_does_not_raise_warn(self):
        probe_imu_crest = (rv.CREST_WARN + rv.IMU_CREST_WARN) / 2.0
        frame = dict(mic_kurtosis=3.0, mic_crest=2.0, mic_rms=0.01,
                     imu_crest=probe_imu_crest)
        warn_streak, ok_streak, sent_alert = 0, 0, rv.EPM_ALERT_OK
        for _ in range(rv.WARN_PERSIST + 1):
            sent_alert, z, p, warn_streak, ok_streak = compute_alert(
                self.sat, frame, warn_streak, ok_streak, sent_alert, hb=0.5)
        self.assertEqual(sent_alert, rv.EPM_ALERT_OK)

    def test_imu_crest_above_imu_warn_still_raises_warn(self):
        frame = dict(mic_kurtosis=3.0, mic_crest=2.0, mic_rms=0.01,
                     imu_crest=rv.IMU_CREST_WARN + 1.0)
        warn_streak, ok_streak, sent_alert = 0, 0, rv.EPM_ALERT_OK
        for _ in range(rv.WARN_PERSIST + 1):
            sent_alert, z, p, warn_streak, ok_streak = compute_alert(
                self.sat, frame, warn_streak, ok_streak, sent_alert, hb=0.5)
        self.assertEqual(sent_alert, rv.EPM_ALERT_WARN)


class TestComputeAlertNoiseFilter(ComputeAlertTestBase):
    def test_high_kurtosis_suppressed_when_high_band_energy_absent(self):
        # hb well below HIGH_BAND_MIN and the machine's adaptive HB baseline
        # is not warmed up -> factory-floor-noise filter must suppress the
        # raw FAULT down to OK rather than alerting on low-frequency noise.
        warn_streak, ok_streak, sent_alert = 0, 0, rv.EPM_ALERT_OK
        for _ in range(rv.WARN_PERSIST + 1):
            sent_alert, z, p, warn_streak, ok_streak = compute_alert(
                self.sat, self._fault_frame(), warn_streak, ok_streak,
                sent_alert, hb=0.01)
        self.assertEqual(sent_alert, rv.EPM_ALERT_OK)


class TestComputeAlertPersistence(ComputeAlertTestBase):
    def test_fault_requires_warn_persist_consecutive_frames(self):
        warn_streak, ok_streak, sent_alert = 0, 0, rv.EPM_ALERT_OK
        frame = self._fault_frame()
        for i in range(rv.WARN_PERSIST - 1):
            sent_alert, z, p, warn_streak, ok_streak = compute_alert(
                self.sat, frame, warn_streak, ok_streak, sent_alert, hb=0.5)
            self.assertEqual(sent_alert, rv.EPM_ALERT_OK,
                              f"must not raise before WARN_PERSIST (frame {i})")
        # One more non-OK frame reaches WARN_PERSIST -> raises.
        sent_alert, z, p, warn_streak, ok_streak = compute_alert(
            self.sat, frame, warn_streak, ok_streak, sent_alert, hb=0.5)
        self.assertEqual(sent_alert, rv.EPM_ALERT_FAULT)

    def test_fault_clears_only_after_fault_clear_persist_ok_frames(self):
        warn_streak, ok_streak, sent_alert = 0, 0, rv.EPM_ALERT_OK
        fault_frame = self._fault_frame()
        for _ in range(rv.WARN_PERSIST):
            sent_alert, z, p, warn_streak, ok_streak = compute_alert(
                self.sat, fault_frame, warn_streak, ok_streak, sent_alert, hb=0.5)
        self.assertEqual(sent_alert, rv.EPM_ALERT_FAULT)

        healthy_frame = self._healthy_frame()
        for i in range(rv.FAULT_CLEAR_PERSIST - 1):
            sent_alert, z, p, warn_streak, ok_streak = compute_alert(
                self.sat, healthy_frame, warn_streak, ok_streak, sent_alert, hb=0.05)
            self.assertEqual(sent_alert, rv.EPM_ALERT_FAULT,
                              f"FAULT must hold until FAULT_CLEAR_PERSIST (frame {i})")
        sent_alert, z, p, warn_streak, ok_streak = compute_alert(
            self.sat, healthy_frame, warn_streak, ok_streak, sent_alert, hb=0.05)
        self.assertEqual(sent_alert, rv.EPM_ALERT_OK)

    def test_healthy_frames_hold_ok_and_reset_warn_streak(self):
        warn_streak, ok_streak, sent_alert = 0, 0, rv.EPM_ALERT_OK
        for _ in range(5):
            sent_alert, z, p, warn_streak, ok_streak = compute_alert(
                self.sat, self._healthy_frame(), warn_streak, ok_streak,
                sent_alert, hb=0.05)
        self.assertEqual(sent_alert, rv.EPM_ALERT_OK)
        self.assertEqual(warn_streak, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
