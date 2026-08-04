---
id: ADR-011
title: MQTT transport added alongside the TCP frame protocol
status: accepted
date: 2026-07-30
deciders: Abhinav Krishna N
---

## Context

Phase 0.5 of the cross-repo interop plan (`docs/MASTER_PLAN.md` Part G) requires
this satellite to join a separate, unmodifiable base station (an Uno Q running
a fixed WiFi AP + Mosquitto broker + Python ingestion pipeline, cloned
read-only as a sibling repo pinned to commit `ab2d89e`) and be accepted by its
pipeline without any change on that side. The existing transport
(`wifi_task.c`: raw TCP to `SERVER_PORT` with a 48-byte header and AES-GCM
framing — ADR-010) speaks an incompatible protocol and cannot reach that
pipeline at all: it has no MQTT client, no section-list frame codec, and no
concept of the base station's `epm/<node_id>/data` / `epm/<node_id>/cmd`
topic scheme.

This ADR adds an MQTT transport alongside the TCP one — additive only. The TCP
path is untouched and still builds/runs; retiring it is deferred to Phase 7
with its own ADR once the base station gateway is the confirmed production
path.

## Findings that shaped the design

Four things surfaced while porting the reference repo's codec/transport code
(`satellite/` in the reference repo) that changed the literal Phase 0.5 task
list:

1. **A frame with only a SCALAR_SET section can never register a node.**
   `mqtt_subscriber.py::normalize_spectrum_message()` returns `None` — a
   documented "normal skip" — whenever `decoded.bins` is empty, which is true
   for any frame with no SPECTRUM sections. No `SensorFrame` is built, so
   `PipelineManager.route()` never runs and the node never appears in the
   dashboard's fleet view. The minimum viable frame is therefore the same
   5-section shape the reference satellite itself emits: 4 SPECTRUM sections
   (mic, accel_x, accel_y, accel_z) + 1 SCALAR_SET section (24 time-domain
   scalars on `TELEM_CHANNEL_PERF`). Verified directly: a hand-built
   scalar-only frame decodes through his `mqtt_subscriber_test.py` unmodified
   with the exact result "scalar-only (no spectrum) frame returns None
   (normal skip): PASS" — confirming the failure mode before committing to
   the 5-section design.
2. **The reference repo's own `telemetry_frame_test.py` does not exercise our
   bytes** — it is a self-contained test of the reference Python codec
   against hand-built payloads of its own. The actual acceptance bar is
   running this firmware's encoder output through the reference pipeline's
   unmodified `decode_frame()` → `normalize_spectrum_message()` →
   `_infer_sensor_config_and_dim()` → `build_feature_vector()` chain, which is
   what `tests/host/decode_check.py` does.
3. **One radio, one AP.** `wifi_task.c` already owns `esp_wifi_init` /
   `esp_wifi_set_config` / `esp_wifi_start` and joins `WIFI_SSID` from
   `wifi_creds.h`. A second `esp_wifi_init()` call aborts. `net_task.c` does
   not touch WiFi itself — it blocks on the existing
   `wifi_wait_connected(portMAX_DELAY)` API and `link_mqtt.c` reads the STA
   MAC via `esp_wifi_get_mac()` only after that call returns. Pointing the
   board at the real Uno Q AP is a `wifi_creds.h` edit at hardware-test time,
   not a code change.
4. **PlatformIO's `src/` is the ESP-IDF "main" component**; there is no
   top-level `main/` directory PlatformIO compiles. The master plan's Part
   C.1 target path `main/threads/net_task.c` becomes `src/threads/net_task.c`
   here — same layering, different name for the same component. Phase 3 is
   expected to rename the tree wholesale.

## Decision

Add four new units, all new code (nothing in `wifi_task.c` is modified):

- **`components/epm_codec/`** — pure C, zero ESP-IDF includes, host-buildable.
  Direct port of the reference repo's `frame_codec/` (`telemetry_schema.h`,
  `wire_protocol.h/.c`, `spectrum_codec.h/.c`). `telemetry_build_frame()`
  encodes the section-list wire format
  (`docs/SENSOR_TELEMETRY_FRAME_PLAN.md` S3): `[num_sections u8]` then, per
  section, `[source_id u8][channel_id u8][data_kind u8][section_len u16][body]`.
  Only porting change from the C++ original: an explicit `#include
  <stdbool.h>` in `wire_protocol.h` (C has no builtin `bool`).
- **`components/epm_hal/include/hal/hal_transport.h`** — the transport
  contract (`transport_init`, `transport_node_id`,
  `transport_publish_spectrum`, `transport_is_connected`,
  `transport_set_cmd_handler`), header-only, zero ESP-IDF includes.
