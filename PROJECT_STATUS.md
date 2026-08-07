# EPM Project Status — Full Multi-Session Log

**Repo:** `edgeai-predictive-monitor-satellite`
**Last updated:** 2026-07-04

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

## Next actions (from planning-tool session, 2026-07-03)

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

Recommended workflow: new AI-assistant session, the standard build model with plan
mode for the mechanical steps above; escalate to the higher-capability model only if
a captured log doesn't match the route-loss hypothesis, and scope that escalation to
the specific log plus `wifi_task.c` / `epm_config.h` / `main.c` — not a full-repo
review.

## Session 8 — planning-tool audit verification + Phase 1 fixes committed (2026-07-04)

A read-only overnight audit (using the higher-capability model) produced
`MASTERPLAN.md` and `ARCHITECTURE.md`. A separate planning-tool session verified
every specific, checkable claim in both files
directly against source (mDNS removal, imu priority traced to the actual
`xTaskCreatePinnedToCore` call, `overflow_count` traced to the exact
`struct.unpack_from` tuple, Flask/http.server, git history on `wifi_creds.h`) —
everything held up. The audit's Phase 0 correctly identified its own premise
(a leak in the removed `resolve_gateway_mdns()` path) as stale; the current
reconnect path has no statically visible leak.

**New data point:** an unattended overnight run (XIAO idle, no rig) ran cleanly
for 7+ hours — heap oscillated 12356-26200 bytes (not draining), contradicting
the earlier 9-minute failure. Also traced why kurtosis/z-score/HST/alert looked
"frozen" for hours: `mic_task.c`'s `if (var > 1e-12f)` guard on the kurtosis
update is working as intended — with a genuinely silent mic (rig was just
sitting on the table, confirmed by user), it correctly holds its last real
value rather than computing garbage on near-zero variance. Not a bug, but a
UX gap worth fixing later: the dashboard can't distinguish "nothing new to
report" from "still actively confirming FAULT."

**Phase 1 - all done, all committed (9 commits, `6b52b42`..`98b16b2`):**
- Fixed stale docs: `main.c`/`epm_config.h` task tables said imu priority
  5/stack 8192; actually 3/3072 since Session 7's retune.
- Rewrote `sdkconfig.defaults`'s PSRAM comment (still said "DISABLED -
  boot-loop investigation pending" directly above the working `CONFIG_SPIRAM=y`).
- Dropped dead `espressif/mdns` (`idf_component.yml`) and `flask>=3.0`
  (`requirements.txt` - dashboard is stdlib `http.server`, no Flask usage
  anywhere, confirmed by grep).
- Deleted dead `src/test_blink.c` (untracked, build-excluded).
- Wired `overflow_count` through: `HEADER_FMT`'s trailing pad byte was
  silently dropping it gateway-side even though firmware always sent it.
  Now unpacked, in `parse_frame`'s returned dict, logged to CSV, and a
  console warning fires on nonzero. Updated `satellite_sim.py` to match the
  new format (added the missing `struct.pack` argument).
- Added the Phase 0 heap-drain instrumentation to `wifi_task.c`: heap_caps
  free-size + largest-free-block logged at connect/send_frame/alert/drop,
  tagged with `frame_id`. Off by default - enable via `-DEPM_HEAP_TRACE=1`
  in `platformio.ini` build_flags (currently commented out).
- Found + fixed: 32 files (`README.md`, `docs/`, `mic_tools/*`) had turned
  into full-file diffs from CRLF line-ending conversion with **zero** actual
  content change (`git diff --stat -w` was empty) - reverted them and added
  `.gitattributes` (`* text=auto eol=lf`) so this stops recurring.
