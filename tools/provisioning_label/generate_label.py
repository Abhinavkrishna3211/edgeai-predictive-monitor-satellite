#!/usr/bin/env python3
"""
generate_label.py — Prints a scannable WiFi-QR + human-readable label for a
satellite's provisioning AP (see docs/decisions/ADR-031 for password
generation, ADR-033 for why this tool exists).

Two ways to get the SSID/password:
  --port <serial-port>    read them live off the device's provisioning log
                           line (components/epm_drivers/provisioning.c)
  --ssid / --password     manual fallback when a technician already read
                           them off a console by hand, or auto-detection
                           hits USB-driver quirks

Usage:
    python generate_label.py --port COM5
    python generate_label.py --ssid EPM-SAT-a1b2c3 --password 0123456789abcdef0123456789abcdef
"""

import argparse
import re
import sys
from pathlib import Path

SSID_PREFIX = "EPM-SAT-"

# Matches provisioning.c's ESP_LOGI format string verbatim:
#   provisioning AP up: ssid="EPM-SAT-a1b2c3" password="..." (WPA2-PSK, http://192.168.4.1)
LOG_LINE_RE = re.compile(r'ssid="([^"]+)"\s+password="([^"]+)"')


def parse_log_line(line):
    """Extract (ssid, password) from a provisioning.c log line.

    Returns None if the line doesn't match — callers should keep reading
    further lines rather than treat that as fatal (serial output is noisy:
    boot banners, unrelated ESP_LOG lines, partial reads).
    """
    match = LOG_LINE_RE.search(line)
    if not match:
        return None
    return match.group(1), match.group(2)


def node_id_from_ssid(ssid):
    """Strip the EPM-SAT- prefix provisioning.c's snprintf() adds."""
    if ssid.startswith(SSID_PREFIX):
        return ssid[len(SSID_PREFIX):]
    return ssid


def _escape_wifi_field(value):
    """Backslash-escape chars the WIFI: QR format reserves inside S:/P: fields."""
    out = []
    for ch in value:
        if ch in ('\\', ';', ',', ':', '"'):
            out.append('\\')
        out.append(ch)
    return ''.join(out)


def build_wifi_qr_payload(ssid, password):
    """Build the standard WiFi QR payload string (WPA/WPA2, no hidden-network flag)."""
    return "WIFI:T:WPA;S:{};P:{};;".format(
        _escape_wifi_field(ssid), _escape_wifi_field(password)
    )


def read_from_serial(port, baud, timeout):
    """Open a serial port and block until a provisioning log line is seen.

    Raises TimeoutError if nothing matches within `timeout` seconds.
    """
    import serial  # lazy: only needed for --port, keeps pure-logic tests dep-free

    with serial.Serial(port, baud, timeout=timeout) as ser:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            parsed = parse_log_line(line)
            if parsed:
                return parsed

    raise TimeoutError(
        "no provisioning log line seen on {} within {}s — is the device in "
        "provisioning mode?".format(port, timeout)
    )


def render_label(node_id, ssid, password, output_dir, size_mm, dpi):
    """Render a QR + human-readable fallback text label PNG, save it as
    <output_dir>/<node_id>.png, and return the saved path."""
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    payload = build_wifi_qr_payload(ssid, password)

    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    qr_px = round(size_mm / 25.4 * dpi)
    qr_img = qr_img.resize((qr_px, qr_px), Image.NEAREST)

    margin = round(qr_px * 0.08)
    text_lines = ["Node: {}".format(node_id), "SSID: {}".format(ssid), "Pass: {}".format(password)]
    line_height = round(qr_px * 0.09)
    text_block_h = margin + line_height * len(text_lines) + margin

    font_size = max(10, round(line_height * 0.7))
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    longest_line_w = max(scratch.textlength(line, font=font) for line in text_lines)
    canvas_w = max(qr_px, round(longest_line_w) + 2 * margin)

    canvas = Image.new("RGB", (canvas_w, qr_px + text_block_h), "white")
    canvas.paste(qr_img, ((canvas_w - qr_px) // 2, 0))

    draw = ImageDraw.Draw(canvas)
    y = qr_px + margin
    for text_line in text_lines:
        draw.text((margin, y), text_line, fill="black", font=font)
        y += line_height

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "{}.png".format(node_id)
    canvas.save(out_path, dpi=(dpi, dpi))
    return out_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="Serial port to read the provisioning log from")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=30, help="Seconds to wait for the log line")
    parser.add_argument("--ssid", help="Manual SSID (skips serial capture)")
    parser.add_argument("--password", help="Manual password (skips serial capture)")
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent / "labels"),
        help="Directory to save the label PNG into (default: ./labels, gitignored)",
    )
    parser.add_argument("--size-mm", type=float, default=40.0, help="QR square edge length in mm")
    parser.add_argument("--dpi", type=int, default=300)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    manual = args.ssid or args.password
    if args.port and manual:
        print("error: --port cannot be combined with --ssid/--password", file=sys.stderr)
        return 2
    if args.port:
        ssid, password = read_from_serial(args.port, args.baud, args.timeout)
    elif args.ssid and args.password:
        ssid, password = args.ssid, args.password
    else:
        print("error: pass either --port, or both --ssid and --password", file=sys.stderr)
        return 2

    node_id = node_id_from_ssid(ssid)
    out_path = render_label(node_id, ssid, password, args.output_dir, args.size_mm, args.dpi)
    print("wrote {}".format(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
