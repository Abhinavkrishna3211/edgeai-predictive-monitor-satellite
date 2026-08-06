#!/usr/bin/env python3
"""
test_mqtt_subscriber.py — integration-level malformed-frame tests for
gateway/ingestion/mqtt_subscriber.py (Phase 10b, Task 3).

Exercises MqttIngestor._on_message() itself -- paho's real callback entry
point -- rather than decode_frame() in isolation (already covered by
tests/common/test_telemetry_frame.py). The thing worth proving here is
integration-level: that the try/except already in _on_message() actually
catches a malformed payload, and that the ingestor is still able to process
the *next* message for the same node afterward (Part G's stated exit --
"gateway handles a malformed/late frame without crashing").

Uses a minimal fake `rv_module` stand-in for recv_verify.py instead of a
real import: the real module's _process_satellite_frame() pulls in HST/RUL/
ML model loading, SQLite storage, and matplotlib, none of which this test
needs -- "keeps running for the next message" only requires observing
_on_message()'s own control flow (registration + frame_counter + which
frames reach _process_satellite_frame), not recv_verify's full pipeline.

"Late frame" is deliberately not tested here -- see mqtt_subscriber.py's own
module docstring: the wire format carries no frame_id/timestamp, so
MqttIngestor synthesizes a per-node counter locally from whatever it
receives, which is monotonic by construction. There is no wire-level
ordering signal for a "late" MQTT frame to violate, and no staleness/
timeout mechanism exists for MQTT nodes at all (that's tcp_legacy.py-
specific, via its real frame_id replay check in
recv_verify._process_satellite_frame()). Forcing a test for a concept
that doesn't map onto this transport would just be testing the fake.

Run with:
    python -m pytest tests/ingestion/test_mqtt_subscriber.py -v
    python tests/ingestion/test_mqtt_subscriber.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import gateway.common.telemetry_schema as schema
from gateway.common.telemetry_frame import (
    encode_frame,
    encode_scalar_body,
    encode_section,
    encode_spectrum_body,
)
from gateway.ingestion.mqtt_subscriber import MqttIngestor

SOURCE_SATELLITE = schema.SOURCE_ID["satellite"]
KIND_SPECTRUM = schema.DATA_KIND["SPECTRUM"]
KIND_SCALAR_SET = schema.DATA_KIND["SCALAR_SET"]
MIC_CHANNEL_ID = schema.CHANNEL_ID_BY_NAME["mic"]
RMS_MIC_ID = schema.SCALAR_ID_BY_NAME["rms_mic"]
KURTOSIS_MIC_ID = schema.SCALAR_ID_BY_NAME["kurtosis_mic"]


def _well_formed_frame() -> bytes:
    """Same shape as test_telemetry_frame.py's _build_frame(): one SPECTRUM
    section (mic) + one SCALAR_SET section."""
    spectrum_section = encode_section(
        SOURCE_SATELLITE, MIC_CHANNEL_ID, KIND_SPECTRUM,
        encode_spectrum_body(16000.0, 1024, (-80.0, -75.5, -60.25, -40.0, -20.0)))
    scalar_section = encode_section(
        SOURCE_SATELLITE, schema.PERF_CHANNEL_ID, KIND_SCALAR_SET,
        encode_scalar_body({RMS_MIC_ID: 0.0123, KURTOSIS_MIC_ID: 3.4}))
    return encode_frame([spectrum_section, scalar_section])


class _FakeSat:
    def __init__(self, name):
        self.name = name


class _FakeRV:
    """Minimal stand-in for recv_verify.py -- just enough surface for
    MqttIngestor._on_message() to run end to end without pulling in real
    model loading, SQLite storage, or matplotlib."""
    EPM_ALERT_OK = 0

    def __init__(self, base_dir):
        self._BASE_DIR = base_dir
        self._satellites = {}
        self.process_calls = []   # frame_id of every frame that reached here

    def _sat_register(self, mac_hex, name, fw_major, fw_minor, addr):
        sat = _FakeSat(name)
        self._satellites[mac_hex] = sat
        return sat

    def _process_satellite_frame(self, sat, frame, node_id, csv_w, csv_f,
                                 warn_streak, ok_streak, sent_alert, last_frame_id):
        self.process_calls.append(frame["frame_id"])
        return (self.EPM_ALERT_OK, 0.0, time.time(),
                warn_streak, ok_streak, sent_alert, frame["frame_id"])


class TestMqttOnMessageMalformedFrame(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.rv = _FakeRV(self._tmpdir)
        self.ingestor = MqttIngestor(self.rv, host="localhost", port=1883)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_truncated_payload_is_caught_and_dropped(self):
        msg = SimpleNamespace(topic="epm/nodeA/data", payload=_well_formed_frame()[:-1])
        self.ingestor._on_message(None, None, msg)   # must not raise
        self.assertEqual(self.ingestor._nodes, {})
        self.assertEqual(self.rv.process_calls, [])

    def test_empty_payload_is_caught_and_dropped(self):
        msg = SimpleNamespace(topic="epm/nodeA/data", payload=b"")
        self.ingestor._on_message(None, None, msg)   # must not raise
        self.assertEqual(self.ingestor._nodes, {})
        self.assertEqual(self.rv.process_calls, [])

    def test_malformed_topic_is_caught_and_dropped(self):
        msg = SimpleNamespace(topic="not-a-topic", payload=b"irrelevant")
        self.ingestor._on_message(None, None, msg)   # IndexError path, must not raise
        self.assertEqual(self.ingestor._nodes, {})

    def test_ingestor_keeps_running_after_malformed_frames(self):
        """The regression this guards against: a malformed frame (or two)
        must not leave _on_message unable to process the node's next,
        well-formed frame -- proving the ingestion path survives bad input
        rather than just proving decode_frame() raises in isolation."""
        bad_msg = SimpleNamespace(topic="epm/nodeA/data", payload=b"")
        self.ingestor._on_message(None, None, bad_msg)
        self.ingestor._on_message(None, None, bad_msg)

        good_msg = SimpleNamespace(topic="epm/nodeA/data", payload=_well_formed_frame())
        self.ingestor._on_message(None, None, good_msg)

        self.assertIn("nodeA", self.ingestor._nodes)
        self.assertEqual(self.ingestor._nodes["nodeA"].frame_counter, 1)
        self.assertEqual(self.rv.process_calls, [1])


if __name__ == "__main__":
    unittest.main()
