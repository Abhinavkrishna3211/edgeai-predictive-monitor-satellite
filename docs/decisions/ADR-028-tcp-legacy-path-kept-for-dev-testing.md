---
id: ADR-028
title: TCP+AES ingestion path relocated to gateway/ingestion/tcp_legacy.py, kept for dev/test only
status: accepted
date: 2026-08-06
deciders: Abhinav Krishna N
---

## Context

Phase 8b3 splits `mic_tools/recv_verify.py` into the new `gateway/{ingestion,pipeline,registry,api,common}` package structure. The TCP+AES wire-format receiver — `FrameDecryptor`, `recv_exact`, `parse_frame`, `satellite_thread`, `accept_loop`, plus the wire-format constants (`EPM_MAGIC`, `HELLO_MAGIC`, `HEADER_FMT`/`HEADER_SIZE`, `HELLO_FMT`/`HELLO_SIZE`, `EPM_PROTO_V2_MAGIC`) — is the last piece of transport-specific code still living directly in `recv_verify.py`. The MQTT section-list path (Phase 8a) already replaced it as the production ingestion route once firmware moved off raw TCP+AES (`tcp_task.c` deleted Phase 7a). The only remaining consumer of the TCP+AES receiver is `mic_tools/satellite_sim.py`, used for manual local testing against a simulated satellite.

Three shapes were available for this code during the restructure:

1. **Delete it outright** — the firmware no longer speaks this protocol in production, and MQTT is the live path.
2. **Relocate it into `gateway/ingestion/` as a first-class peer of `mqtt_subscriber.py`** — implying it's an equally-supported, currently-live ingestion route.
3. **Relocate it into `gateway/ingestion/` under a name that signals dev/test-only status**, keeping it working (satellite_sim.py still needs it) without implying production parity with MQTT.

## Decision

**Option 3.** The TCP+AES receiver moves to `gateway/ingestion/tcp_legacy.py` — deliberately not `tcp_subscriber.py` or any name suggesting parity with `mqtt_subscriber.py`. Everything moves except `_log_security_event`, which stays in `recv_verify.py` because it's called from both `tcp_legacy.py`'s `satellite_thread` *and* the shared `_process_satellite_frame` (a transport-agnostic function neither ingestion path owns) — moving it would have created a reverse dependency of the shared pipeline step on one specific transport's module.

Reasoning:

- Deleting it (`Option 1`) would break `satellite_sim.py`, the only tool that lets a developer exercise the full alerting/HST/fusion/CSV pipeline end-to-end without physical hardware or a running MQTT broker. No replacement for that workflow exists yet on the MQTT side (`satellite_sim.py` doesn't speak MQTT), so deleting the receiver it depends on would be a net loss of dev/test capability with no corresponding gain — the restructure's mandate is organizing code, not removing working functionality that's still in active use.
- Naming it as a peer of `mqtt_subscriber.py` (`Option 2`) would misrepresent its status to anyone reading `gateway/ingestion/`'s file listing — it is not a second production ingestion route a deployer should choose between; it's the vestige of a protocol the firmware has already moved off. `tcp_legacy.py` makes that unambiguous at a glance, matching how the wire-format constants' own docstrings already describe this path as dev/test-only.
- Keeping the code physically working (rather than stubbing or deleting) costs nothing at runtime — it's dead weight only in the sense that production traffic never reaches it, not in the sense that it's broken or unmaintained-looking code left to rot. `gateway/main.py` still wires `accept_loop` to a background thread unconditionally, exactly as `recv_verify.py`'s old `main()` did, so a satellite (real or simulated) connecting via raw TCP+AES to a running gateway continues to work unchanged.

## Consequences

**Positive:**

- `satellite_sim.py`-based manual testing keeps working with zero changes to that script.
- `gateway/ingestion/`'s naming makes the MQTT-vs-legacy status distinction discoverable without reading either file's body.
- The wire-format constants and crypto code move out of `recv_verify.py` entirely, shrinking it by the exact amount that was transport-specific rather than shared pipeline logic.

**Negative / trade-offs:**

- The gateway still opens a TCP listener and spins up `accept_loop` on every startup, even in deployments that only ever use MQTT — this was already true before Phase 8b3 (the TCP receiver was never made opt-out), so it's not a regression, but it remains a live loose end: a `--tcp-legacy`/`--no-tcp-legacy` flag to make this opt-in would be a reasonable follow-up if the always-on listener becomes a real concern (unused open port, thread overhead), but is out of scope for a structural-only phase.
- `_log_security_event` staying in `recv_verify.py` while the rest of the TCP-specific code lives in `tcp_legacy.py` means the security-event logging logic is split across two files by a "shared vs. transport-specific" line that isn't visible from either file's name alone — mitigated by an explicit note in both files' docstrings/comments pointing at each other.

**Revisit this ADR if:** MQTT-based simulation tooling is ever built (making `satellite_sim.py` and `tcp_legacy.py` fully redundant), or if the TCP+AES listener's always-on overhead becomes a real operational concern — at that point, deleting `tcp_legacy.py` entirely (Option 1, revisited) becomes the right call.

## Validation

Design decision only — no hardware dependency. Verified against actual code, not assumption: grepped the full repo for `satellite_thread`, `accept_loop`, `FrameDecryptor`, `parse_frame`, `recv_exact` call sites before finalizing the move, confirming `satellite_sim.py` (manual testing) is the only real external consumer and no test file calls these functions directly (only docstring mentions). Confirmed `_log_security_event`'s two call sites (`tcp_legacy.py`'s `satellite_thread` and `recv_verify.py`'s `_process_satellite_frame`) directly in the code before deciding not to move it. Full test suite re-run after the move: 124 passed, matching the pre-move baseline.
