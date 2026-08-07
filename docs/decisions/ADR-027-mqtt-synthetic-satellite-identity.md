---
id: ADR-027
title: MQTT satellite identity derived from node_id alone, no hello-equivalent invented
status: accepted
date: 2026-08-05
deciders: Abhinav Krishna N
---

## Context

The gateway's satellite registry (`SatelliteState`/`_sat_register()` in `mic_tools/recv_verify.py`) was built entirely around the old TCP+AES transport, where every connection opens with a `HELLO_FMT` handshake (`<I6sBB12s`: magic, 6-byte MAC, fw_major, fw_minor, 12-byte name) *before* any frame arrives. `_sat_register(mac_hex, name, fw_major, fw_minor, addr)` requires all four identity fields up front, and they're used downstream in three rendered views: the console satellite table, the dashboard JSON API, and the dashboard HTML card header.

Phase 8a's MQTT ingestion path (`mic_tools/mqtt_ingest.py`) has no equivalent. `epm/<node_id>/data`'s payload (`telemetry_frame.decode_frame()`) is pure section-list bytes — no magic, no MAC, no firmware version, no device name. The only identity information available anywhere in the MQTT path is `node_id` itself, carried in the topic string (e.g. `epm/5ab004/data`), which `net_task.c` derives from the last 3 octets of the station's real MAC, lowercase hex, no colons (confirmed against ADR-026's own capture: MAC `1c:db:d4:5a:b0:04` → `node_id=5ab004`). This matches the reference base station's own design — its `SensorFrame.node_id` also comes from the topic, and its gateway has no hello-equivalent either. There was never a wire message this could be recovered from; the firmware simply doesn't send one over MQTT.

Two shapes were available:

1. **Invent a second identity-announcement mechanism** — e.g. a retained MQTT message on `epm/<node_id>/hello` publishing full MAC/name/fw version once at boot, mirrored on the gateway side. This would restore full parity with the TCP path's registry fields.
2. **Derive a synthetic identity from `node_id` alone** and register directly under it, accepting that `name`/`fw_major`/`fw_minor` are placeholders rather than real device-reported values.

## Decision

**Option 2.** `mqtt_ingest.py` registers each satellite as `_sat_register(node_id, name=f"sat-{node_id}", fw_major=None, fw_minor=None, addr=(mqtt_host, mqtt_port))` — `node_id` is used directly as the registry's `mac_hex` key, with no attempt to reconstruct or announce the full 6-byte MAC.

Reasoning:

- The firmware has never sent identity data over MQTT and Phase 8a's mandate is an ingestion-boundary swap, not a firmware change — inventing a hello-equivalent would mean adding a new publish call to `net_task.c`, a new topic, a new decode path, and a new gateway-side registration hook, all to recover two integers (`fw_major`/`fw_minor`) and a name string the dashboard only ever displays, never acts on programmatically.
- `node_id` is already a stable, collision-resistant-enough identifier per satellite (derived from a real MAC's low 3 octets) and is exactly what the reference gateway keys satellites by. Diverging from that to build a fuller identity mechanism our own firmware doesn't support would be scope creep with no consumer.
- `node_id` (6 lowercase hex chars, e.g. `5ab004`) and TCP's `mac_hex` (17-char colon-separated uppercase, e.g. `AA:BB:CC:DD:EE:FF`) are visually and structurally distinct — no risk of an MQTT-registered satellite silently colliding with or overwriting a TCP-registered one in `_satellites`.
- `fw_major`/`fw_minor` are set to `None` (not `0`) specifically so they're distinguishable from a real "firmware 0.0" in code — `SatelliteState.fw_str()` (new method) checks for `None` and renders a `"mqtt"` placeholder instead of a numeric version at all three render sites (console table, dashboard JSON `fw` field, dashboard HTML card header), rather than emitting a confusing `"FW 0.0"` that reads as a real, very old firmware build.

## Consequences

**Positive:**

- Zero firmware changes required — Phase 8a stays a pure gateway-side ingestion swap as scoped, no `net_task.c`/`transport_task.cpp` touched.
- No new topic, no new wire format, no new decode path to test or maintain.
- Registry key collision with the TCP path is structurally impossible given the two ID formats' shapes.

**Negative / trade-offs:**

- `name` is a synthetic `sat-<node_id>` string, not a human-assigned device name — an MQTT-registered satellite's dashboard card will show `sat-5ab004` rather than whatever friendly name a TCP-connected satellite would have sent via its hello packet. Anyone renaming satellites for a fleet dashboard will need a separate mechanism (e.g. a config-file name override keyed by `node_id`) if this matters in practice — not built here, no consumer asked for it yet.
- `fw_major`/`fw_minor=None` means per-satellite firmware-version tracking (useful for fleet upgrade audits) is unavailable for any satellite connected only over MQTT. If that becomes a real need, it's still cheapest to add as a retained hello-equivalent message at that point, not to guess at it now.
- If a future need arises to disambiguate multiple physical satellites that happen to produce the same last-3-MAC-octets `node_id` (a real, if unlikely, collision), this design has no way to detect or warn about it — `node_id` is trusted as unique.

**Revisit this ADR if:** a fleet-management need emerges for real per-satellite firmware version tracking or human-assigned names over MQTT — the fix at that point is a small retained-message hello-equivalent, not a redesign of `SatelliteState`.

## Validation

Design decision only — no hardware dependency. Verified against actual code, not assumption: grepped `_sat_register()`'s and `SatelliteState`'s full field usage in `mic_tools/recv_verify.py` (all three `fw_major`/`fw_minor` render sites identified by direct line search: console table, dashboard JSON API, dashboard HTML card), and confirmed `node_id` derivation and format against ADR-026's real hardware capture (`node_id=5ab004` from MAC `1c:db:d4:5a:b0:04`) rather than guessing the shape.
