---
id: ADR-033
title: Bench-time QR label tool prints the AP's captured password rather than deriving it
status: accepted
date: 2026-08-08
deciders: Abhinav Krishna N
---

## Context

`components/epm_drivers/provisioning.c`'s `hal_provisioning_start()` already
generates a true-random WPA2 password once per device
(`ap_credentials_get_or_create()`, NVS-persisted, per
`docs/decisions/ADR-031-provisioning-ap-random-per-device-password.md`'s
Option C) and logs it every time the device enters provisioning mode:

```c
ESP_LOGI(TAG, "provisioning AP up: ssid=\"%s\" password=\"%s\" (WPA2-PSK, http://192.168.4.1)",
         ap_cfg.ap.ssid, ap_pass);
```

ADR-031's own Consequences section already named the gap this closes:

> No physical label exists yet as a manufacturing step in this project —
> "read the password off a label" is aspirational until an actual labeling
> process exists; until then, the serial log line is the only real
> distribution channel.

This ADR does not touch password generation or the security model ADR-031
decided — that decision is correct as designed and out of scope here. It
only decides how the already-generated password gets from "one serial log
line a technician can read" to "a physical label an installer can use
without a laptop," and does not require any firmware change.

## Options considered

### Option A: shared-secret derivation scheme
Compute the password from something both the device and an external label
generator can derive independently (e.g. a manufacturing-time master key
plus `node_id`), so a label could in principle be generated without ever
capturing the device's actual runtime output.

Rejected: this is exactly the class of design ADR-031's Option B already
rejected for the password's *generation*, and it doesn't get better by
moving it to a *labeling* tool. A shared master key that can regenerate
any device's password, if it ever leaks, compromises every device at once
— the opposite of ADR-031's whole point, which was to make the password a
true RNG output with no formula an attacker could reconstruct. There is no
way to build a "derive the password for a label" tool without either
reintroducing a master secret or reducing to Option B, so this option is
incompatible with ADR-031 by construction, not just undesirable.

### Option B: capture-and-print at bench time (chosen)
A bench tool reads the actual generated password off the device — either
by parsing it live from the serial log line above, or from a technician
typing in values already read off a console by hand — and renders it onto
a label. Nothing is derived; the tool only ever prints what
`ap_credentials_get_or_create()` already generated and NVS already
persisted.

This preserves ADR-031's true-randomness guarantee exactly: the label tool
has zero knowledge of how the password was produced and holds no secret
that, if leaked, affects any device other than the one the label is
physically stuck on. The operational cost is a one-time bench step per
device (already true today, minus the label) rather than new device-side
work.

## Decision

**Option B.** `tools/provisioning_label/generate_label.py` captures the
SSID/password either live over serial (parsing `provisioning.c`'s existing
log line format) or from manual `--ssid`/`--password` input, and renders a
label PNG per device.

**Label format: a standard `WIFI:` QR code plus human-readable fallback
text.** The QR payload is `WIFI:T:WPA;S:<ssid>;P:<password>;;`, the format
essentially every modern phone camera already recognizes natively (no app
required) to prompt "Join this WiFi network?". This was chosen over
printing the password as plain text alone because the AP password is a
32-character random hex string (`AP_CRED_PASSWORD_LEN`,
`components/epm_drivers/include/drivers/ap_credentials.h`) — typeable, but
error-prone by hand, and a QR scan is both faster and less failure-prone
for an installer than transcribing 32 hex characters into a WiFi settings
screen. The human-readable node id/SSID/password text is kept on the label
alongside the QR as a fallback for the case the QR itself is damaged,
smudged, or unscannable.

The QR-payload builder backslash-escapes `\`, `;`, `,`, `:`, `"` inside the
`S:`/`P:` fields per the `WIFI:` format spec, even though
`provisioning.c`'s actual SSID alphabet (`EPM-SAT-` + 6 lowercase hex
chars) and password alphabet (32 lowercase hex chars) never produce any of
these characters today — defensive correctness against the spec, not a
response to an observed bug.

## Consequences

**Positive:**
- Closes the labeling-process gap ADR-031's Consequences section flagged,
  with zero changes to `provisioning.c`'s password generation/security
  model.
- No new secret-holding component anywhere in the system: the label tool
  is a dumb capture-and-render step, not a second source of truth for any
  password.
- Standard `WIFI:` QR format means no companion app or documentation is
  needed for whoever installs the unit — a phone camera already handles
  it.

**Negative / trade-offs:**
- Still a manual bench step per device (run the tool, print, apply the
  label) — this ADR does not automate manufacturing; it only makes the
  step tractable that ADR-031 had explicitly left as a future process to
  build.
- Serial auto-capture depends on `provisioning.c`'s log line format
  staying `ssid="..." password="..."` — a future change to that log
  string's wording would silently break `parse_log_line()`'s regex (it
  would fall through to "no match" and keep waiting rather than mis-parse,
  per `tools/provisioning_label/generate_label.py`'s `--timeout`, but the
  tool would need a matching update). The manual `--ssid`/`--password`
  fallback exists precisely so this failure mode is never a hard blocker
  at the bench.
- **Generated label PNGs contain a real per-device WPA2 password in
  plaintext and must never be committed** — `tools/provisioning_label/labels/`
  (the tool's default output directory) is added to `.gitignore`, matching
  the discipline already applied to `tools/devrig/.env.local`. No
  sample/placeholder label is checked into the repo for the same reason.

## Validation

Exercised locally with a synthetic `--ssid`/`--password` pair (no hardware
serial capture in this session): the QR payload builder's escaping was
checked against the `WIFI:` format spec, and the rendered label PNG was
visually inspected to confirm the QR code, node id, SSID, and password
text all render legibly and the password text does not get clipped at the
label's edge. `tools/provisioning_label/test_generate_label.py` covers the
log-line parser and QR-payload builder as pure-logic unit tests. Serial
auto-capture against real device output is not yet validated on hardware —
left for the first real bench run.