- Found + fixed: `.gitignore`'s AI-tool-session-directory / `build_log_stackfix.txt` entries
  didn't land in the first commit (a file-tool/shell sync timing issue) -
  caught by cross-checking `git show HEAD:<file>` against every edit before
  calling the work done, not just trusting the edit calls succeeded. The
  SAME sync issue hit this very PROJECT_STATUS.md update too (this Session 8
  section didn't land in the shell-visible copy on the first attempt either)
  - worth remembering for future sessions: always verify file-tool edits
    against the shell view before git-committing, don't assume they matched.
- pytest 91/91 passing throughout (`test_drift.py`/`test_online_detector.py`
  can't be collected in the planning-tool sandbox - `river` fails to build there;
  environment limitation, not a code issue).

**Still open, untouched:** `find_xiao_port.ps1` and `partitions_8mb.csv` (the
unused OTA partition variant) remain untracked - low priority, no decision
made yet.

### Next action - Phase 0 soak test (blocking, needs physical hardware)

The planning-tool session has no hardware access, so this must run in a local
AI-assistant session pointed at this same repo folder:
1. Uncomment `-DEPM_HEAP_TRACE=1` in `platformio.ini`.
2. Flash, run 30+ min (longer is better) with the board just sitting there -
   no test rig needed; this only exercises the WiFi/TCP/encryption/reconnect
   path, not fault detection.
3. Capture the full serial log (`HEAPTRACE` lines interleaved with normal
   output).
4. Revert the flag afterward.
5. Bring the log back for diagnosis - the higher-capability model, Plan mode recommended for
   reading the free-size vs. largest-free-block trend against `frame_id`
   (distinguishes true leak vs. fragmentation, per-frame vs. per-reconnect).

Only after that's resolved: (b) reconnect-frequency hardening if still
needed, (c) real KX134 IMU integration (stub TODOs 3a-3d already in
`imu_task.c`), (d) model/accuracy tuning (`KNOWN_ISSUES.md` WP-02/03/05/08/09).

## Session 9 — Phase 0 CONFIRMED: heap fragmentation, not a leak (2026-07-04)

2-hour instrumented soak test (`-DEPM_HEAP_TRACE=1`) run via a local
AI-assistant session with hardware access. Monitor attached at frame 276 (board had
already been running ~4 min), captured to `heap_soak_log.txt` through frame
6609 (~125.5 min firmware uptime, 12,500 HEAPTRACE lines, 3 drop/connect
event pairs).

**Results:**
| Event | Frame | Time | free | largest |
|---|---|---|---|---|
| drop | 2441 | t=36.8min | 3,516 B | 1,024 B |
| connect | 2442 | t=55.9min (stuck 19min) | 26,672 B | 17,408 B |
| drop | 2788 | t=61.2min | 3,516 B | 1,280 B |
| connect | 2789 | t=68.1min (stuck 7min) | 26,080 B | 17,408 B |
| drop | 4023 | t=86.7min | 28,504 B | 18,432 B |
| connect | 4024 | t=86.7min (~2s, no stuck loop) | 26,664 B | 17,408 B |

**Root cause confirmed: heap fragmentation, not a monotonic leak.** Total
free memory fully recovers (26-28 KB) — it is not draining forever, which
is why the earlier 7-hour idle run looked "healthy." What actually degrades
is the *largest contiguous free block*: it drops to ~1-1.3 KB (too small for
lwIP/mbedTLS to stand up a fresh connection) while total free is still
~3.5 KB. Frame 4023's drop shows this precisely — heap was already healthy
(28,504 B free / 18,432 B largest) at drop time, so that reconnect took ~2s
with no stuck loop at all; it was never a heap event.

**The recovery mechanism, both times it got unstuck:** a genuine WiFi-radio-
level disconnect (reasons 1/203/205) forced a full `esp_wifi` stack
stop/start cycle, and THAT is what actually defragments the heap back into
one large contiguous block (free jumped ~7-8x, largest jumped ~14-17x each
time). The app-level `drop_connection()`/`connect_to_gateway()` TCP-only
reconnect loop never triggers this — it just retries `tcp_connect()` against
an already-fragmented heap. So the stuck duration is entirely luck-dependent
on an unrelated radio-level disconnect happening to bail it out. This is
exactly why the original 9-minute test never recovered: it simply didn't get
that lucky break within the observation window. Same failure mode, not a
fluke — confirmed across a 125-minute window with 2 independent occurrences.

**Fix identified (mitigation, not yet implemented):** in `wifi_task_fn`'s
reconnect loop, track consecutive `connect_to_gateway()` failures / elapsed
stuck time. After ~60-90s of failures (well under the 7-19 min observed
stuck windows), force a full WiFi-level reset (`esp_wifi_disconnect()` +
reconnect, integrating with whatever `on_wifi_disconnected()` already does)
rather than only retrying `tcp_connect()`. This bounds the outage to ~90s
instead of indefinite, without needing to know the exact fragmentation
source.

**Not yet done — deeper root cause:** *why* the heap fragments during normal
operation (which specific lwIP/mbedTLS/WiFi-driver allocation pattern is
responsible) is still unknown. The bounded-reset fix is a mitigation the
system can ship with; finding and fixing the actual fragmentation source is
a separate, deeper investigation for later.

### Next action

Implement the bounded-reset mitigation in `wifi_task.c`'s reconnect loop
(prompt handed to the user for a fresh AI-assistant session). After that's in
and flashed, re-run a multi-hour soak with `EPM_HEAP_TRACE=1` again to
confirm stuck windows are now bounded to ~90s instead of open-ended.

## Session 10 — Phase 0 CLOSED: bounded-reset fix validated on real 3-hour soak (2026-07-04)

Flashed the Session 9 mitigation (75s bounded WiFi-reset escape hatch in
`wifi_task_fn`'s reconnect loop) and ran a 3-hour validation soak
(`heap_soak_log_v2.txt`, 178.5 min, `EPM_HEAP_TRACE=1`). Independently
verified (note: this log is UTF-16LE-encoded — `iconv -f UTF-16LE -t UTF-8`
before grepping, plain grep silently returns 0 matches otherwise).

**Fix confirmed working:** one genuine heap-fragmentation event at t=34.0min
(frame 1981) — heap at drop: free=2884B, largest=1280B (matches Session 9's
3516/1024 signature exactly). The escape hatch fired after 84 seconds
(vs the original 7-19 *minutes*), forced `esp_wifi_disconnect()`, and the
heap fully defragmented within ~2 seconds (free=26248B, largest=17408B).
Normal operation resumed immediately and stayed healthy for ~15 more
minutes (frames 1981-3077). `forced_reset` fired 80 times total across the
run; only the first was a genuine fragmentation event — the other 79 (all
from frame 3078 onward, all showing an already-healthy heap ~29024/18432)
were the escape hatch correctly but harmlessly firing every ~96s because
the *gateway* was unreachable, not because of heap state.

**Root cause of the post-frame-3077 outage: NOT a gateway crash — the
Windows Mobile Hotspot itself went off.** Grepping disconnect reasons in
that window: 39× `NO_AP_FOUND`, 1× `ASSOC_LEAVE`, 5× `OTHER` — the ESP32
was correctly failing to find an access point that was no longer there.
This matches Windows Mobile Hotspot's default "turn off automatically when
no devices are connected" behavior (or the host laptop sleeping). User
confirmed on physical inspection: the hotspot was indeed off. Not a
firmware or gateway bug — a test-environment gotcha for future unattended
soaks: disable that Windows setting (Settings > Network & Internet >
Mobile hotspot > "Turn off hotspot automatically") or keep the host awake,
or the satellite will correctly-but-uselessly loop `NO_AP_FOUND` once the
AP disappears mid-soak.

**Phase 0 is now closed.** Root cause found (fragmentation, Session 9),
fix implemented and flashed (Session 9/10), fix validated against a real
3-hour hardware run with a genuine fragmentation event captured and
recovered exactly as designed (Session 10). Connectivity can now be
considered solid.

**Residual, low-priority cleanup (not urgent):** the escape hatch still
fires every ~96s when the gateway is unreachable for an extended period,
even though the heap is healthy — harmless (just an unnecessary WiFi
reassociation cycle) but noisy in the log. Future fix: skip
`esp_wifi_disconnect()` in the escape hatch when `largest` is already above
some healthy threshold (e.g. 8-10 KB) — only reset when fragmentation is
the actual suspected cause.

### Next steps (in rough priority order)

1. Optional: the harmless-but-noisy repeated-reset cleanup above.
2. Mic subsystem audit completed this session (separate from Phase 0) —
   found the pipeline itself is sound, but flagged two real gaps ahead of
   calling this "industrial-ready": (a) `MIC_SAMPLE_RATE_HZ=16000` was
   picked as a placeholder and never empirically validated against the
   mic's real usable ceiling or the project's own cited 2-10kHz bearing
   resonance range — test plan written, needs hardware; (b) the live fault
   classifier (`_classify_fault_type` in recv_verify.py) does NOT use
   `bearing_math.py`'s BPFO/BPFI/BSF computation or any envelope/
   demodulation analysis — it's broadband-statistics anomaly detection
   with diagnostic-sounding labels, not physics-verified fault-type
   diagnosis. No live shaft-speed reference either. This is a bigger,
   deliberate feature decision, not a quick fix.
3. Real KX134 IMU integration (stub TODOs 3a-3d in imu_task.c).
4. Model/accuracy tuning (KNOWN_ISSUES.md WP-02/03/05/08/09) — all still
   simulation-tuned, never validated against a real recorded bearing fault.