- **`components/epm_drivers/link_mqtt.c`** — the one implementation of that
  contract, behind ESP-IDF's `esp-mqtt` component. Derives the node id from
  the last 3 octets of the STA MAC (lowercase hex, no separators — same
  derivation as the reference repo's `transport_task.cpp`), builds
  `epm/<node_id>/data` and `epm/<node_id>/cmd` topics, and drives
  `esp_mqtt_client_config_t`'s nested-struct config (IDF 5.4):
  `broker.address.{hostname,port,transport}`, `credentials.client_id`,
  `network.reconnect_timeout_ms`, `buffer.{size,out_size}`,
  `task.stack_size`. Publishes data at QoS 0 (occasional loss acceptable for
  a high-rate telemetry stream); subscribes to the cmd topic at QoS 1 and
  decodes `MQTT_MSG_TYPE_STATUS_LED` (the only defined command today) via
  `mqtt_decode_message()`, logging it — real LED wiring is Phase 3's job.
- **`src/threads/net_task.c`** — blocks on `wifi_wait_connected(portMAX_DELAY)`,
  starts `link_mqtt_start()`, then builds and publishes one 5-section frame
  every `EPM_NET_PUBLISH_INTERVAL_MS` (200 ms, matching the reference
  satellite's `FUSER_EPOCH_MS`). Bin/scalar values are synthetic this phase
  (a slow-moving decaying-ramp shape, non-zero so the dashboard's spectrum
  chart is visibly alive) — real mic/accel data replaces them in Phase 6.
  Only the channel layout must stay fixed: the base station's registry
  commits `sensor_config`/`input_dim` from this node's first frame and
  silently drops every later frame that doesn't match it exactly
  (`manager.py::_validate_frame_bins`).

Wiring is additive only: `src/CMakeLists.txt` gained `threads/net_task.c` in
`SRCS` and `epm_hal epm_drivers epm_codec` in `REQUIRES`; `src/main.c` gained
one `net_task_start()` call after `wifi_task_start(...)`. No existing line was
removed or reordered.

## Consequences

**Positive:**
- Interop with an unmodifiable base station is now possible without touching
  its code, verified against its actual pipeline (not just its own tests).
- The TCP path keeps working unchanged — no regression risk to the current
  gateway, and no forced cutover before the MQTT path is hardware-proven.
- `epm_codec` is host-testable in isolation (zero ESP-IDF includes), so the
  wire format can be regression-tested without hardware or a full firmware
  build.

**Negative / trade-offs:**
- Two transports now run concurrently on the same board, each with its own
  task, buffers, and (for TCP) AES-GCM context — more RAM/flash pressure
  until Phase 7 retires one. Flash usage after this change is 94.3% of the
  8 MB app partition on a clean build — comfortable today but leaves little
  headroom before Phase 7.
- `EPM_MQTT_BROKER_HOST`/`PORT` default to placeholder values
  (`10.42.0.1:1883`) pending the real Uno Q's address; a `wifi_creds.h` /
  `link_mqtt.c` edit is required before the real hardware test.
- The synthetic frame's values are not diagnostic — this phase only proves
  the transport and frame shape, not sensor fidelity.

## Validation

- `tests/host/test_frame_encode.c` — host-buildable (gcc, no ESP-IDF), builds
  the 5-section frame via `components/epm_codec/`, asserts the encoded length
  is exactly 2251 bytes and `num_sections == 5`. `RESULT: PASS`.
- `tests/host/decode_check.py` — imports the reference repo's unmodified
  Python pipeline read-only via `PYTHONPATH` and runs `frame.bin` through
  `decode_frame()`, `normalize_spectrum_message()` (asserts non-`None`),
  `manager._infer_sensor_config_and_dim()` (asserts
  `sensor_config == {mic, accel_x, accel_y, accel_z}`, `input_dim == 536`),
  and `features.build_feature_vector()`. `RESULT: PASS`.
- The reference repo's own `base-station/tests/telemetry_frame_test.py` and
  `mqtt_subscriber_test.py`, run unmodified as a reference-health check: both
  print `RESULT: PASS`.
- `pio run -e xiao_esp32s3` — clean build succeeds; `epm_codec`, `epm_hal`,
  `epm_drivers`, and `src/threads/net_task.c` all compile with zero warnings
  (the build's one `-Wcpp` warning is a pre-existing, unrelated `#warning` in
  `imu_task.c` flagging the IMU driver as a stub).
- Real hardware (real SSID/broker IP, on-device MQTT connect/publish/cmd
  round-trip, and appearance in the base station's live dashboard) is a
  follow-up once the real Uno Q's SSID/password/broker address are available.

## Addendum (2026-08-03): Windows Mobile Hotspot ruled out as the temporary stand-in base station

Phase 0.5b (`docs/PHASE_0.5B_LAPTOP_AS_BASESTATION_PROMPT.md`) used the
laptop's own WiFi Mobile Hotspot plus a native Mosquitto broker as a temporary
stand-in for the real Uno Q base station, since that hardware was not yet
available. This was config-only: `wifi_creds.h`'s SSID/password and
`EPM_MQTT_BROKER_HOST` pointed at the hotspot instead of the real base
station, with no firmware structure changes.

The XIAO reliably joined the hotspot's WiFi and obtained a DHCP lease, but the
MQTT/TCP handshake to the broker on the hotspot gateway (`192.168.137.1`)
almost never completed. A long troubleshooting arc empirically falsified
McAfee, Cloudflare WARP, LetsVPN, Windows Defender Firewall, ICS's own
firewall policy, a stale hotspot session, WiFi power-save, and WiFi
band-mismatch as causes.

Direct packet-level evidence (Windows' built-in `pktmon`, filtered on port
1883 and the XIAO's MAC, captured across a full connection-retry cycle)
settled it:

- Windows' driver stack correctly constructs and transmits every reply frame
  (SYN-ACK, bare ACK, MQTT CONNACK) all the way down to the WiFi 802.11 radio
  layer, repeatedly, on every attempt. No firewall/WFP/ICS drop of any kind
  was ever logged for this traffic — the earlier config theories are not just
  unconfirmed but directly disproven.
- The XIAO's uplink direction works well enough to complete a TCP handshake
  and deliver a real MQTT CONNECT payload to the broker (confirmed by
  Windows correctly ACKing it and flagging retransmits as
  `duplicate segment`, meaning the original bytes were fully received).
- The XIAO's inbound path, however, does not reliably act on Windows' replies
  after the initial SYN-ACK: it never advances its ACK pointer past the
  CONNECT payload, keeps retransmitting the same CONNECT bytes, and Windows
  keeps retransmitting the unacknowledged CONNACK in turn — until the XIAO's
  own ~10 s connect timeout fires, it closes the socket, and retries on a new
  port, indefinitely.

This is consistent with a known category of downlink-delivery interop gap
between Windows' Wi-Fi-Direct-based Mobile Hotspot implementation and certain
client WiFi chipsets — not a Windows configuration problem, and not fixable
from this side.

**Decision:** pivot the temporary stand-in base station from the laptop's
Mobile Hotspot to the laptop connected to a real home WiFi router, keeping
Mosquitto and the firmware unchanged. This sidesteps the Wi-Fi Direct virtual
adapter entirely by using a real hardware AP. The revert path when the real
Uno Q arrives is unchanged: the same two config values
(`wifi_creds.h`'s SSID/password, `EPM_MQTT_BROKER_HOST`).

## Addendum (2026-08-04): remaining stand-ins exhausted; real-hardware validation deferred

Three further attempts followed the Mobile Hotspot finding above, using the
home WiFi router as the stand-in base station instead:

1. **Router, laptop and XIAO far apart** — WiFi joined, but MQTT/TCP never
   completed; RSSI logged at -77 to -83 dBm. Weak signal was the leading
   hypothesis at the time, but this was superseded by finding 3 below before
   being independently confirmed.
2. **iPhone Personal Hotspot** — WiFi joined reliably (RSSI as strong as
   -55 dBm), but MQTT/TCP never completed. Root-caused via comparative ping
   tests from the laptop: 100% loss to the XIAO, 0% loss to the hotspot
   gateway and the internet over the same interface — the standard signature
   of iOS Personal Hotspot **client isolation** (the iPhone forwards
   hotspot-client traffic only to itself/the internet, never between two
   connected clients). A platform-level restriction, not fixable from the
   Windows or firmware side.
3. **Router, laptop and XIAO moved physically close together** — ruling out
   distance as the cause of finding 1. The first attempt at this range
   coincided with an unrelated household power interruption (confirmed live:
   the firmware's own `wifi_task: Still waiting for WiFi...` log showed the
   `WIFI_CONNECTED_BIT` was genuinely unset for an extended period, while a
   stale DHCP lease made an unrelated device answer ICMP pings on the XIAO's
   old IP) and was discarded rather than used as evidence. After the outage
   cleared and the XIAO was power-cycled for a clean boot, the test was
   repeated and gave an unconfounded result: WiFi joined at RSSI **-55 dBm**
   (`wifi:connected with MUTHIYATTIRI 2.4GHz, ... rssi: -55`, logged
   immediately following a reconnect) — well above the -60 dBm threshold
   taken as "strong enough" — with a freshly assigned IP
   (`192.168.1.7`). MQTT (port 1883) and the raw TCP path (port 5100) both
   still failed with the same `select() timeout` / `Error transport connect`
   / `connect() ... timed out after 10 s (errno=119)` pattern seen at
   distance. A comparative-ping diagnostic from the laptop (on Ethernet,
   `192.168.1.5`) mirrored finding 2 exactly: 75-100% loss to the XIAO's IP
   (mostly `Request timed out`, one `Destination host unreachable` — an ARP
   resolution failure), 0% loss to the router gateway (`192.168.1.1`) over
   the same interface. With both distance and the power interruption ruled
   out, the leading explanation is that this router applies **AP/client
   isolation between its WiFi and wired Ethernet segments** (a common
   feature on consumer/ISP-supplied routers, sometimes on by default) — the
   laptop is wired, the XIAO is on WiFi, and the router never forwards
   traffic between them. This is a router admin-panel setting, in principle
   checkable/fixable outside of firmware or host-OS changes.

   Confirmed with one further test at RSSI **-35 dBm** (laptop and XIAO
   relocated to ~1 m from the router, effectively the strongest signal the
   radio can report): WiFi joined normally, but MQTT/TCP failed with the
   identical pattern, and the comparative ping test again showed 75% loss to
   the XIAO's IP against 0% loss to the gateway on the same interface. This
   rules out signal strength entirely — the failure is unchanged across an
   80+ dB range of measured RSSI (-83 dBm to -35 dBm).

   **Correction:** the "WiFi vs. wired Ethernet segments" framing above was
   itself confounded. A code review of the laptop's network state (prompted
   by a general recheck of the WiFi/MQTT code and host setup) found the
   laptop's Ethernet adapter was actually `Disconnected` throughout this
   entire finding, while Windows had not purged its stale DHCP lease
   (`192.168.1.5`) or default route from that dead interface — so
   `EPM_MQTT_BROKER_HOST`/`SERVER_IP` had been pointing at a dead NIC the
   whole time, and the laptop's real, live connectivity was via WiFi
   (`192.168.1.8`) on the same router throughout. This was a genuine
   configuration bug, now fixed: both `wifi_creds.h`'s `SERVER_IP` and
   `platformio.ini`'s `EPM_MQTT_BROKER_HOST` were changed to the laptop's
   live WiFi IP (`192.168.1.8`), and the firmware rebuilt/reflashed and
   confirmed via serial log to target the correct host/port.

   Retesting with this corrected config — laptop and XIAO now both
   genuinely WiFi clients on the same AP, live IPs on both ends
   (`192.168.1.8` and `192.168.1.7`), strong signal — reproduced the
   **identical failure**: MQTT/TCP timed out with the same
   `select() timeout` / `Error transport connect` pattern, and
   `ping 192.168.1.7` from the laptop's own WiFi interface returned
   `Destination host unreachable` / request-timed-out on all 4 attempts,
   while `ping 192.168.1.1` (gateway) was 0% loss. This rules out the
   stale-Ethernet-route theory as the cause and is the cleanest test run
   yet — same interface type on both ends, no routing ambiguity — and it
   points to genuine **WiFi client-to-client isolation on this AP**
   (isolating two of its own wireless clients from each other, a common
   default alongside or instead of wired/wireless isolation on
   consumer/ISP routers), superseding the earlier "wired vs. wireless
   segment" hypothesis. A separate review of `wifi_task.c` and
   `link_mqtt.c` end-to-end, plus the laptop's Mosquitto process and
   Windows Firewall rules, found no bugs and no misconfiguration on our
   side beyond the stale-IP issue just described — Mosquitto was listening
   on `0.0.0.0:1883` throughout, and all `EPM*`-named firewall rules were
   correctly scoped (`Profile: Any`, `Enabled: True`, correct ports,
   `RemoteAddress: Any`). No further stand-ins were attempted per the
   decision below.

## Addendum (2026-08-04, cont'd): router config verified clean; failure isolated to the TCP handshake itself

The "WiFi client-to-client isolation" hypothesis above was checked directly
against the router's admin panel (KFON/Alphion, WLAN → wlan1 2.4GHz):
**Client Isolation is Disabled**, Access Control has no MAC filtering, and
Firewall → IP/Port Filtering is default-Allow with no active rules. So that
specific mechanism is ruled out as the cause — the earlier conclusion was an
inference from symptoms, not a confirmed setting, and the router itself is
not configured to isolate its WiFi clients.

Retested at the same close range (RSSI -31 to -39 dBm) with a fresh XIAO IP
pulled live from the serial log (`192.168.1.7`) and the laptop confirmed via
`netsh wlan show interfaces` to be associated to the identical BSSID
(`90:67:17:02:b1:89`) as the XIAO — i.e. definitively the same AP, not a
guest/secondary SSID split:

- `ping 192.168.1.7` from the laptop: **0% loss**, ARP resolved
  (`1c-db-d4-5a-b0-04`). ICMP now succeeds cleanly in both directions —
  unlike every earlier attempt at this router.
- MQTT/TCP still fails with the same `connect() ... timed out after 10 s
  (errno=119)` / `esp-tls: select() timeout` pattern on the XIAO.
- `Get-NetTCPConnection -LocalPort 1883` on the laptop, captured while the
  XIAO was actively retrying, showed `192.168.1.8:1883 <- 192.168.1.7:56247
  State: SynReceived`. This means Windows accepted the XIAO's inbound SYN
  and issued a SYN-ACK at the kernel level (a stateful firewall allow
  decision was already made — Windows Firewall does not gate the SYN-ACK of
  an already-accepted connection) — yet the XIAO's own log shows it never
  received a SYN-ACK at all (`sel=0`, no fd became writable within the 10 s
  timeout).

So the failure is not a blanket link-layer isolation (ICMP proves the AP
bridges unicast frames between these two WiFi clients just fine) — it is
specific to completing a TCP handshake between them. A relevant pattern
across this investigation: the two cases where the handshake stalled this
same way (Windows Mobile Hotspot, and this home router at close range) both
had **Mosquitto running on this Windows laptop**; the AP hardware differed
(a Windows virtual Wi-Fi Direct adapter vs. a real hardware router) but the
signature was identical (SYN-ACK apparently sent by Windows, never received
by the XIAO). This points at the Windows-hosted broker/NIC side (TCP/IP
stack or NIC offload behavior are the usual suspects for this kind of
silent SYN-ACK loss) as a plausible common denominator, rather than
something specific to either AP. The real Uno Q hosts Mosquitto on Linux,
not Windows — so this may be an artifact of the Windows-hosted stand-in
broker rig specifically, and may not reproduce at all against the real
target hardware. This was not pursued further (would require a packet
capture on the laptop's WiFi NIC to confirm whether the SYN-ACK actually
leaves the machine) since it would only characterize a temporary test rig,
not the real target.

**Decision:** all four temporary stand-ins for the Uno Q base station have
now been tried, each hitting a distinct, environment-specific link-layer
failure:

1. **Windows Mobile Hotspot** — Wi-Fi Direct driver bug (confirmed via
   `pktmon` packet capture showing the downlink delivery failure).
2. **Home router, laptop and XIAO far apart** — weak RSSI (-77 to -83 dBm);
   superseded by findings 3/4 below before being independently confirmed on
   its own.
3. **iPhone Personal Hotspot** — iOS Personal Hotspot client isolation
   (confirmed via comparative ping: 100% loss to the XIAO, 0% loss to
   gateway/internet over the same interface).
4. **Home router, close range** — TCP handshake stalls despite a
   confirmed-clean router config (Client Isolation off, no ACL/firewall
   rules) and working bidirectional ICMP; most plausibly a Windows-hosted
   broker/NIC artifact specific to this stand-in rig rather than an AP or
   firmware issue.

None of these four are fixable from the firmware or Windows side within the
scope of a temporary stand-in. Real-hardware validation of the MQTT
transport (WiFi join → MQTT CONNECT/CONNACK → subscribe → periodic publish,
observed live against a real broker) is **formally deferred until the real
Uno Q base station hardware is available**. No further temporary stand-ins
will be attempted. `tests/host/decode_check.py`'s existing pass — a
byte-correct encoding verified against the reference-repo maintainer's real, unmodified decode
pipeline — remains sufficient evidence that the MQTT transport code itself
(framing, codec, topic scheme) is correct and independent of this
environment issue; only the live radio join against a real device is
unverified, and that requires the real base station rather than another
consumer-router or Windows-hosted stand-in.

This does not block progress: Phase 0.5's host-side validation
(`tests/host/decode_check.py`, recorded in this ADR's Validation section)
already proves this firmware's actual encoder output decodes byte-correct
through the reference repo's unmodified pipeline —
`sensor_config == {mic, accel_x, accel_y, accel_z}`, `input_dim == 536`.
That evidence is sufficient to proceed to later phases; only the live
network/broker round-trip on real hardware remains outstanding, gated on the
real Uno Q.
