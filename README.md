# EdgeAI Predictive Monitor — Satellite Node

A wireless multi-satellite bearing-fault detection system for industrial motors.
XIAO ESP32-S3 sensor nodes stream real-time FFT data over WiFi to a Python gateway
that applies statistical + ML anomaly detection, logs CSV data, and serves a live
web dashboard accessible from any device on the LAN.

---

## System Architecture

```
┌────────────────────────────────────────────────────────────┐
│  XIAO ESP32-S3  (satellite node)  ×N                       │
│                                                            │
│  INMP441 mic ─► I2S ─► 1024-pt FFT ×4 avg  (16 kHz)      │
│  KX134 IMU   ─► SPI ─► 2048-pt FFT ×3 axes (25.6 kHz)    │
│                            ↓                               │
│  led_task ◄── alert byte ◄── TCP frame ──► gateway         │
└──────────────────────────────────────────────────────────┘
                         WiFi / TCP
┌────────────────────────────────────────────────────────────┐
│  Arduino Uno Q  (permanent gateway + AI engine)            │
│                                                            │
│  recv_verify.py  --no-plot                                 │
│   ├─ 30-frame adaptive baseline (z-score calibration)      │
│   ├─ kurtosis + crest factor + high-band energy scoring    │
│   ├─ IsolationForest ML anomaly detection (optional)       │
│   ├─ CSV log → mic_tools/logs/  (per satellite per day)    │
│   ├─ Maintenance log → logs/maintenance_log.json           │
│   ├─ Web dashboard  http://<uno-q-ip>:8080/                │
│   └─ 1-byte alert reply to satellite  0x00/0x01/0x02       │
└──────────────────────────────────────────────────────────┘
                         LAN (WiFi / Ethernet)
┌────────────────────────────────────────────────────────────┐
│  Any browser on the LAN  (phone / tablet / laptop)         │
│                                                            │
│  http://<uno-q-ip>:8080/   ← live dashboard               │
│  http://<uno-q-ip>:8080/api/report  ← printable report    │
└──────────────────────────────────────────────────────────┘
```

**The Arduino Uno Q is the only always-on compute node.**  
Satellite firmware (ESP32) → streams sensor data only.  
The laptop is used only during firmware development and flashing.  
Once the system is deployed, the laptop is not needed at all.

---

## Hardware

| Component | Role |
|-----------|------|
| Seeed XIAO ESP32-S3 | Satellite sensor node — captures vibration + sound, streams over WiFi |
| INMP441 / ICS-43434 | External I2S MEMS microphone — wire to D1/D2/D3 |
| KX134 3-axis IMU | SPI accelerometer — bolt to motor casing |
| Arduino Uno Q | Permanent gateway: runs Python, scores data, serves dashboard |
| 2.4 GHz AP / hotspot | WiFi access point — Windows / Android / iPhone hotspot all work |

> **Microphone note:** The firmware uses the standard I2S driver targeting
> **external** microphones (INMP441, ICS-43434) wired to GPIO 2/3/4.  
> The XIAO ESP32-S3 Sense board's onboard PDM microphone needs a different driver
> (`i2s_pdm_rx_config_t`).  To use the onboard mic, swap
> `i2s_channel_init_std_mode` for `i2s_channel_init_pdm_rx_mode` in
> `components/mic_capture/mic_capture.c` — the comment at the top of that file
> explains exactly what to change.

### Wiring — XIAO ESP32-S3 ↔ INMP441

| XIAO pin | GPIO | INMP441 pin |
|----------|------|-------------|
| D1       | 2    | SCK (BCLK)  |
| D2       | 3    | WS (LRCLK)  |
| D3       | 4    | SD (data out from mic) |
| 3V3      | —    | VDD + L/R pin → GND (selects left channel) |
| GND      | —    | GND         |

Pin assignments are in `components/mic_capture/include/mic_capture.h`.

---

## Repository Layout

