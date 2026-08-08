# tools/provisioning_label

Generates a printable label (WiFi QR code + human-readable fallback text)
for a satellite's provisioning-AP WPA2 password, so it can be stuck on the
physical enclosure instead of only living in a one-time serial log line. See
`docs/decisions/ADR-031-provisioning-ap-random-per-device-password.md` for
why the password is a true-random per-device value (not derivable from the
SSID), and `docs/decisions/ADR-033-provisioning-password-qr-label.md` for
why this tool exists and why it captures-and-prints rather than
re-deriving the password from anything.

This is bench tooling — it runs on a technician's laptop, not on the
gateway or the satellite itself, and is not installed by CI (see
`requirements.txt`).

## Bench workflow

1. Flash the device.
2. Connect it to your laptop via USB while it boots into provisioning mode
   (first boot with no saved WiFi credentials, or any later re-entry into
   provisioning — see `components/epm_drivers/provisioning.c`).
3. Run the tool against the serial port:
   ```
   python generate_label.py --port COM5
   ```
   It watches the serial output for `provisioning.c`'s
   `provisioning AP up: ssid="..." password="..."` log line, extracts the
   SSID/password, and derives the node id by stripping the `EPM-SAT-`
   prefix from the SSID.
4. If auto-detection doesn't work (wrong port, USB-driver quirk, or you
   already copied the values off a console by hand), pass them directly
   instead — no serial connection needed:
   ```
   python generate_label.py --ssid EPM-SAT-a1b2c3 --password 0123456789abcdef0123456789abcdef
   ```
5. The label PNG is written to `labels/<node_id>.png` (gitignored — see
   below). Print it on adhesive label stock and stick it on the
   enclosure.

## Options

- `--port <serial-port>` / `--baud` (default `115200`) / `--timeout`
  (default `30`s): serial capture mode.
- `--ssid` / `--password`: manual mode, mutually exclusive with `--port`.
- `--output-dir`: defaults to `tools/provisioning_label/labels/`.
- `--size-mm`: QR square edge length in mm (default `40`).
- `--dpi`: render resolution (default `300`, print-quality).

## Security — generated labels are never committed

Each label PNG embeds a real per-device WPA2 password in plaintext (both
in the QR code and as fallback text). The default output directory
(`tools/provisioning_label/labels/`) is gitignored — same discipline as
`tools/devrig/.env.local`. Never commit a label PNG, and never point
`--output-dir` at a path that isn't already covered by `.gitignore`.

## Tests

`test_generate_label.py` covers the two pure-logic pieces (the log-line
parser and the QR-payload builder) with no serial/qrcode/Pillow dependency.
It is not wired into the repo's root `tests/` pytest suite / CI — run it
manually:
```
python -m pytest tools/provisioning_label/test_generate_label.py -v
```
