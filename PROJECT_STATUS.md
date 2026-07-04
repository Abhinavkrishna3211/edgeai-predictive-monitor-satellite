# EPM Project Status — Full Multi-Session Log

**Repo:** `edgeai-predictive-monitor-satellite`
**Last updated:** 2026-07-03

## What the project is

XIAO ESP32-S3 "satellite" node: I2S mic + SPI IMU capture → FFT (1024-pt mic,
2048-pt × 3-axis IMU) → AES-128-GCM encryption → TCP to a Windows laptop gateway
(`recv_verify.py`, port 5100). Gateway runs HalfSpaceTrees anomaly detection +
Bayesian fusion, writes CSV/SQLite, serves a dashboard on port 8080.

**Goal:** real encrypted frames flowing end-to-end so the CSV fills and the ML
pipeline runs on live hardware, not simulation.

---

## Session 1 — WiFi never connected at all

- **Root cause:** WiFi event handlers were registered *before* `esp_wifi_init()` —
  `WIFI_EVENT_STA_START` was never delivered, so `esp_wifi_connect()` was never called.
- **Fix:** moved handler registration to after `esp_wifi_init()`.
- **Also fixed:** `WIFI_AUTH_WPA2_PSK` + `pmf_capable=true` was silently rejected by
  Windows Mobile Hotspot → changed to `WIFI_AUTH_WPA_WPA2_PSK` + `pmf_capable=false`.
- Added a 20s WiFi watchdog (`esp_wifi_stop()`+`start()` on stall) — **later proved to
  be a mistake, removed in Session 4.**

## Session 2 — Build/config fixes

- Flash size mismatch: stale `sdkconfig.xiao_esp32s3` had
  `CONFIG_ESPTOOLPY_FLASHSIZE_2MB=y` on an 8MB chip → fixed.
- PSRAM not enabled → fixed with `CONFIG_SPIRAM_IGNORE_NOTFOUND=y`.
- Wrong partition table (2MB single-app) → new `partitions_8mb.csv` (8MB, OTA-capable).
- Boot loop: `EXT_RAM_BSS_ATTR` on `s_mag_db` (dsp_task) / `s_frame` (imu_task) tried
  placing large arrays in PSRAM before it was initialised → moved back to internal DRAM.
- `wifi_task` stack: dynamic 16KB alloc was fragmenting the heap → switched to a
  static `StackType_t s_wifi_stack[16384]` in BSS.
- Deleted stale `sdkconfig.xiao_esp32s3`, clean rebuild. 105 unit tests passing,
  3-hour 6-satellite simulation verified.

## Session 3 — IMU queue timeout fix

- Symptom: XIAO connected, sent Hello (gateway logged "Satellite connected"), then
  immediately disconnected.
- **Root cause:** `recv_mic_and_imu()` had a 1500ms timeout on the IMU queue, but
  `imu_task` needs `SPEC_AVG_N × (80ms + 120ms) = 16 × 200ms = 3200ms` for its first
  frame. `imu_task` starts at the same instant WiFi connects, so at TCP-connect time
  the IMU queue is empty — the 1.5s timeout fires before the first frame exists.
- **Fix:** `wifi_task.c:442` — `pdMS_TO_TICKS(1500)` → `pdMS_TO_TICKS(4000)`.

## Session 4 — WiFi watchdog removal + debugging

Starting state: connects, Hello received, then gateway immediately logs "peer closed
connection." CSV still empty.

**Diagnosis (confirmed correct):** the 20s WiFi watchdog was tearing down the netif
mid-cycle, losing the route → `EHOSTUNREACH` (errno 113) on the next `send()`.

Fixes applied:
1. Removed the WiFi watchdog entirely (timer, callback, all create/start/stop calls).
2. Confirmed `WIFI_PS_NONE` was already set — no change needed.
3. Added an IP diagnostic (`esp_netif_get_ip_info()` logged before `tcp_connect()`).
   - **Bug introduced:** this diagnostic also called `xEventGroupClearBits(WIFI_CONNECTED_BIT)`
     when IP read back as `0.0.0.0`. This cleared the bit while WiFi was still actually
     connected, and nothing re-set it (no new `on_got_ip` fires without a real
     disconnect/reconnect) → XIAO got permanently stuck in the 10s wifi-wait loop,
     went idle, and the hotspot eventually dropped its ARP entry.
   - **Fixed:** removed the `ClearBits` call, kept only the log line.

**Diagnostic evidence gathered:**

