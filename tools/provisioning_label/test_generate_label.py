#!/usr/bin/env python3
"""
test_generate_label.py — Unit tests for generate_label.py's pure logic:
the provisioning-log-line parser and the WiFi QR payload builder. Neither
needs a serial port, qrcode, or Pillow installed (both are lazy-imported
inside generate_label.py's render/serial functions), so this file has no
dependency on tools/provisioning_label/requirements.txt.

Not wired into the repo's root `tests/` pytest suite / CI — this tool is
bench tooling (see requirements.txt), not something CI needs to install.
Run manually:
    python -m pytest tools/provisioning_label/test_generate_label.py -v
    python tools/provisioning_label/test_generate_label.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from generate_label import build_wifi_qr_payload, node_id_from_ssid, parse_log_line


class TestParseLogLine(unittest.TestCase):
    def test_matches_provisioning_c_format(self):
        line = (
            'I (1234) provisioning: provisioning AP up: ssid="EPM-SAT-a1b2c3" '
            'password="0123456789abcdef0123456789abcdef" (WPA2-PSK, http://192.168.4.1)'
        )
        self.assertEqual(
            parse_log_line(line), ("EPM-SAT-a1b2c3", "0123456789abcdef0123456789abcdef")
        )

    def test_no_match_returns_none(self):
        self.assertIsNone(parse_log_line("I (999) wifi_task: connecting to STA..."))
        self.assertIsNone(parse_log_line(""))

    def test_matches_regardless_of_log_prefix(self):
        # Real ESP_LOGI output is prefixed with a level/tag/timestamp header
        # that varies by build; the parser should only care about the
        # ssid=".."/password=".." portion of the line.
        line = 'ssid="EPM-SAT-ffffff" password="deadbeef"'
        self.assertEqual(parse_log_line(line), ("EPM-SAT-ffffff", "deadbeef"))

    def test_ssid_only_no_password_returns_none(self):
        self.assertIsNone(parse_log_line('ssid="EPM-SAT-a1b2c3" connecting...'))


class TestNodeIdFromSsid(unittest.TestCase):
    def test_strips_prefix(self):
        self.assertEqual(node_id_from_ssid("EPM-SAT-a1b2c3"), "a1b2c3")

    def test_passes_through_when_no_prefix(self):
        self.assertEqual(node_id_from_ssid("some-other-ssid"), "some-other-ssid")


class TestBuildWifiQrPayload(unittest.TestCase):
    def test_basic_payload(self):
        self.assertEqual(
            build_wifi_qr_payload("EPM-SAT-a1b2c3", "0123456789abcdef0123456789abcdef"),
            "WIFI:T:WPA;S:EPM-SAT-a1b2c3;P:0123456789abcdef0123456789abcdef;;",
        )

    def test_escapes_reserved_characters(self):
        # Defensive correctness per the WIFI: QR format spec — provisioning.c's
        # actual SSID/password alphabet never produces these today.
        payload = build_wifi_qr_payload('a;b,c:d"e\\f', 'p;q')
        self.assertEqual(payload, 'WIFI:T:WPA;S:a\\;b\\,c\\:d\\"e\\\\f;P:p\\;q;;')

    def test_empty_fields(self):
        self.assertEqual(build_wifi_qr_payload("", ""), "WIFI:T:WPA;S:;P:;;")


if __name__ == "__main__":
    unittest.main()
