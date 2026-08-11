# New Satellite Node Setup Guide

Step-by-step guide to bringing up a brand-new EPM satellite node from a bare
XIAO ESP32-S3, an INMP441 microphone, and a KX134 accelerometer — nothing
else pre-configured. It consolidates and cross-verifies wiring, build,
provisioning, and debugging information that otherwise lives spread across
[`README.md`](../README.md), [`docs/hardware/PIN_ALLOCATION.md`](hardware/PIN_ALLOCATION.md),
and [`docs/BASE_STATION_CONTRACT.md`](BASE_STATION_CONTRACT.md). Every
value below was checked against the current firmware source, not copied
from an earlier draft — if something here ever disagrees with the code,
trust the code and file an issue.

---

## 1. Bill of Materials

| Component | Notes |
|---|---|
| Seeed XIAO ESP32-S3 | Dual-core LX7 @ 160 MHz, 8 MB flash, 2 MB PSRAM. Either the plain or **Sense** variant works — see the warning below. |
| INMP441 or ICS-43434 breakout | I2S MEMS microphone, −26 dBFS sensitivity, 60 Hz – 15 kHz. **Required as an external part** — see below. |
| KX134 3-axis IMU breakout | SPI, ±8g/±16g/±32g/±64g, up to 25.6 kHz ODR. Needs a way to bolt it rigidly to the motor housing. |
| USB-C cable | Data-capable (not charge-only) — used for flashing and the serial console. |
| 2.4 GHz WiFi access point or hotspot | Windows/Android/iPhone hotspot all work. The firmware only joins WPA/WPA2-PSK 2.4 GHz networks. |
| A running gateway | An Arduino Uno Q 4GB or a laptop, already running `gateway/` with a Mosquitto broker reachable on the same LAN. You'll need its IP/hostname and broker port during provisioning (§5). |

> **If you have the XIAO ESP32-S3 *Sense* variant: its onboard PDM
> microphone is NOT used by this project, and this has caused confusion
> before.** The firmware's default build (`components/epm_drivers/mic_inmp441_i2s.c`)
> talks to an **external** INMP441/ICS-43434 over the standard I2S driver
> (`i2s_channel_init_std_mode`), wired to GPIO 2/3/4 as in §2 below — it does
> not read the Sense board's built-in PDM mic at all. If you only have a
> Sense board and no external mic wired up, the firmware will build and run
> but the mic channel will read silence/garbage. Using the onboard PDM mic
> instead would require swapping in `i2s_channel_init_pdm_rx_mode`
> (`i2s_pdm_rx_config_t`) — the comment at the top of `mic_inmp441_i2s.c`
> explains exactly what would need to change — but this is not what the
> default build does, and is not covered by the rest of this guide.

---

## 2. Wiring

Pin assignments below are re-verified against
[`docs/hardware/PIN_ALLOCATION.md`](hardware/PIN_ALLOCATION.md) and the
GPIO `#define`s in `components/epm_drivers/include/drivers/` — **do not**
reassign these without also updating the firmware, they're compiled in.

### Microphone — XIAO ESP32-S3 ↔ INMP441

| XIAO pin | GPIO | INMP441 pin |
|----------|------|-------------|
| D1 | 2 | SCK (BCLK) |
| D2 | 3 | WS (LRCLK) |
| D3 | 4 | SD (data out from mic) |
| 3V3 | — | VDD **and** L/R → GND (selects left channel) |
| GND | — | GND |

