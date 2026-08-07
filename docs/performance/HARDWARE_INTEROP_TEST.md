# Real Hardware Interop Test — XIAO Satellite vs Reference Base Station

**Date:** 2026-08-07
**Branch:** feat/base-station-interop
**Commit:** 19d530eb2aa4b9d473b63a8a4d587c15f4bc4409

---

## Setup

Real Seeed XIAO ESP32-S3 satellite hardware (node_id `5ab004`) tested
end-to-end against the reference repo's unmodified
`base-station/python/main.py`, run under WSL Python pointed at a native
Windows Mosquitto broker (`192.168.1.7:1883`). No reference-repo files were
modified, per `tools/devrig/README.md`'s own rule.

## Result

- Node `5ab004` registered on the dashboard (`/nodes`) with correct
  `sensor_config`/`input_dim`, and the Fleet view rendered a live
  `accel_x`/`accel_y`/`accel_z` spectrum.
- A remote `STATUS_LED` command sent from the dashboard side was visually
  confirmed to change the satellite's physical LED (steady green -> red
  strobe).

## Soak window

17m45s continuous run (`mosquitto_sub -t 'epm/#' -v`, single unbroken
process, no restarts):

| Topic | Count |
|---|---|
| `epm/5ab004/data` | 4599 |
| `epm/5ab004/cmd` | 2 |

- Zero cross-talk from unexpected topics.
- Zero `WARN`/`ERROR`/`Exception`/`malformed`/`disconnect` matches in the
  dashboard-side log for the full session.

**Throughput:** ~4.3 msg/s against a nominal 5 msg/s (200ms
`EPM_NET_PUBLISH_INTERVAL_MS`). Observed, unexplained at the margins — most
likely attributable to DSP cycle timing rather than transport loss (the 2
successful `cmd` round-trips rule out a congested or dead link), not a
confirmed root cause.

## Known limitation of this test

Broker-side connect/disconnect logging was disabled (`log_dest`/`log_type`
commented out in the native Windows Mosquitto's `mosquitto.conf`) for this
run. The "zero drops" observation above rests on subscriber-side and
dashboard-side evidence only, not the broker itself. Recommend enabling
broker logging before the next soak test.

## What this does NOT prove

This validated the wire protocol and gateway software path against a real
satellite for the first time — not the reference implementation's own
physical hardware. The reference base station's own MCU<->MPU link and
hardware-specific bridges were not exercised.

---

## Addendum: 2026-08-07 — Repeat/Regression Check

**Date:** 2026-08-07
**Branch:** feat/base-station-interop

Repeat of the setup above (real satellite `5ab004` vs. the reference repo's unmodified
`base-station/python/main.py`), run to confirm the wire path still holds after this
session's documentation/diagram/git-hygiene changes. No satellite or gateway code was
touched between the original run and this one.

### Deviations from the original setup

- **Broker path:** connected via `192.168.1.5:1883` (Ethernet), not the `192.168.1.7`
  (WiFi) address in the original entry above. Same broker process, same port — only the
  local NIC differs. At the time of this run the WiFi adapter had no resolved Windows
  network profile, so `.7` was not a live path to test against; `.5` was the address the
  satellite's own connection was already landing on.
- **STATUS_LED step skipped:** this session's node registration was a fresh,
  uncommissioned `5ab004` (unlike the original run's already-commissioned node), and the
  reference dashboard only pushes a `STATUS_LED` command as a side effect of a
  commissioned node's status transitions — it exposes no manual/direct trigger for an
  uncommissioned node. Running the node through full commissioning just to exercise this
  wire path was judged out of scope for a regression check with no LED/firmware code
  changed this session. `epm/5ab004/cmd` traffic was correspondingly zero.

### Result

- Node `5ab004` registered on the dashboard (`/nodes`) with `sensor_config`/`input_dim`
  matching the original run.
- Dashboard process uptime: **~5h43m**, zero `WARN`/`ERROR`/`Exception`/`malformed`/
  `disconnect` matches across the full log. The satellite's broker-side TCP session held
  a single stable ephemeral port (`50041`) for that entire span — no evidence of a
  reconnect at any point.

### Soak window

Dedicated ground-truth capture (`mosquitto_sub -t 'epm/#' -v`, single unbroken process):
**10.5 minutes (22:08:29-22:18:59)**, not the originally intended 15-20 minutes — this
was a timekeeping mistake on the operator side, caught and corrected before reporting,
not a shortened test by design.

| Topic | Count |
|---|---|
| `epm/5ab004/data` | 3001 |
| `epm/5ab004/cmd` | 0 |

**Throughput:** ~4.76 msg/s vs. nominal 5 msg/s (vs. ~4.3 msg/s in the original run).

### Conclusion

Satellite-to-reference-dashboard interop still holds after this session's changes. No
code changed, so this is a confirmation of the original result, not a new validation.