```
edgeai-predictive-monitor-satellite/
├── src/
│   ├── main.c              # app_main — WiFi-before-DMA boot order, task start
│   ├── led_task.c/h        # 7-state LED state machine (esp_timer, active-low)
│   ├── mic_task.c/h        # I2S capture, windowed FFT, kurtosis, crest factor
│   ├── imu_task.c/h        # KX134 SPI stub — replace generate_stub_axis() with real FIFO reads
│   ├── wifi_task.c/h       # WiFi STA + TCP client + 1-byte alert receive
│   ├── epm_config.h        # Compile-time tunables: FFT sizes, task stacks, GPIO pins
│   ├── epm_protocol.h      # Binary wire format (48-byte header, static_assert verified)
│   └── wifi_creds.h        # ← NOT IN REPO — create manually (Step 2 below)
│
├── components/
│   └── mic_capture/        # Reusable I2S MEMS capture component
│       ├── mic_capture.c
│       └── include/mic_capture.h
│
├── mic_tools/
│   ├── recv_verify.py      # Gateway: receive, score, alert, CSV log, dashboard
│   ├── satellite_sim.py    # Test gateway without hardware (N simulated satellites)
│   ├── bearing_math.py     # ISO bearing fault frequencies — BPFO/BPFI/BSF/FTF
│   ├── ml_trainer.py       # Train IsolationForest anomaly model from CSV logs
│   ├── ml_infer.py         # Offline anomaly analysis with trained model
│   ├── plot_mic.py         # LEGACY serial debug tool — do not use with current firmware
│   └── requirements.txt
│
├── CMakeLists.txt          # Root ESP-IDF project
├── platformio.ini          # PlatformIO build + upload config
├── sdkconfig.defaults      # ESP-IDF KConfig overrides (watchdog, TCP buffers, -O2)
└── .gitignore
```

> `src/wifi_creds.h` is gitignored and must **never** be committed.

---

## Quick Start — Satellite Firmware

### 1. Prerequisites