The firmware runs I2S in Philips standard mode, mono, 32-bit slot width
(the mic's 24-bit samples are left-justified within the slot), 48 kHz
sample rate (`MIC_FS_HZ` in `src/epm_config.h`). Tying L/R to GND is
required — the driver only reads the left channel.

### IMU — XIAO ESP32-S3 ↔ KX134 (SPI)

| GPIO | KX134 pin | Function |
|------|-----------|----------|
| 7 | SCLK | SPI clock (10 MHz) |
| 8 | MISO/SDO | SPI data in (acceleration reads) |
| 9 | MOSI/SDI | SPI data out (command/config writes) |
| 43 | CS | Chip select, active LOW (`KX134_PIN_CS`) |
| 44 | INT1 | Wired but not currently read by firmware (`KX134_PIN_INT1`) |
| 3V3 | VDD | Power |
| GND | GND | Ground |

GPIO43/44 double as UART0 TX/RX on this board, but that's safe here — the
debug console runs over USB-JTAG (`esp-builtin`), not physical UART0.

---

## 3. Toolchain Setup

Either of:

- [PlatformIO](https://platformio.org/) with the Espressif32 platform installed, **or**
- ESP-IDF v5.x with `idf.py` on your `PATH`

Plus Python 3.9+ on the machine you'll flash from (used by the flashing
tooling only — the satellite itself runs no Python).

---

## 4. Clone and Build

```bash
git clone https://github.com/Abhinavkrishna3211/edgeai-predictive-monitor-satellite.git
cd edgeai-predictive-monitor-satellite

# Build only, no upload yet — confirms the toolchain is set up correctly
pio run --environment xiao_esp32s3
```

`main` is the repository's sole branch — clone it with no `-b` flag needed.

---

## 5. First Boot / WiFi + MQTT Provisioning

There is no compile-time credentials file to edit. On a genuinely first
boot, `src/threads/wifi_task.c`'s `wifi_rf_init()` finds nothing saved in
NVS and seeds it from the compiled `WIFI_SSID`/`WIFI_PASS` placeholder
values (a dev-bench escape hatch) before the join is even attempted — those
placeholders won't match your network, so the board spends the full
`BOOT_JOIN_TIMEOUT_MS` (15 s) failing to join before
`src/threads/wifi_provision_task.c` gives up and enters
`WIFI_PROV_PROVISIONING`, bringing up its own AP and a captive portal:

1. Connect a phone or laptop to the AP `EPM-SAT-<node_id>`. Its WPA2
   password is generated once per device on first boot
   (`esp_fill_random()` in `components/epm_drivers/ap_credentials.c`,
   persisted in NVS so it survives reboots) and printed to the serial
   console — watch the console (§6) the first time you power the board on,
   or you won't have it.
2. Your OS should auto-open the portal via its captive-portal detection —
   the board runs a DNS wildcard responder
   (`components/epm_drivers/dns_captive.c`) that answers *every* DNS query
   with `192.168.4.1`, which is what triggers that auto-open. If it doesn't
   pop up, browse to `http://192.168.4.1` manually.
3. Submit: WiFi SSID, WiFi password, **MQTT Broker Host** (your gateway
   machine's LAN IP or hostname), and **MQTT Broker Port** (`1883` unless
   you changed Mosquitto's default). The firmware then spends up to 15 s
   (`STA_TEST_TIMEOUT_MS`) test-joining with what you submitted
   (`WIFI_PROV_STA_TESTING`) before committing it — a bad password or
   unreachable SSID just drops you back into provisioning to try again,
   nothing is bricked.
4. On success, credentials persist in NVS
   (`components/epm_drivers/net_credentials.c`) and survive reboots and
   reflashes — you only do this once per board (or after a manual
   NVS/credentials reset).

---

## 6. Flashing

**PlatformIO (recommended):**

```bash
pio run --target upload --environment xiao_esp32s3
pio device monitor
```

No `--upload-port`/COM number needed — `platformio.ini` uses
`upload_protocol = esp-builtin` (the board's built-in USB-JTAG, which
avoids a CDC-triggered reset-to-download-mode bug) and
`monitor_port = hwgrep://303A:1001`, which auto-detects the board by
USB VID:PID instead of a fixed COM number. **Do not hardcode a COM port
here** — the assigned number shifts across replugs/reboots on Windows, and
`platformio.ini`'s own comment notes it drifted from COM7 to COM15 on one
dev machine already.

**ESP-IDF directly:**

```bash
idf.py -p <YOUR_PORT> flash monitor
```

Since `idf.py` doesn't read `platformio.ini`'s `hwgrep://` auto-detect, you
need the actual port here. On Windows, check Device Manager → Ports (COM &
LPT) for the "USB JTAG/serial debug unit" entry right after plugging in —
don't reuse a port number from a previous session or machine, it isn't
guaranteed to still be correct.

---

## 7. Verifying It's Alive

Watch the RGB LED (8-pixel WS2812 ring, `components/epm_drivers/display_neopixel.c`)
for this sequence on a healthy first boot:

| State | Color / mode | Meaning |
|-------|--------------|---------|
| `RGB_BOOT` | White, solid | Power-on, initializing |
| `RGB_WIFI_CONN` | Blue, breathe (1200 ms) | Joining WiFi (or in the provisioning AP — see §9 for how these two look identical) |
| `RGB_TCP_CONN` | Blue, strobe (300 ms) | WiFi has an IP, connecting to the MQTT broker |
| `RGB_CALIBRATING` / `RGB_LEARNING` | Cyan, solid | Building the adaptive baseline from live frames |
| `RGB_OK` | Green, solid | Healthy, streaming normally |

Reaching solid green confirms boot, WiFi, and MQTT all came up. To confirm
it's actually publishing data (independent of whether the gateway's own
pipeline is running), from a machine on the same LAN as the broker:

```bash
mosquitto_sub -h <broker-ip> -t 'epm/+/data' -v
```

You should see one binary frame per node roughly every 200 ms
(`EPM_NET_PUBLISH_INTERVAL_MS`). Alternatively, open the gateway's
dashboard (`http://<gateway-ip>:8080/`, Machines tab) and confirm the new
node's card appears and its FPS/last-seen fields are live.

---

## 8. Code Architecture Overview

[`ARCHITECTURE.md`](../ARCHITECTURE.md) at the repo root is the
authoritative, already-detailed reference for this — end-to-end data flow,
the firmware's task/queue map and boot order, memory placement, the wire
protocol, and the gateway's 7-step pipeline. Read that rather than a
summary here. Two things worth knowing before you dive in:

- Firmware tasks live in `src/threads/` (`mic_task`, `dsp_task`, `imu_task`,
  `net_task`, `wifi_task`, `wifi_provision_task`), each producing/consuming
  through FreeRTOS queues wired up in `src/main.c` — see
  `ARCHITECTURE.md`'s §2 for exact boot order and priorities.
- The wire format is a self-describing section-list frame (mic + 3-axis
  accel spectra + 3 envelope-analysis channels + scalar sets), decoded on
  the gateway side by `gateway/common/wire_protocol.py`. Full byte layout:
  [`docs/BASE_STATION_CONTRACT.md`](BASE_STATION_CONTRACT.md).

---

## 8a. Making changes

The actual rules — naming conventions, commit message format, the ADR
process for anything architecturally significant — live in
[`docs/CONVENTIONS.md`](CONVENTIONS.md); read that before your first commit
rather than guessing from what's around it.

The one thing most likely to bite someone unfamiliar with this repo: if a
change touches the wire format (adding/changing a channel, scalar, or
section), the source of truth is `schema/telemetry_schema.json` — edit that
and re-run `schema/gen_schema.py`, never hand-edit the generated
`components/epm_codec/include/frame_codec/telemetry_schema.h` or
`gateway/common/telemetry_schema.py` directly (both carry a `GENERATED ...
DO NOT EDIT BY HAND` banner at the top).

Most tunables aren't buried in code logic — they're grouped at the top of
two files: firmware knobs (FFT sizes, task stacks, bin count, thresholds)
in `src/epm_config.h`, and gateway alert thresholds in
`mic_tools/recv_verify.py`.

---

## 9. Debugging

### Reading the serial monitor

`pio device monitor` (or `idf.py monitor`) streams the live ESP-IDF log.
Log lines are tagged by source — `wifi_task`, `net_task`, `DIAG`, etc. — so
grepping for a tag narrows things down fast.

### The 30-second DIAG health log

Every 30 s, `diagnostics_task_fn()` (`src/main.c`) logs a block of `DIAG`-tagged
lines covering: FreeRTOS stack high-water marks for every task, heap
(`internal`/`largest_free`/`PSRAM`/`IRAM` free bytes — a falling
`largest_free` with flat `internal` means fragmentation, both falling
together means real exhaustion), WiFi connect/disconnect/retry counters,
MQTT connect/disconnect/publish counters, provisioning state, and per-task
counters for mic capture, DSP, IMU, net, and LED. On a healthy node you
want to see `mqtt: connects=1 disconnects=0` staying put and `net:
frames_built` climbing steadily with `build_failures=0
publish_failures=0`. This is the single most useful thing to paste when
something looks wrong on a board nobody is watching live.

### Confirming raw frames independent of the gateway

`mosquitto_sub -t 'epm/+/data' -v` (§7) proves the satellite itself is
publishing, without the gateway's own decode/pipeline logic in the loop at
all — useful for telling "firmware isn't sending" apart from "gateway isn't
processing what it receives."

### WiFi-layer drop vs. MQTT-layer stall — `RGB_WIFI_CONN` vs `RGB_MQTT_STALL`

These two used to be indistinguishable — both were `RGB_WIFI_CONN` ("blue,
slow breathe"), which caused real confusion during demo prep
(`docs/performance/SATELLITE_STRESS_STABILITY_TEST.md`'s 2026-08-11
addendum). They're now two different colors, but the underlying two call
sites are worth knowing if you ever need to double-check against the serial
log instead of trusting the LED alone:

- `src/threads/wifi_task.c`'s `on_wifi_disconnected()` sets `RGB_WIFI_CONN`
  (blue, breathe 1200 ms) on a genuine WiFi-association-level drop, and
  **only this path** logs a line starting `wifi_task: Disconnect reason:
  ...` (with a decoded reason like `ASSOC_LEAVE`, `BEACON_TIMEOUT`,
  `AUTH_FAIL`, etc.).
- `src/threads/net_task.c`'s publish loop sets `RGB_MQTT_STALL` (violet,
  breathe 900 ms) whenever `transport_is_connected()` goes from true to
  false — i.e. purely an MQTT-session drop, with WiFi still fully
  associated. This path logs `net_task: MQTT disconnected — reverting
  display to local state` and **does not** log any `Disconnect reason`
  line, because WiFi itself never saw an event.

So: if the LED goes **violet**, WiFi is fine and this is a silent
MQTT-layer stall, not a real network drop — no need to go check the serial
log for a `Disconnect reason` line anymore, the color already tells you
which layer dropped. That stall self-heals — `diagnostics_task_fn()`
counts consecutive MQTT disconnects and calls `esp_restart()` once it hits
10 (shortened from the original 30 on 2026-08-11; see
`docs/decisions/ADR-036-mqtt-reconnect-watchdog.md`'s 2026-08-11 addendum
for the reasoning). Real-hardware-confirmed self-heal time at the new
threshold is **~152 seconds (~2.5 minutes)** from stall onset to automatic
restart — measured directly, not just threshold × retry-cadence math. This
was independently reproduced live on 2026-08-11 against the reference base
station's own unmodified code — a ~403 s data gap, confirmed frozen on both
`mosquitto_sub` and the reference dashboard's own `last_seen` field at once,
then a clean self-recovery with no reset button pressed
(`docs/performance/HARDWARE_INTEROP_TEST.md`'s 2026-08-11 addendum). If you
see the LED go violet during a demo or bring-up: **wait, don't power-cycle**
— power-cycling mid-stall is what has been mistaken for "real disconnects"
before.

---

## 10. Troubleshooting

Common bring-up issues and fixes are already maintained in
[README.md's Troubleshooting section](../README.md#troubleshooting) —
check there first. The entries most relevant to a first-time bring-up:

- **LED stays solid white / never leaves BOOT:** WiFi isn't starting.
- **LED stuck blue with no `Disconnect reason` log line:** see §9 above —
  likely a self-healing MQTT stall, not a WiFi problem.
- **No satellite in the dashboard / gateway shows no connects:** check the
  MQTT broker port (1883) and dashboard port (8080) aren't firewalled.
- **`TG1WDT_SYS_RST` crash on boot:** verify `wifi_rf_init()` runs before
  `imu_task_start()`/`mic_task_start()` in `src/main.c` — I2S DMA
  interrupts during WiFi RF scan cause this if the order is wrong.
- **Build error `i2s_std.h: No such file`:** your ESP-IDF/PlatformIO
  platform is too old — needs ESP-IDF 5.x (`platform = espressif32 @
  ^6.0.0` in `platformio.ini`).

For anything not covered above or in README.md, the DIAG health log (§9)
is almost always the fastest way to narrow down which subsystem is at fault.