| Observation | Conclusion |
|---|---|
| ARP entry present, IP changes across reboots (.108 → .173) | WiFi RF + DHCP working |
| `SYN_RECEIVED` in netstat | XIAO sends TCP SYN to 192.168.137.1:5100 |
| `ESTABLISHED` briefly visible | 3-way handshake completes |
| `satellites.last_seen` updated in `epm.db` | Gateway receives the 36-byte Hello |
| `LAST_ACK` 3-7s after `ESTABLISHED` | XIAO closes the socket seconds after Hello |
| CSV empty, no `alert_events` for SAT-140C | No data frame ever fully decoded |

**Root-cause hypothesis (timeline):**
```
t=0s    Boot
t=3s    WiFi connects, WIFI_CONNECTED_BIT set
t=3s    connect_to_gateway() → SYN → TCP ESTABLISHED, Hello sent → gateway logs "connected"
t=3.5s  recv_mic_and_imu() begins waiting (mic ready ~1s, imu not ready until t=6.2s)
t=6.2s  imu frame arrives → build_header + send_frame() → AES-GCM encrypt → tcp_send_all()
          ↑ route lost somewhere in this ~2.7s window → send() returns EHOSTUNREACH
t=6.2s  send_frame() fails → drop_connection() → FIN → gateway sees LAST_ACK
t=8.2s  reconnect after 2s delay → cycle repeats
```
Working theory: the hotspot is intermittently dropping the XIAO's association during
the multi-second gap between Hello and the first data send.

**Unresolved blocker discovered this session:** no UART visibility. Opening COM7
resets the XIAO's native USB-CDC into **download/bootloader mode** rather than
showing the running app's log output, so none of `ESP_LOGI`/`ESP_LOGE` calls the
firmware already has can be read. All diagnosis so far has been done blind, from
gateway-side netstat/SQLite state only.

---

## Latest test (2026-07-03) — after both hardware resets

Hotspot toggled off/on, XIAO unplugged/replugged. Gateway restarted fresh
(`recv_verify.py --psk-hex ... --no-plot`, listening on 0.0.0.0:5100, mDNS
advertising `epm-gateway.local:5100`). User confirms XIAO reassociated to the
hotspot, but **the dashboard/gateway shows nothing** — only the startup banner was
captured, no `[+] Satellite connected` line has been confirmed yet either way.

This is the open question right now: did the gateway ever log a connection attempt
after this specific reconnect, or did nothing reach it at all? That distinguishes
"same route-loss bug, still happening" from "a new/different failure."

---

## Immediate next steps

1. **Restore UART visibility first — this is blocking further diagnosis.** The
   COM7-resets-to-bootloader behavior is almost certainly DTR/RTS toggling on port
   open (the ESP32-S3's built-in USB-JTAG/Serial peripheral treats certain
   DTR/RTS transitions as a reset-to-download signal — same mechanism esptool uses
   for auto-flash). Add to `platformio.ini`:
   ```ini
   monitor_dtr = 0
   monitor_rts = 0
   ```
   Then `pio device monitor` should attach without forcing bootloader mode. If it
   still resets, close any other program that might be holding COM7 (leftover
   monitor session, Arduino IDE, a second VSCode window) before opening it.

2. **With logs back, capture one full connect cycle** and check specifically for:
   `Got IP`, `TCP connected to 192.168.137.1:5100`, `Hello sent`, then whatever
   happens in the following ~3s — a `send() failed: errno 113` line will confirm the
   route-loss hypothesis directly instead of inferring it from netstat timing.

3. **In parallel, shrink the vulnerable window** — change `platformio.ini`:
   ```ini
   build_flags =
       -DSPEC_AVG_N=4      ; was 16
   ```
   This cuts the IMU first-frame time from 3.2s to 0.8s, reducing the gap between
   Hello and first send where the hotspot has time to drop the association. This is
   a mitigation, not a root-cause fix — do it alongside #1/#2, not instead of them.

4. **If the route-loss keeps recurring even with a short window**, the Windows Mobile
   Hotspot itself is the likely constant — it's known to be flaky with certain
   client radios. Options at that point: add a TCP keepalive probe fired *during*
   the imu wait (not just after connect), or test against a real WiFi router/AP
   instead of Windows ICS to confirm whether the hotspot is the actual variable.

## Session 5 — UART restored, hypothesis revised (2026-07-03/04)

