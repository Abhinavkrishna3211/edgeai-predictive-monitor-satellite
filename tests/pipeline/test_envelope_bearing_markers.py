#!/usr/bin/env python3
"""
test_envelope_bearing_markers.py — Phase 11b sanity check: does a bearing
fault-frequency marker actually land near a real spectral peak in a decoded
envelope spectrum, or does it just draw somewhere on the panel?

This is the check PHASE_11B's own prompt called "the actual point of the
whole feature" -- Phase 11a's envelope channels only help if BPFO/BPFI/BSF/
FTF markers, computed via bearing_math.BearingFreqs.markers(), line up with
where a real bearing-fault signature actually shows up in the (decimated)
envelope spectrum. It is not enough for run_plot()'s envelope panels to
render without crashing.

Scope note on how the synthetic signal is built: tests/host/test_envelope.c
already covers the firmware DSP chain itself (band-pass -> rectify ->
low-pass -> decimate -> FFT, via a Goertzel reference detector on a known
AM-modulated carrier) -- re-deriving that DSP in Python here would just be a
weaker duplicate of a C test that already links the real source. What the
*gateway* side has never had exercised is everything downstream of the DSP:
does a spectrum's real energy concentration survive encode_spectrum_frame()
-> decode_frame() -> decoded_to_frame_dict() -> BearingFreqs.markers() with
its bearing-frequency label landing on the right bin. So this test builds a
synthetic envelope spectrum shaped the same way test_envelope.c's carrier
does (band-limited energy concentrated at one dominant frequency, matching
what epm_dsp_envelope_process() output looks like for a real outer-race
defect: strong tone at 1x BPFO), pushes it through the real wire codec, and
checks where the BPFO marker actually lands versus where the spectrum's own
peak actually is.

tools/satellite_sim.py could not stand in for this: it and
gateway/ingestion/tcp_legacy.py both speak the old fixed 48-byte-header TCP
wire format (mic + 3 raw IMU axes only) -- structurally incapable of
carrying channel ids 9-11 (see tcp_legacy.py's own module docstring). Only
the MQTT section-list path (gateway.common.telemetry_frame) can carry an
envelope channel at all, so that is the real path this test drives, using
the same encode_spectrum_frame()/decode_frame() this project's own tests
already use elsewhere.

Run with:
    python -m pytest tests/pipeline/test_envelope_bearing_markers.py -v -s
    python tests/pipeline/test_envelope_bearing_markers.py
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import gateway.common.telemetry_schema as schema
from gateway.common.telemetry_frame import decode_frame, encode_spectrum_frame
from gateway.ingestion.mqtt_subscriber import decoded_to_frame_dict
from gateway.pipeline.bearing_math import BearingFreqs, COMMON_BEARINGS

SOURCE_SATELLITE = schema.SOURCE_ID["satellite"]
ENV_X_ID = schema.CHANNEL_ID_BY_NAME["accel_x_envelope"]

# Same bearing/speed fixture as test_bearing_math.py (6205 @ 1500 RPM) --
# also recv_verify.py's own --shaft-rpm 1500 --bearing 6205 CLI example.
BEARING_6205 = COMMON_BEARINGS['6205']
SHAFT_RPM    = 1500.0

# Firmware constants (src/epm_config.h): IMU_ENVELOPE_N=256 (FFT_IMU_N/8),
# IMU_ENVELOPE_HALF=128 positive-frequency bins, decimated fs = IMU_FS_HZ/8.
IMU_FS_HZ       = 25600.0
ENV_DECIM       = 8
ENV_FS_HZ       = IMU_FS_HZ / ENV_DECIM     # 3200 Hz
ENV_FFT_SIZE    = 256
ENV_N_BINS      = 128
ENV_BIN_WIDTH_HZ = ENV_FS_HZ / ENV_FFT_SIZE  # 12.5 Hz/bin

NOISE_FLOOR_DBFS = -90.0
PEAK_DBFS        = -20.0


def _synthetic_outer_race_envelope_spectrum(fault_freq_hz: float) -> tuple:
    """A 128-bin dBFS envelope spectrum shaped like epm_dsp_envelope_process()'s
    real output for an outer-race defect: near-noise-floor everywhere except a
    dominant tone at fault_freq_hz (+ a smaller 2nd-harmonic tone, matching
    fault_models.py's add_bearing_fault()'s 'outer' case and Part G's own
    "peak + harmonics" convention from test_envelope.c). Energy is spread
    across the 2 nearest bins (linear interpolation by distance to each bin
    center) rather than dumped into one bin, matching how a real FFT of a
    frequency that doesn't land exactly on a bin center behaves -- the same
    spirit as fault_models.py's _add_tone()."""
    bins = [NOISE_FLOOR_DBFS] * ENV_N_BINS

    def _add_peak(freq_hz, peak_dbfs):
        pos = freq_hz / ENV_BIN_WIDTH_HZ - 0.5   # bin k's center is (k+0.5)*bw
        k_lo = int(np.floor(pos))
        frac = pos - k_lo
        for k, w in ((k_lo, 1.0 - frac), (k_lo + 1, frac)):
            if 0 <= k < ENV_N_BINS and w > 0.05:
                # Linear (not dB) mix so the dominant bin stays close to
                # peak_dbfs rather than being diluted by log-averaging.
                lin_existing = 10 ** (bins[k] / 20.0)
                lin_add      = w * 10 ** (peak_dbfs / 20.0)
                bins[k] = 20.0 * np.log10(lin_existing + lin_add)

    _add_peak(fault_freq_hz, PEAK_DBFS)
    _add_peak(2 * fault_freq_hz, PEAK_DBFS - 8.0)   # 2nd harmonic, smaller
    return tuple(float(b) for b in bins)


