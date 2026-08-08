#!/usr/bin/env python3
"""
test_generate_label.py — Unit tests for generate_label.py.

TestParseLogLine, TestNodeIdFromSsid, TestBuildWifiQrPayload, and
TestResolveInputMode cover pure logic only — no serial port, qrcode, or
Pillow needed (those are lazy-imported inside generate_label.py's
render/serial functions), so this file has no hard dependency on
tools/provisioning_label/requirements.txt.

TestRenderLabel additionally exercises the actual PNG rendering path
end-to-end (canvas sizing, save) and is skipped automatically if qrcode/
Pillow aren't installed — install tools/provisioning_label/requirements.txt
to run it.

Not wired into the repo's root `tests/` pytest suite / CI — this tool is
bench tooling (see requirements.txt), not something CI needs to install.
Run manually:
    python -m pytest tools/provisioning_label/test_generate_label.py -v
    python tools/provisioning_label/test_generate_label.py
"""

import argparse
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from generate_label import (
    build_wifi_qr_payload,
    node_id_from_ssid,
    parse_log_line,
    resolve_input_mode,
)

try:
    import qrcode  # noqa: F401
    from PIL import Image, ImageChops  # noqa: F401

    _RENDER_DEPS_AVAILABLE = True
except ImportError:
    _RENDER_DEPS_AVAILABLE = False

try:
    import cv2  # noqa: F401

    _CV2_AVAILABLE = True
except ImportError:
    # Not a tool dependency (see requirements.txt) — only used here, if
    # present, to independently verify a rendered QR actually decodes.
    _CV2_AVAILABLE = False


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


def _args(port=None, ssid=None, password=None):
    return argparse.Namespace(port=port, ssid=ssid, password=password)


class TestResolveInputMode(unittest.TestCase):
    def test_empty_string_ssid_and_password_accepted_as_manual(self):
        # Regression: an explicitly-passed --ssid "" --password "" was
        # previously rejected by an `args.ssid and args.password`
        # truthiness check (empty string is falsy), which is
        # indistinguishable from the flags never being passed at all.
        mode, payload = resolve_input_mode(_args(ssid="", password=""))
        self.assertEqual(mode, "manual")
        self.assertEqual(payload, ("", ""))

    def test_neither_port_nor_manual_is_error(self):
        mode, _ = resolve_input_mode(_args())
        self.assertEqual(mode, "error")

    def test_only_ssid_without_password_is_error(self):
        mode, _ = resolve_input_mode(_args(ssid="EPM-SAT-a1b2c3"))
        self.assertEqual(mode, "error")

    def test_port_alone_is_serial_mode(self):
        mode, payload = resolve_input_mode(_args(port="COM5"))
        self.assertEqual(mode, "serial")
        self.assertIsNone(payload)

    def test_port_combined_with_manual_is_error(self):
        mode, _ = resolve_input_mode(_args(port="COM5", ssid="x", password="y"))
        self.assertEqual(mode, "error")


@unittest.skipUnless(_RENDER_DEPS_AVAILABLE, "qrcode/Pillow not installed — see requirements.txt")
class TestRenderLabel(unittest.TestCase):
    def _content_bbox(self, path):
        """Bounding box of non-white content, via PIL only (no numpy)."""
        im = Image.open(path).convert("RGB")
        bg = Image.new("RGB", im.size, "white")
        diff = ImageChops.difference(im, bg)
        return im.size, diff.getbbox()

    def test_empty_node_id_does_not_crash_and_saves_valid_png(self):
        # Regression: node_id="" produced an out_path of just ".png", which
        # pathlib parses as a suffix-less dotfile — Image.save() then
        # couldn't infer PNG from the (empty) extension and raised.
        from generate_label import render_label

        with tempfile.TemporaryDirectory() as tmp:
            out_path = render_label("", "", "", tmp, size_mm=40.0, dpi=300)
            self.assertTrue(out_path.exists())
            size, bbox = self._content_bbox(out_path)
            self.assertGreater(size[0], 0)
            self.assertGreater(size[1], 0)
            self.assertIsNotNone(bbox)

    def test_long_ssid_widens_canvas_instead_of_clipping(self):
        from generate_label import render_label

        long_ssid = "EPM-SAT-" + "f" * 76
        password = "2049231551ac0446bc5357c7a1ab3b83"
        with tempfile.TemporaryDirectory() as tmp:
            out_path = render_label("f" * 76, long_ssid, password, tmp, size_mm=40.0, dpi=300)
            size, bbox = self._content_bbox(out_path)
            # Rightmost non-white pixel must sit inside the canvas with
            # room to spare, not touch/exceed the edge (the clipping bug).
            self.assertLess(bbox[2], size[0] - 1)

    @unittest.skipUnless(_CV2_AVAILABLE, "opencv-python not installed (optional, decode-check only)")
    def test_qr_round_trips_through_a_decoder(self):
        import cv2

        from generate_label import render_label

        ssid, password = "EPM-SAT-a1b2c3", "2049231551ac0446bc5357c7a1ab3b83"
        with tempfile.TemporaryDirectory() as tmp:
            out_path = render_label("a1b2c3", ssid, password, tmp, size_mm=40.0, dpi=300)
            decoded, _points, _ = cv2.QRCodeDetector().detectAndDecode(
                cv2.imread(str(out_path))
            )
            self.assertEqual(decoded, build_wifi_qr_payload(ssid, password))


if __name__ == "__main__":
    unittest.main()