- [PlatformIO](https://platformio.org/) with the Espressif32 platform installed, **or**
  ESP-IDF v5.x (`idf.py` in PATH)
- Python 3.9+ (on the dev laptop, for flashing only)

### 2. Create `src/wifi_creds.h`

Create this file manually — it is gitignored and will never be committed:

```c
// src/wifi_creds.h — gitignored, never in the repo
#pragma once
#define WIFI_SSID    "YourNetworkName"
#define WIFI_PASS    "YourPassword"
#define SERVER_IP    "192.168.137.1"   // Uno Q's IP on the LAN — see table below
#define SERVER_PORT  5100
```

Common gateway IPs by hotspot type:

| Hotspot type | Default gateway IP |
|--------------|--------------------|
| Windows Mobile Hotspot | `192.168.137.1` |
| Android hotspot        | `192.168.43.1`  |
| iPhone hotspot         | `172.20.10.1`   |
| macOS Internet Sharing | `192.168.2.1`   |
| Home router / fixed IP | Run `ip a` on the Uno Q |

> When you move from laptop development to Uno Q deployment, only `SERVER_IP`
> changes — update it to the Uno Q's LAN IP and reflash all satellite nodes.

### 3. Build and Flash

**PlatformIO (recommended):**
```bash
pio run --target upload --environment xiao_esp32s3
pio device monitor
```

**ESP-IDF directly:**
```bash
idf.py -p COM9 flash monitor    # adjust port for your system
```

---

## Quick Start — Gateway on Arduino Uno Q

The Uno Q runs Python headlessly — no display, no laptop needed after setup.

### 1. Install dependencies on Uno Q

```bash
sudo apt update && sudo apt install python3 python3-pip -y
cd mic_tools
pip3 install -r requirements.txt
```

### 2. Start the gateway

```bash
# Minimal headless startup
python3 recv_verify.py --no-plot

# With factory label and HTTP Basic Auth (recommended for production)
python3 recv_verify.py --no-plot \
    --factory-name "Plant A — Line 3" \
    --auth admin:yourpassword

# With emergency notifications (Discord/Slack/Teams webhook)
python3 recv_verify.py --no-plot \
    --factory-name "Plant A" \
    --auth admin:yourpassword \
    --notify-webhook "https://hooks.slack.com/services/..."

# With email alerts (SMTP)
python3 recv_verify.py --no-plot \
    --notify-email "from@example.com:to@example.com:smtp.gmail.com:587:user:pass"

# With ML anomaly model (after training on collected CSVs)
python3 recv_verify.py --no-plot --model model/epm_model

# With bearing fault frequency markers
python3 recv_verify.py --no-plot --shaft-rpm 1500 --bearing 6205

# Override alert thresholds
python3 recv_verify.py --no-plot --crest-warn 4.5 --crest-fault 9.0
```

### 3. Run as a background service (always-on)

```bash
nohup python3 recv_verify.py --no-plot \
    --factory-name "Plant A" \
    --auth admin:yourpassword &
echo $! > gateway.pid
```

Or install as a `systemd` service so it starts automatically on Uno Q boot:

```ini
# /etc/systemd/system/epm-gateway.service
[Unit]
Description=EPM Gateway
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/user/edgeai/mic_tools/recv_verify.py \
    --no-plot --factory-name "Plant A" --auth admin:yourpassword
WorkingDirectory=/home/user/edgeai/mic_tools
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable epm-gateway
sudo systemctl start epm-gateway
```

### 4. Open the dashboard

```
http://<uno-q-ip>:8080/
```

Open from any phone, tablet, or laptop on the same WiFi network.
No software to install on the viewing device — it's just a browser.

---

## Web Dashboard

The dashboard is a full industrial monitoring interface served by the gateway.
Access it from **any browser on the LAN** — phones, tablets, laptops.

### Tabs

| Tab | Contents |
|-----|----------|
| **Machines** | Live machine cards: health bar, kurtosis, crest factor, RMS, z-score, FPS, RUL estimate, sparkline chart, maintenance date |
| **Alert Log** | Full compliance-ready audit trail of every state transition (OK→WARN→FAULT→OK) with timestamps |
| **Maintenance** | Per-machine maintenance records; log a new service entry via modal form |
| **Reports** | System overview, compliance checklist, per-machine report links |

### Machine card actions

Each machine card has three buttons:
- **Log Maintenance** — opens a form to record a service visit (technician, type, date, notes)
- **Report** — opens a printable HTML inspection report for that machine in a new tab
- **CSV** — downloads the latest sensor data as a spreadsheet

### Printable HTML reports

Navigate to **Reports → Full Factory Report** or click **Report** on any machine card.

The report opens in your browser with:
- Cover page (factory name, date, report scope)
- Executive summary (risk level, 6 KPI tiles)
- Machine status table (one row per satellite)
- Per-machine detail: metrics, health bar, session analysis (fault/warn rates, trend), last 12 alert events, maintenance record, recommendations
- Full alert audit trail (all events, numbered and timestamped)
- Maintenance log summary with overdue detection
- 7-point compliance checklist

To save as PDF: `Ctrl+P` → **Save as PDF**.  
The `@media print` CSS removes navigation elements automatically for a clean A4 layout.

### HTTP Basic Auth

Start the gateway with `--auth USER:PASS` to require login before the dashboard loads.
The browser caches credentials for the session — one login per browser.

### Emergency notifications

- **Webhook** (`--notify-webhook URL`): posts a JSON alert card to Discord, Slack, or Microsoft Teams when any satellite enters FAULT state. Rate-limited to one notification per satellite per 5 minutes.
- **Email** (`--notify-email FROM:TO:HOST:PORT:USER:PASS`): sends an SMTP email alert. Works with Gmail (app password), Outlook, or any SMTP relay.

---

## Testing Without Hardware — Satellite Simulator

```bash
# Terminal 1: start gateway
python3 recv_verify.py --no-plot

# Terminal 2: simulate 3 healthy satellites
python3 satellite_sim.py 127.0.0.1 5100 3

# Inject fault conditions — test alert logic and LED patterns
python3 satellite_sim.py 127.0.0.1 5100 5 --fault 1 --warn 2
```

Each simulated satellite has a unique fake MAC, sends realistic FFT data, and
auto-reconnects if the gateway restarts.

---

## Bearing Fault Analysis

`bearing_math.py` computes ISO standard bearing defect frequencies from geometry
and shaft speed.

```bash
# Print BPFO / BPFI / BSF / FTF for bearing 6205 at 1500 RPM
python3 bearing_math.py 6205 1500

# List all 18 built-in bearing geometries (6200-6210, 6304-6310)
python3 bearing_math.py 6205 1500 --list

# Custom geometry: n=9 balls, D=38.5 mm pitch, d=10.3 mm ball
python3 bearing_math.py 9,38.5,10.3 1500
```

Run with the gateway to add colored fault frequency markers on FFT plots:

```bash
python3 recv_verify.py --shaft-rpm 1500 --bearing 6205
```

| Marker | Color | Fault type |
|--------|-------|------------|
| `BPFO` | Red | Outer race defect |
| `2×BPFO` | Pink | Outer race 2nd harmonic |
| `BPFI` | Orange | Inner race defect |
| `2×BPFI` | Amber | Inner race 2nd harmonic |
| `BSF` | Purple | Ball spin defect |
| `FTF` | Cyan | Cage fundamental |
| `shaft` | Yellow | Shaft 1× (imbalance reference) |

---

## ML Training Pipeline

### 1. Collect training data

Run the gateway with real or simulated equipment for at least 30 minutes of
healthy operation.  Each satellite logs to `mic_tools/logs/epm_<name>_<YYYYMMDD>.csv`.

### 2. Train the model (on a PC — copy logs from Uno Q)

```bash
cd mic_tools
python3 ml_trainer.py                          # all satellites
python3 ml_trainer.py --satellite SAT-A3B4     # one satellite only
python3 ml_trainer.py --contamination 0.03 --n-estimators 300
```

Writes `model/epm_model_iso.joblib` and `model/epm_model_meta.json`.

### 3. Deploy back to Uno Q

Copy the `model/` directory to the Uno Q, then:

```bash
python3 recv_verify.py --no-plot --model model/epm_model
```

The ML model runs alongside the threshold detector — the more severe alert wins.
Inference activates only after each satellite's 30-frame baseline.

### 4. Offline analysis

```bash
python3 ml_infer.py                        # compare ML vs threshold alerts
python3 ml_infer.py --top-anomalies 20     # 20 worst frames across all satellites
python3 ml_infer.py --export report.csv    # export per-frame predictions
```

---

## Battery Efficiency on XIAO

The firmware calls `esp_wifi_set_ps(WIFI_PS_NONE)` — full power, best throughput.
Typical draw ~80–200 mA at 3.3 V on active WiFi.

| Change | Location | Effect |
|--------|----------|--------|
| `WIFI_PS_MIN_MODEM` | `wifi_task.c` | ~30% lower WiFi power, ≤100 ms extra latency |
| `FFT_MIC_N=512` | `platformio.ini` build_flags | Shorter compute → shorter radio-on time |
| `SPEC_AVG_N=8` | `platformio.ini` build_flags | Longer inter-frame sleep → lower duty cycle |
| Deep-sleep burst | Requires wifi_task rework | Lowest power; loses continuous streaming |

For USB-powered or panel-mounted installs the current setting is optimal.
For LiPo field use, switch to `WIFI_PS_MIN_MODEM`.

---

## LED Indicator

GPIO21 on XIAO ESP32-S3 (active-low: LOW = ON).  Driven by a 100 ms `esp_timer`.
Patterns are rhythm-based — distinguishable by counting taps, not estimating speed.

| State | Pattern | Meaning |
|-------|---------|---------|
| `LED_BOOT` | Solid ON | Power-on, initialising |
| `LED_WIFI_CONN` | 3 quick taps · 0.7 s dark · repeat | Scanning for WiFi AP |
| `LED_TCP_CONN` | 1 s ON / 1 s OFF | WiFi connected, connecting to gateway |
| `LED_CALIBRATING` | 2 quick taps · 1.8 s dark · repeat | Learning baseline (first 30 frames) |
| `LED_OK` | Single 100 ms blip every 3 s | Healthy, normal vibration |
| `LED_WARN` | Steady 1 Hz 50/50 blink | Elevated kurtosis or crest factor |
| `LED_FAULT` | 5 Hz rapid strobe | Bearing fault detected — inspect now |

**Quick ID:** solid=boot · count 3=WiFi · slow blink=TCP · count 2=calibrating · rare blip=OK · 1 Hz=warn · strobe=FAULT

---

## Wire Protocol

All multi-byte fields are **little-endian**.

### Hello packet (sent once after connect)

```
[uint32_t  magic]    4 B   0xEA1D0000
[uint8_t   mac[6]]   6 B   WiFi STA MAC
[uint8_t   fw_major] 1 B
[uint8_t   fw_minor] 1 B
[char      name[12]] 12 B  null-padded ASCII, e.g. "SAT-A3B4"
```

Total: 24 bytes.

### Frame (per ~450 ms, ~14.3 KB total)

```
[uint32_t  payload_bytes]     4 B
[epm_header_t header]        48 B   magic + frame_id + timestamp + metrics
[float mic_fft[512]]       2048 B   MIC dBFS spectrum, 15.6 Hz/bin
[float imu_x_fft[1024]]    4096 B   X radial dBFS, 12.5 Hz/bin
[float imu_y_fft[1024]]    4096 B   Y radial dBFS
[float imu_z_fft[1024]]    4096 B   Z axial  dBFS
```

### Gateway → Satellite (1 byte after each frame)

| Byte | Meaning |
|------|---------|
| `0x00` | OK |
| `0x01` | WARN |
| `0x02` | FAULT |

---

## Alert Thresholds

Configured at the top of `mic_tools/recv_verify.py`:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `CAL_FRAMES` | 30 | Frames to build z-score baseline |
| `K_WARN` | 6.0 | Kurtosis WARN (Gaussian noise ≈ 3) |
| `K_FAULT` | 12.0 | Kurtosis FAULT (advanced bearing damage) |
| `CREST_WARN` | 5.0 | Crest factor WARN |
| `CREST_FAULT` | 10.0 | Crest factor FAULT |
| `HIGH_BAND_MIN` | 0.12 | Min 2–8 kHz energy fraction to raise any alert |
| `WARN_PERSIST` | 2 | Consecutive above-threshold frames to raise alert |
| `CLEAR_PERSIST` | 3 | Consecutive OK frames to clear alert |

`HIGH_BAND_MIN` prevents low-frequency factory floor rumble from triggering false
positives — bearing defects always excite the 2–8 kHz resonance band.

---

## Troubleshooting

**LED stays solid ON:**
WiFi not connecting. Check SSID/password in `wifi_creds.h`. Serial monitor shows
the reason code: 15/203 = wrong password, 200 = SSID not found.

**LED stuck on 3-tap WiFi blink:**
`SERVER_IP` doesn't match the Uno Q's IP. Run `ip a` on the Uno Q to get its address.
Confirm `recv_verify.py` is running before the satellite boots.

**No satellites in dashboard / gateway shows no connects:**
Firewall blocking port 5100 or 8080.
- Linux/Uno Q: `sudo ufw allow 5100 && sudo ufw allow 8080`
- Windows (dev only): run the `New-NetFirewallRule` command the gateway prints at startup (elevated PowerShell, once)

**`satellite_sim.py` prints "Connection refused":**
Start `recv_verify.py` first, then the simulator.

**Dashboard shows login prompt:**
Enter the credentials from your `--auth USER:PASS` flag. The browser caches them.

**Report page is blank or errors:**
The report is generated from live in-memory data — at least one satellite must have
connected and sent frames. Open the Machines tab first to confirm a satellite is visible.

**`TG1WDT_SYS_RST` crash on boot:**
Mitigated by `sdkconfig.defaults` (`CONFIG_ESP_INT_WDT_TIMEOUT_MS=1200`).
If it recurs, verify that `wifi_rf_init()` is called before `imu_task_start()`
and `mic_task_start()` in `src/main.c` — that order is critical.

**Build error: `i2s_std.h: No such file`:**
PlatformIO platform is on ESP-IDF 4.x. Add to `platformio.ini`:
`platform = espressif32 @ ^6.0.0`

---

## Roadmap

- [x] MEMS microphone capture (I2S, 16 kHz, 1024-pt FFT)
- [x] Kurtosis, crest factor, high-band energy scoring
- [x] Adaptive z-score baseline (30-frame calibration per satellite)
- [x] Binary TCP streaming protocol (48-byte header + FFT arrays)
- [x] Multi-satellite gateway with per-satellite CSV logging
- [x] 7-state rhythm LED indicator (active-low, timer-driven)
- [x] Multi-satellite simulator (`satellite_sim.py`)
- [x] ISO bearing fault frequency calculator — BPFO/BPFI/BSF/FTF (`bearing_math.py`)
- [x] IsolationForest ML anomaly model training (`ml_trainer.py`)
- [x] Offline ML inference and fleet anomaly report (`ml_infer.py`)
- [x] Remaining Useful Life (RUL) estimate via kurtosis trend regression
- [x] Headless gateway mode for Uno Q / SSH (`--no-plot`)
- [x] Professional industrial web dashboard — dark theme, tabbed UI, live machine cards
- [x] Alert audit trail — compliance-ready log of every state transition
- [x] Maintenance log — per-machine service records, modal entry form, JSON persistence
- [x] HTTP Basic Auth on dashboard (`--auth USER:PASS`)
- [x] Emergency notifications — Discord/Slack/Teams webhook + SMTP email (`--notify-webhook`, `--notify-email`)
- [x] Printable HTML inspection reports — per-machine and factory-wide, PDF-ready
- [x] Factory-wide status overview — global risk level, 6 KPI summary tiles
- [ ] KX134 IMU real SPI DMA driver (replace stub in `imu_task.c`)
- [ ] Envelope analysis on IMU data (amplitude demodulation of bearing impacts)
- [ ] NTC thermistor ADC channel for motor temperature trending
- [ ] Deep-sleep burst mode for LiPo battery field deployment
- [ ] Offline Chart.js bundle (removes CDN dependency for air-gapped installs)

---

## Security Notes

- `src/wifi_creds.h` is gitignored and must **never** be committed. The firmware
  falls back to clearly non-functional placeholder credentials if the file is absent.
- The firmware enforces `WIFI_AUTH_WPA2_PSK` only — WPA/TKIP is rejected because
  TKIP is cryptographically broken and trivially crackable.
- The gateway's web dashboard binds to `0.0.0.0:8080`. Use `--auth USER:PASS` in
  any deployment where the LAN is not fully trusted.
- Do not expose port 8080 or 5100 to the public internet. Run behind a firewall or
  VPN for remote access.