class TestEnvelopeMarkerLandsNearRealPeak(unittest.TestCase):
    def setUp(self):
        self.bf = BearingFreqs.from_rpm(SHAFT_RPM, BEARING_6205)
        self.bpfo_hz = self.bf.bpfo   # ~82.4 Hz

    def test_bpfo_marker_lands_within_one_bin_of_the_actual_spectral_peak(self):
        spectrum_bins = _synthetic_outer_race_envelope_spectrum(self.bpfo_hz)

        # ── Real wire round-trip: exactly what a satellite would publish and
        # the gateway's MQTT path would ingest (Task 1's decoded_to_frame_dict). ──
        wire_bytes = encode_spectrum_frame(
            SOURCE_SATELLITE, [(ENV_X_ID, ENV_FS_HZ, ENV_FFT_SIZE, spectrum_bins)])
        decoded = decode_frame(wire_bytes)
        frame = decoded_to_frame_dict(decoded, frame_id=1, ts_ms=0)

        decoded_fs = frame['imu_env_fs']
        decoded_bins = np.asarray(frame['imu_env_x'])
        self.assertEqual(decoded_fs, ENV_FS_HZ)
        self.assertEqual(len(decoded_bins), ENV_N_BINS)

        # ── Where the spectrum's own energy actually peaks. ──
        peak_bin = int(np.argmax(decoded_bins))
        peak_freq_hz = (peak_bin + 0.5) * (decoded_fs / ENV_FFT_SIZE)

        # ── Where Task 2's live_plot.py would draw the BPFO marker: exactly
        # bf.markers(imu_env_fs), the same call run_plot() makes once a real
        # frame's fs is known. ──
        markers = self.bf.markers(decoded_fs)
        self.assertIn('BPFO', markers)
        marker_freq_hz = markers['BPFO']

        gap_hz = abs(peak_freq_hz - marker_freq_hz)
        bin_width_hz = decoded_fs / ENV_FFT_SIZE

        print(f"\n[Task 3 sanity check] outer-race defect synthesized at "
              f"{self.bpfo_hz:.2f} Hz (BPFO, 6205 bearing @ {SHAFT_RPM:.0f} RPM)")
        print(f"[Task 3 sanity check] decoded envelope fs={decoded_fs:.1f} Hz  "
              f"bin_width={bin_width_hz:.2f} Hz")
        print(f"[Task 3 sanity check] spectrum peak: bin {peak_bin} -> "
              f"{peak_freq_hz:.2f} Hz  ({decoded_bins[peak_bin]:.1f} dBFS)")
        print(f"[Task 3 sanity check] BPFO marker:   {marker_freq_hz:.2f} Hz")
        print(f"[Task 3 sanity check] gap: {gap_hz:.2f} Hz "
              f"({gap_hz / bin_width_hz:.2f} bins)")

        self.assertLessEqual(gap_hz, bin_width_hz,
            f"BPFO marker ({marker_freq_hz:.2f} Hz) is {gap_hz:.2f} Hz "
            f"({gap_hz / bin_width_hz:.2f} bins) from the actual spectral "
            f"peak ({peak_freq_hz:.2f} Hz) -- should be within one bin")

    def test_wrong_fs_would_fabricate_an_off_panel_marker(self):
        """Negative control for Task 2's dynamic-fs plumbing: at 1500 RPM
        every 6205 fault frequency clears both Nyquists (see
        test_bearing_math.py's fixture notes), so this uses the same faster
        1000 Hz shaft as test_bearing_math.py's
        test_markers_envelope_fs_differs_from_raw_imu_fs to get a frequency
        that actually depends on which fs is used.

        If run_plot() were wired to (incorrectly) call bf.markers(imu_fs)
        for an envelope panel instead of bf.markers(env_fs), 2xBPFO would
        come back as a 'real' marker at ~6592 Hz -- but the envelope panel's
        own xlim is set to env_fs/2 = 1600 Hz (from the panel's own decoded
        data), so that marker would be fabricated past the right edge of the
        axis entirely: not just imprecise, but for a frequency the panel
        cannot represent at all. bf.markers(env_fs) correctly omits it."""
        bf_fast = BearingFreqs.from_shaft_hz(1000.0, BEARING_6205)
        two_x_bpfo = 2 * bf_fast.bpfo

        markers_wrong = bf_fast.markers(IMU_FS_HZ)   # the bug this guards against
        markers_right = bf_fast.markers(ENV_FS_HZ)   # what Task 2 actually wires up

        self.assertIn('2×BPFO', markers_wrong)
        self.assertAlmostEqual(markers_wrong['2×BPFO'], two_x_bpfo, places=6)
        self.assertGreater(two_x_bpfo, ENV_FS_HZ / 2,
            "fixture must pick a frequency past the envelope Nyquist for this control to mean anything")
        self.assertNotIn('2×BPFO', markers_right)


if __name__ == "__main__":
    unittest.main()
