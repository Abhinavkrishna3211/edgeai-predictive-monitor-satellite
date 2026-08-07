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