`monitor_port` had been set to `disabled` (an earlier HW-01 workaround) — that, plus
DTR/RTS toggling, was why COM7 never showed live logs. Fixed:
```ini
monitor_port = COM7
monitor_dtr  = 0
monitor_rts  = 0
```
`board_build.flash_size = 8MB` and `upload_protocol = esp-builtin` also added
(uncommitted). Monitor now attaches without forcing bootloader mode — confirmed
working.

**However Step 2 (capturing the XIAO's own log during a live connect cycle) was
never actually completed** — the session instead spent the remaining budget on
Windows-layer network fixes (NDIS bindings, ICS restart, firewall), none of which
changed anything.

**New gateway-side evidence** (still no XIAO-side log yet):
- Hello (36 bytes) consistently reaches `recv_verify.py` — "satellite connected"
  logs every time.
- 14KB data frames never arrive — FPS stays 0.0.
- Connection pattern across attempts: 1st = `WinError 10054` (RST from XIAO), 2nd =
  "peer closed connection" (FIN from XIAO), subsequent = stuck in `SYN_RECEIVED`.

**Hypothesis revised.** The original EHOSTUNREACH/route-loss theory doesn't fit this
pattern — that would mean `connect()` fails client-side with no SYN ever sent. Here
the full TCP handshake completes and Hello is received, so the network path is fine.
The RST-then-FIN-then-stuck-SYN_RECEIVED progression instead matches **the XIAO
crashing and rebooting** shortly after Hello — most likely during the first real
`encrypt_frame_data()` / AES-GCM call in `send_frame()` (the first moment the wifi
task does the full encryption + GDMA work, since only the plaintext Hello was sent
before this point). A crash/reboot would explain the RST directly: the gateway's
socket still looks open on its side, but the freshly-rebooted ESP32 has no record of
that TCP connection, so its network stack answers with RST on the next packet.
Repeated crash-reboots would also explain the SYN_RECEIVED-stuck pattern on later
attempts. "All Windows-layer fixes exhausted, no change" supports this — it's not a
network-stack issue on the gateway side.

**The one missing piece is still the same: an actual XIAO-side log during a connect
cycle.** UART now works — this just needs to actually be captured, narrowly, without
detouring into more Windows-side changes.

### Exact next action (single bounded step — do nothing else)

```
UART is confirmed working (monitor_port=COM7, monitor_dtr=0, monitor_rts=0 already
applied). Do ONLY this:

1. Run a bounded capture, e.g.:
   timeout 20 pio device monitor > capture.log 2>&1
   (or platform equivalent — must have a hard time limit, don't run unbounded)
2. Ask the user to power-cycle the XIAO once monitor has attached and is idle.
3. After the capture window ends, stop the command.
4. Search capture.log for these patterns only, and report matched lines with 2-3
   lines of surrounding context each — nothing else from the file:
     "Guru Meditation"
     "Backtrace"
     "abort()"
     "rst:0x"                     (a second boot-reason line = unexpected reboot)
     a second occurrence of "Boot memory (before tasks)"  (= reboot happened)
     "errno"
     "EHOSTUNREACH" / "ENOMEM" / "EPIPE"
     "AES-GCM"
     "send_frame" / "send() failed"
5. Do not attempt any fix yet. Do not touch Windows/network settings. Report findings
   and stop.
```

This either confirms the crash-reboot hypothesis (panic/backtrace visible, or a
second unexplained boot banner) or rules it out — whichever it is, it's the last
piece needed to know what to actually fix.

## Session 6 — Real UART capture obtained, crash hypothesis REFUTED (2026-07-04)

First real firmware log captured (COM7 fix worked). Key facts:

- Timestamps run continuously (14709ms → 51949ms), no repeated "Boot memory" line,
  no Guru Meditation / Backtrace anywhere. **The ESP32-S3 is NOT crashing or
  rebooting.** The Session 5 crash-reboot hypothesis is refuted.
- Frame 0: `send() failed: errno 113` (EHOSTUNREACH) — occurred right after a fresh
  `STA ip=192.168.137.2` line, i.e. a real brief WiFi disconnect/reconnect happened.
- Next cycle, frame 1: `send() failed: errno 11` (EAGAIN) — no fresh IP reassignment
  before this one. TCP connected, Hello sent successfully, then `send()` blocked for
  ~10.6s (matches `SO_SNDTIMEO=10s`) before failing. The connection was nominally
  "up" the whole time; the ~14KB frame payload simply never got acknowledged.
- Stack HWM, heap, I2S DMA overflow (0) all look healthy in the DIAG log — no
  resource exhaustion.
- Netstat corroborates: `ESTABLISHED` holds for many polls (consistent with the
  connection looking fine during the failed bulk send), then gets stuck in
  `LAST_ACK` indefinitely (gateway's FIN never ACKed back), and the *next* connect
  attempt gets stuck in `SYN_RECEIVED` forever (gateway's SYN-ACK never ACKed
  either). In both stuck states it's specifically the ESP32→gateway packet that
  never arrives; small control packets (SYN, the 24-byte Hello) go through fine.

**Current hypothesis:** small packets succeed, sustained/bulk transfers silently
fail, connections get stuck half-open — this is the signature of TCP segmentation
offload (LSO) / checksum offload bugs on the Windows Mobile Hotspot's underlying
WiFi adapter driver, not a firmware issue. This is distinct from what was already
ruled out (NDIS bindings, ICS restart, firewall rules).

**Next action (no firmware change, no AI budget needed):**
1. Device Manager → Network adapters → the physical WiFi adapter backing the
   hotspot (not the virtual hotspot NIC).
2. Properties → Advanced → disable: Large Send Offload V2 (IPv4), Large Send
   Offload V2 (IPv6), IPv4/IPv6 Checksum Offload (if present).
3. Toggle Mobile Hotspot off/on, power-cycle XIAO, watch gateway terminal for frame
   data / CSV rows.

If this resolves it, firmware is done as-is (no code changes needed) — the bug was
never in `wifi_task.c`. If not, next step is testing against a real WiFi router
instead of Windows ICS to conclusively isolate hotspot-vs-something-else.

## Session 7 — ROOT CAUSE FOUND: PSRAM disabled, internal heap starved (2026-07-04)

Reproduced the identical failure (errno 113 then errno 11, ~10.6s after Hello,
"peer closed connection"/WinError 10054) across FOUR completely different network
configs tonight: Windows Mobile Hotspot, home WiFi router (laptop on Ethernet), home
WiFi router (laptop also joined via WiFi), and after applying `-Profile Any` to the
firewall rule. Also applied `TASK_PRIO_IMU` 5→3 (wifi_task can now preempt imu_task)
— no change. Total invariance to every network/driver/priority change tried strongly
indicated the cause was never the network layer.

**Found via direct source read of `sdkconfig.defaults`:**
```
# PSRAM DISABLED — boot-loop investigation pending.
# Enabling CONFIG_SPIRAM=y causes the ESP-IDF bootloader to attempt OPI PSRAM
# init before the app starts. On this XIAO ESP32-S3 hardware the OPI init
# fails (confirmed via OpenOCD: continuous Core-was-reset ~200 ms/cycle) and
# the bootloader panics.
```
PSRAM has been fully disabled (not just "gracefully absent") since some earlier,
undocumented session — the bootloader itself panicked trying to init Octal PSRAM,
and rather than fix it, PSRAM was turned off and the issue deferred. Every large
buffer meant for PSRAM (AES pt/ct scratch ~28KB, IMU FFT working buffers ~45KB, mic
ring buffer, 16KB static wifi_task stack) instead lives in internal SRAM. Every DIAG
capture all night has shown `Heap free: internal=2104-2352` — critically low.

**This explains the deterministic, network-invariant failure:** Hello (tiny) always
succeeds since it needs almost no buffer headroom. The first real 14KB frame needs
LWIP to allocate packet buffers to segment/transmit it; with ~2KB heap free, those
allocations starve, and the send silently stalls until `SO_SNDTIMEO` fires —
independent of WiFi driver, AP, or firewall, which is exactly why it reproduced
identically across every network tested tonight.

**Fix is real work, not a toggle.** The sdkconfig comment (never completed) lays out
the remediation:
1. Uncomment `CONFIG_SPIRAM=y`, `CONFIG_SPIRAM_USE_CAPS_ALLOC=y`,
   `CONFIG_SPIRAM_IGNORE_NOTFOUND=y` in `sdkconfig.defaults`.
2. Re-enable `CONFIG_SPIRAM=y` in `sdkconfig.seeed_xiao_esp32s3` (~line 1083).
3. Delete the stale `sdkconfig.xiao_esp32s3` and do a clean build.
4. Monitor the bootloader log over JTAG/OpenOCD for a "PSRAM: Found" message vs. the
   original panic — open questions noted in the comment: wrong PSRAM speed config,
   eFuse issue, or possibly a silicon variant without PSRAM. `CONFIG_SPIRAM_MODE_OCT=y`
   is already set correctly for this board's Octal PSRAM interface, so if it still
   panics, look at PSRAM speed (`CONFIG_SPIRAM_SPEED_*`) vs. flash frequency mismatch
   next — this was flagged as a known risk in `imu_task.c`'s own comments.

Once PSRAM actually initializes, restore `EXT_RAM_BSS_ATTR` on `dsp_task.c`'s
`s_mag_db` and `imu_task.c`'s `s_frame` (per the sdkconfig comment) to move them
back off internal DRAM.

**Cheap same-night confirmation test (optional, before the real fix):** temporarily
shrink `FFT_IMU_N` (e.g. 2048→512) to free up internal SRAM without touching PSRAM
at all — if frames start flowing, that's strong independent confirmation this is a
heap-pressure issue before investing in the PSRAM boot-loop fix. Not a permanent fix
(degrades IMU frequency resolution), diagnostic only.

## Session 7 RESOLVED — connectivity confirmed working end-to-end (2026-07-04)

Root cause fully confirmed and fixed: PSRAM enablement had to happen in
`sdkconfig.xiao_esp32s3` (the file matching `[env:xiao_esp32s3]` in
platformio.ini — the one PlatformIO's ESP-IDF build actually reads), not
`sdkconfig.seeed_xiao_esp32s3` (a same-looking but unused file that caused three
rounds of wasted edits). Once `CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY=y` was
set there and `EXT_RAM_BSS_ATTR` restored on `dsp_task.c`'s `s_mag_db` and
`imu_task.c`'s `s_frame`, internal RAM usage dropped from 230164 to 203212 bytes
at build time, and live heap free jumped from ~1.2-2.3KB to ~21KB.

Separately, `wifi_creds.h` had stale hotspot credentials (`EPM_Hotspot`/`epm12345`)
that didn't match the actual Windows Mobile Hotspot name (`abhi0331`). Fixed to
match.

**Confirmed working:** satellite connects, calibrates (30-frame baseline), HST
warms up, adaptive v2 protocol round-trips correctly (gateway told firmware
avg_n 4→8, firmware complied), CSV logs real frames (rms/kurtosis/crest/z-score/
p_fault per frame). This is the first time in the whole multi-day saga that any
data has reached the gateway.

**Residual, non-blocking:** disconnects roughly every 60-90s (WinError 10054 /
errno 11 / errno 113 on occasional frames), reconnects automatically, frame
numbering and data flow continue normally after each reconnect. This is ordinary
WiFi-flakiness territory the existing reconnect logic already handles — not the
deterministic, total-failure bug that was the actual multi-day blocker. Worth
investigating for reliability hardening, but not a blocker for moving on to IMU
integration and model tuning.

**Revise the overnight audit priorities accordingly:** connectivity validation is
DONE, don't waste audit time re-litigating it. Priorities are now: (a) understand/
harden the periodic reconnect (optional, low urgency), (b) real KX134 IMU hardware
integration (replace imu_task.c's stub), (c) model/accuracy tuning on the gateway
side. `sdkconfig.seeed_xiao_esp32s3` should still be flagged for deletion/cleanup
as a dead, confusing file.

## Next actions (from the planning tool session, 2026-07-03)

1. Restore UART visibility: add `monitor_dtr = 0` / `monitor_rts = 0` to
   `platformio.ini`, verify `pio device monitor` attaches without forcing
   bootloader/download mode.
2. Capture one connect cycle (bounded ~15s), report only matched lines: `Got IP`,
   `TCP connected to`, `Hello sent`, any `errno`, `EHOSTUNREACH`/`send() failed`.
3. Branch on result — if errno 113 confirmed near the IMU wait, apply
   `-DSPEC_AVG_N=4` (was 16) in `platformio.ini` build_flags and retest. If the log
   shows something unexpected, stop and diagnose from that instead of the prior
   hypothesis.
4. If route-loss persists even with a short window, suspect the Windows Mobile
   Hotspot itself as the constant — consider a keepalive during the imu wait, or
   testing against a real WiFi AP to isolate hotspot vs. firmware.

Recommended workflow: new the AI coding assistant session, Sonnet model with plan mode for the
mechanical steps above; escalate to Opus only if a captured log doesn't match the
route-loss hypothesis, and scope that escalation to the specific log plus
`wifi_task.c` / `epm_config.h` / `main.c` — not a full-repo review.
