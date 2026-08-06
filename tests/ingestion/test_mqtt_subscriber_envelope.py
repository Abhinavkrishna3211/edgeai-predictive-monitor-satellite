#!/usr/bin/env python3
"""
test_mqtt_subscriber_envelope.py — decoded_to_frame_dict() envelope-channel
tests for gateway/ingestion/mqtt_subscriber.py (Phase 11b).

Phase 11a shipped 3 new SPECTRUM wire channels (accel_x/y/z_envelope, ids
9-11, ADR-032). The wire-level codec (telemetry_frame.decode_frame()) and
the schema maps already handled these correctly -- see
tests/common/test_telemetry_frame.py -- but decoded_to_frame_dict() only
ever read mic/accel_x/y/z out of DecodedFrame.bins, so envelope sections
decoded fine and then vanished before reaching the flat frame-dict shape
live_plot.py/main.py consume. This is the adapter-layer gap Phase 11b
closes.

Exercises decoded_to_frame_dict() directly against a hand-built frame (same
style as test_telemetry_frame.py's _build_frame()) rather than going through
MqttIngestor -- the thing worth proving here is the adapter's translation
from DecodedFrame.spectra (which carries fs, unlike .bins) into the flat
imu_env_x/y/z + imu_env_fs keys, not the paho callback plumbing already
covered by test_mqtt_subscriber.py.

Run with:
    python -m pytest tests/ingestion/test_mqtt_subscriber_envelope.py -v
    python tests/ingestion/test_mqtt_subscriber_envelope.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import gateway.common.telemetry_schema as schema
from gateway.common.telemetry_frame import decode_frame, encode_spectrum_frame
from gateway.ingestion.mqtt_subscriber import decoded_to_frame_dict

SOURCE_SATELLITE = schema.SOURCE_ID["satellite"]

MIC_ID   = schema.CHANNEL_ID_BY_NAME["mic"]
X_ID     = schema.CHANNEL_ID_BY_NAME["accel_x"]
Y_ID     = schema.CHANNEL_ID_BY_NAME["accel_y"]
Z_ID     = schema.CHANNEL_ID_BY_NAME["accel_z"]
ENV_X_ID = schema.CHANNEL_ID_BY_NAME["accel_x_envelope"]
ENV_Y_ID = schema.CHANNEL_ID_BY_NAME["accel_y_envelope"]
ENV_Z_ID = schema.CHANNEL_ID_BY_NAME["accel_z_envelope"]

MIC_FS_HZ = 16000.0
IMU_FS_HZ = 25600.0
ENV_FS_HZ = 3200.0   # IMU_FS_HZ / IMU_ENVELOPE_DECIM(8) -- used only to build
                     # the synthetic wire frame below; must NEVER appear
                     # hardcoded inside mqtt_subscriber.py itself

ENV_BINS = tuple(float(-100 + i) for i in range(128))   # distinct per-bin values


def _frame_with_envelope() -> bytes:
    """mic + 3 raw axes + 3 envelope axes, all as SPECTRUM sections -- the
    real shape net_task.c's build_real_frame() emits post-Phase-11a (8
    sections total)."""
    return encode_spectrum_frame(SOURCE_SATELLITE, [
        (MIC_ID,   MIC_FS_HZ, 1024, (-80.0, -75.5, -60.25, -40.0, -20.0)),
        (X_ID,     IMU_FS_HZ, 2048, (-90.0,) * 1024),
        (Y_ID,     IMU_FS_HZ, 2048, (-91.0,) * 1024),
        (Z_ID,     IMU_FS_HZ, 2048, (-92.0,) * 1024),
        (ENV_X_ID, ENV_FS_HZ, 256,  ENV_BINS),
        (ENV_Y_ID, ENV_FS_HZ, 256,  tuple(v - 1 for v in ENV_BINS)),
        (ENV_Z_ID, ENV_FS_HZ, 256,  tuple(v - 2 for v in ENV_BINS)),
    ])


class TestEnvelopeChannelsInFrameDict(unittest.TestCase):
    def setUp(self):
        self.decoded = decode_frame(_frame_with_envelope())
        self.frame = decoded_to_frame_dict(self.decoded, frame_id=1, ts_ms=0)

    def test_envelope_arrays_present_with_right_values(self):
        self.assertEqual(tuple(self.frame['imu_env_x']), ENV_BINS)
        self.assertEqual(len(self.frame['imu_env_y']), 128)
        self.assertEqual(len(self.frame['imu_env_z']), 128)

    def test_envelope_fs_comes_from_wire_not_a_hardcoded_constant(self):
        # Envelope fs deliberately NOT 3200 Hz here -- if mqtt_subscriber.py
        # hardcoded IMU_FS_HZ/8 (or any other constant) anywhere instead of
        # reading ChannelSpectrum.fs, this would fail to reflect the actual
        # wire value.
        odd_fs = 4321.0
        frame_bytes = encode_spectrum_frame(SOURCE_SATELLITE, [
            (ENV_X_ID, odd_fs, 256, ENV_BINS),
        ])
        decoded = decode_frame(frame_bytes)
        frame = decoded_to_frame_dict(decoded, frame_id=1, ts_ms=0)
        self.assertEqual(frame['imu_env_fs'], odd_fs)

    def test_raw_imu_and_mic_panels_unaffected(self):
        # Task 2's "no regression to the 4 existing panels" exit criterion,
        # exercised here at the adapter layer that feeds them.
        self.assertEqual(len(self.frame['imu_x']), 1024)
        self.assertEqual(len(self.frame['imu_y']), 1024)
        self.assertEqual(len(self.frame['imu_z']), 1024)
        self.assertEqual(len(self.frame['mic_fft']), 5)

    def test_missing_envelope_sections_default_to_empty_and_zero_fs(self):
        # An MQTT frame that omits the envelope sections entirely (e.g. an
        # older firmware build, or a node with envelope disabled) must
        # degrade cleanly to empty arrays / fs=0.0 rather than KeyError
        # downstream in live_plot.py.
        frame_bytes = encode_spectrum_frame(SOURCE_SATELLITE, [
            (MIC_ID, MIC_FS_HZ, 1024, (-80.0,)),
        ])
        decoded = decode_frame(frame_bytes)
        frame = decoded_to_frame_dict(decoded, frame_id=1, ts_ms=0)
        self.assertEqual(len(frame['imu_env_x']), 0)
        self.assertEqual(len(frame['imu_env_y']), 0)
        self.assertEqual(len(frame['imu_env_z']), 0)
        self.assertEqual(frame['imu_env_fs'], 0.0)


if __name__ == "__main__":
    unittest.main()
