#!/usr/bin/env python3
"""
test_telemetry_frame.py — Unit tests for mic_tools/telemetry_frame.py.

This is the first thing in mic_tools/ that decodes the real MQTT
section-list wire format (Phase 8a) -- everything before this phase only
ever spoke the now-deleted TCP+AES fixed-header protocol. Exercises
decode_frame() directly against a hand-built payload (one SPECTRUM section +
one SCALAR_SET section, matching a real satellite frame's shape) rather than
going through a broker, mirroring test_bearing_math.py's no-mocking-needed
style (pure codec, no I/O, no hardware).

Run with:
    python -m pytest tests/common/test_telemetry_frame.py -v
    python tests/common/test_telemetry_frame.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import gateway.common.telemetry_schema as schema
from gateway.common.telemetry_frame import (
    MalformedFrameError,
    decode_frame,
    encode_frame,
    encode_scalar_body,
    encode_section,
    encode_spectrum_body,
)
from gateway.common.wire_protocol import ChannelSpectrum

SOURCE_SATELLITE = schema.SOURCE_ID["satellite"]
KIND_SPECTRUM = schema.DATA_KIND["SPECTRUM"]
KIND_SCALAR_SET = schema.DATA_KIND["SCALAR_SET"]

MIC_CHANNEL_ID = schema.CHANNEL_ID_BY_NAME["mic"]
MIC_FS_HZ = 16000.0
MIC_FFT_N = 1024
MIC_BINS = (-80.0, -75.5, -60.25, -40.0, -20.0)

RMS_MIC_ID = schema.SCALAR_ID_BY_NAME["rms_mic"]
KURTOSIS_MIC_ID = schema.SCALAR_ID_BY_NAME["kurtosis_mic"]
SCALARS = {RMS_MIC_ID: 0.0123, KURTOSIS_MIC_ID: 3.4}


def _build_frame() -> bytes:
    """One SPECTRUM section (mic) + one SCALAR_SET section (rms_mic,
    kurtosis_mic) -- the exit test's stated minimum, per PHASE_8A_PROMPT.md."""
    spectrum_section = encode_section(
        SOURCE_SATELLITE, MIC_CHANNEL_ID, KIND_SPECTRUM,
        encode_spectrum_body(MIC_FS_HZ, MIC_FFT_N, MIC_BINS))
    scalar_section = encode_section(
        SOURCE_SATELLITE, schema.PERF_CHANNEL_ID, KIND_SCALAR_SET,
        encode_scalar_body(SCALARS))
    return encode_frame([spectrum_section, scalar_section])


class TestDecodeFrame(unittest.TestCase):
    def test_spectrum_section_decodes_into_bins_and_spectra(self):
        decoded = decode_frame(_build_frame())
        self.assertIn("mic", decoded.bins)
        self.assertEqual(decoded.bins["mic"], MIC_BINS)
        self.assertIn("mic", decoded.spectra)
        spectrum = decoded.spectra["mic"]
        self.assertIsInstance(spectrum, ChannelSpectrum)
        self.assertEqual(spectrum.fs, MIC_FS_HZ)
        self.assertEqual(spectrum.fft_size, MIC_FFT_N)
        self.assertEqual(spectrum.bins, MIC_BINS)

    def test_scalar_set_section_decodes_by_raw_id(self):
        # struct-packs values as f32 on the wire, so compare with float32
        # precision (a real bit-width round-trip, not a test-precision fudge).
        decoded = decode_frame(_build_frame())
        self.assertAlmostEqual(decoded.scalars[RMS_MIC_ID],
                                SCALARS[RMS_MIC_ID], places=5)
        self.assertAlmostEqual(decoded.scalars[KURTOSIS_MIC_ID],
                                SCALARS[KURTOSIS_MIC_ID], places=5)

    def test_scalar_ids_resolve_to_schema_names(self):
        decoded = decode_frame(_build_frame())
        by_name = {schema.SCALAR_NAME_BY_ID[sid]: v
                   for sid, v in decoded.scalars.items()}
        self.assertAlmostEqual(by_name["rms_mic"], SCALARS[RMS_MIC_ID], places=5)
        self.assertAlmostEqual(by_name["kurtosis_mic"], SCALARS[KURTOSIS_MIC_ID],
                                places=5)

    def test_no_unknown_sections_for_a_well_formed_frame(self):
        decoded = decode_frame(_build_frame())
        self.assertEqual(decoded.unknown_sections, 0)
        self.assertEqual(decoded.time_series, {})

    def test_zero_bin_count_channel_contributes_no_bins_key(self):
        """bin_count=0 is the 'channel not part of this node' case (plan
        S4) -- distinct from an all-zero zero-fill spectrum, which DOES
        get a bins key."""
        section = encode_section(
            SOURCE_SATELLITE, MIC_CHANNEL_ID, KIND_SPECTRUM,
            encode_spectrum_body(MIC_FS_HZ, MIC_FFT_N, ()))
        decoded = decode_frame(encode_frame([section]))
        self.assertNotIn("mic", decoded.bins)
        self.assertIn("mic", decoded.spectra)   # still recorded, just no bins

    def test_unknown_channel_id_counts_as_unknown_section(self):
        bogus_channel_id = 250   # not in CHANNEL_NAME_BY_ID
        section = encode_section(
            SOURCE_SATELLITE, bogus_channel_id, KIND_SPECTRUM,
            encode_spectrum_body(MIC_FS_HZ, MIC_FFT_N, MIC_BINS))
        decoded = decode_frame(encode_frame([section]))
        self.assertEqual(decoded.unknown_sections, 1)
        self.assertEqual(decoded.bins, {})

    def test_unrecognized_data_kind_skipped_by_length_not_raised(self):
        bogus_kind = 99
        body = b"\x01\x02\x03\x04"
        section = encode_section(SOURCE_SATELLITE, MIC_CHANNEL_ID, bogus_kind, body)
        decoded = decode_frame(encode_frame([section]))
        self.assertEqual(decoded.unknown_sections, 1)

    def test_truncated_payload_raises_malformed_frame_error(self):
        frame = _build_frame()
        with self.assertRaises(MalformedFrameError):
            decode_frame(frame[:-1])

    def test_empty_payload_raises_malformed_frame_error(self):
        with self.assertRaises(MalformedFrameError):
            decode_frame(b"")


if __name__ == "__main__":
    unittest.main()
