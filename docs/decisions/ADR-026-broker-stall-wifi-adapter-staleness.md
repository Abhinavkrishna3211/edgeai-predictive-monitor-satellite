---
id: ADR-026
title: MQTT broker stall did not reproduce after a fresh Wi-Fi adapter join — NIC/TCP-stack-artifact hypothesis superseded
status: accepted
date: 2026-08-05
deciders: Abhinav Krishna N
---

## Context

`docs/decisions/ADR-011-mqtt-transport-added.md`'s addenda exhausted four network stand-in topologies (Windows Mobile Hotspot, home router at distance, iPhone hotspot, home router at close range) trying to reproduce a reliable MQTT connection from the XIAO satellite to a Windows-laptop-hosted Mosquitto broker. All four stalled identically (`select() timeout` / no SYN-ACK observed at the client) and the investigation was formally deferred to the real Uno Q base station, with a working hypothesis of "a Windows TCP/IP-stack or NIC-driver artifact." `docs/decisions/ADR-024-esp-mqtt-heap-guard.md`, dated the same day as this ADR, independently reproduced the identical signature against the same broker host (`192.168.1.8`) earlier in this session, describing broker reachability as "verified live, not assumed stale."

This session's trigger was `docs/BROKER_CONNECTIVITY_DEBUG_PROMPT.md`, which proposed two specific, cheap causes never explicitly checked in ADR-011's addenda: Mosquitto's listener binding, and the Windows host's own firewall. Direct inspection ruled out both before any live test: `C:\Program Files\mosquitto\mosquitto.conf` already carries an EPM-added block (dated 2026-07-30) with `listener 1883 0.0.0.0` and `allow_anonymous true` — not loopback-only — and Windows Firewall already has an enabled inbound rule `EPM temporary MQTT 1883` (`Allow`, `Profile: Any`). Neither is the cause.

The one uncontrolled variable found at the start of this session: the laptop's Wi-Fi adapter (Realtek RTL8852AE) was fully disconnected (`Media disconnected`), with only a stale Ethernet IP (`192.168.1.5`) up. This is structurally the same category of bug ADR-011's addenda already root-caused once before — a stale/dead interface holding the IP `EPM_MQTT_BROKER_HOST` was configured to — except that earlier instance was the Ethernet adapter; this one is Wi-Fi.

## Measurement

Rejoined `MUTHIYATTIRI 2.4GHz` (the SSID `src/wifi_creds.h` already configures the firmware to use) via `netsh wlan connect`, landing on `192.168.1.8` — an exact match with `platformio.ini`'s already-configured `EPM_MQTT_BROKER_HOST`, requiring no firmware change.

5 consecutive full hard-reset cycles of the XIAO (`esptool.py --chip esp32s3 --port COM15 run`, an RTS-pin hardware reset — not a reflash) were run against this same broker, each observed via a simultaneous `pio device monitor` (COM15) and an independent `mosquitto_sub -h 192.168.1.8 -t 'epm/#' -v` client:

| Cycle | Connect event captured | Stall signature observed | Data messages in capture window |
|---|---|---|---|
| 1 (initial) | yes — `link_mqtt: connected, subscribed to epm/5ab004/cmd` | one transient write-timeout + auto-reconnect (~2.5s), then clean | n/a (exploratory capture) |
| 2 (60s stability check, same boot as cycle 1) | already connected | none | 286 |
| 3 (fresh hard reset) | connect happened before monitor attach; messages flowing | none | 192 |
| 4 (fresh hard reset) | yes — `link_mqtt: connected, subscribed to epm/5ab004/cmd` at 11.1s uptime | none | 171 |
| 5 (fresh hard reset) | connect happened before monitor attach; messages flowing | none | 191 |

Zero occurrences, across all 5 cycles, of ADR-011/ADR-024's documented stall signature (`esp-tls: [sock=54] select() timeout` → `transport_base: Failed to open a new connection` → `mqtt_client: Error transport connect`). Roughly 840 data messages were delivered in total across capture windows summing to ~220 seconds, all on `epm/5ab004/data` (node id derived from the XIAO's real MAC, `1c:db:d4:5a:b0:04`).

## Decision

**ADR-011's addenda's "Windows TCP/IP-stack or NIC-driver artifact" working hypothesis is superseded (not deleted, per this repo's append-only ADR convention) by "stale/degraded Windows-host Wi-Fi adapter association" as the better-supported explanation** for the historical connectivity stall.

This is recorded strictly as an **observed correlation across 5 reset cycles in one session, not a proven causal mechanism.** Specifically not established by this measurement:

- *Why* a stale/disconnected Wi-Fi adapter produces a silent TCP stall rather than a fast failure — a SYN sent against a torn-down/never-associated interface would normally fail immediately, not time out the way ADR-011's `pktmon` evidence showed. The mechanism connecting "adapter state" to "SYN-ACK apparently sent but never received" is not understood here, only that fixing the adapter state also removed the symptom.
- Whether this is specific to this laptop's Realtek RTL8852AE driver, to this router (BSSID `90:67:17:02:b1:89`), or general to Windows Wi-Fi.
- Whether ADR-024's same-day reproduction happened because its Wi-Fi adapter was in the same stale state this session found at the start. Plausible given the pattern, but that session's network adapter state wasn't captured at the time and can't be reconstructed now.

## Consequences

**Positive:**

- Real-hardware validation against a laptop stand-in is no longer conclusively blocked — Phase 6c/7b/7c's repeated re-confirmations of the "known limitation" may not have been hitting a genuinely unfixable artifact, just this adapter-staleness issue.
- A cheap, repeatable mitigation exists for any future test session: explicitly (re)join Wi-Fi immediately before testing (`netsh wlan connect` or equivalent) rather than trusting an adapter that merely shows as "connected."

**Negative / trade-offs:**

- Does not explain why ADR-011's four stand-in topologies — including "home router at close range," materially similar to this session's setup — failed consistently across multiple prior sessions if adapter freshness were the only variable. It's possible some of those sessions also had stale adapter state that simply wasn't checked at the time; this can't be verified retroactively.
- The formal deferral to the real Uno Q (ADR-011's addenda) is weakened by this ADR, not retracted outright — pending further reproduction attempts that deliberately test the hypothesis from the other direction (stale the adapter on purpose, confirm the stall returns, then confirm a fresh rejoin fixes it again).

**Metrics to watch / revisit this ADR if:**

- A future session reproduces the stall with a Wi-Fi adapter confirmed freshly joined immediately beforehand — this would falsify the hypothesis outright.
- Someone deliberately reproduces the stall by staling the adapter first (e.g., leaving it idle/disconnected for an extended period before reconnecting) and confirms a fresh rejoin fixes it again — this would strengthen the claim from correlation to a demonstrated mechanism, and would justify filing this as a real, named Windows/driver bug rather than a workaround.

## Validation

Hardware was available and used throughout this session (XIAO ESP32S3, COM15, MAC `1c:db:d4:5a:b0:04`). 5 hard-reset cycles via `esptool.py --chip esp32s3 --port COM15 run` (RTS-pin reset, not a reflash), each observed via a simultaneous serial monitor and an independent `mosquitto_sub` client subscribed broker-side. `docs/BROKER_CONNECTIVITY_DEBUG_PROMPT.md`'s Steps 1–2 (Mosquitto listener binding, Windows host firewall) were reconfirmed clean by direct file/rule inspection before any live test, ruling out both as contributing causes.
