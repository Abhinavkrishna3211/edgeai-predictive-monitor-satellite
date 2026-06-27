# EdgeAI Predictive Monitor — Satellite Node

A wireless multi-satellite bearing-fault detection system for industrial motors.
XIAO ESP32-S3 sensor nodes stream real-time FFT data over WiFi to a Python gateway
that applies statistical + ML anomaly detection, logs CSV data, and serves a live
web dashboard accessible from any device on the LAN.

---

## System Overview

```
┌────────────────────────────────────────────────────────────┐
│  XIAO ESP32-S3  (satellite node)  ×N                       │
│                                                            │
│  INMP441 mic ─► I2S ─► 1024-pt FFT ×4 avg  (16 kHz)      │
│  KX134 IMU   ─► SPI ─► 2048-pt FFT ×3 axes (25.6 kHz)    │
│                            ↓                               │
│  led_task ◄── alert byte ◄── TCP ──► gateway               │
└────────────────────────────────────────────────────────────┘
                           WiFi / TCP
┌────────────────────────────────────────────────────────────┐
│  Laptop / Arduino Uno Q  (gateway + AI engine)             │
│                                                            │
│  recv_verify.py                                            │
│   ├─ 30-frame adaptive baseline (z-score calibration)      │
│   ├─ kurtosis + crest factor + high-band energy scoring    │
│   ├─ IsolationForest ML anomaly detection (optional)       │
│   ├─ CSV log → mic_tools/logs/  (per satellite per day)    │
│   ├─ Live FFT + waterfall plot (matplotlib, optional)      │
│   ├─ Web dashboard  http://<host>:8080/                    │
│   └─ 1-byte alert reply to satellite  0x00/0x01/0x02       │
└────────────────────────────────────────────────────────────┘
```

Multiple satellites connect simultaneously.  Each is tracked, calibrated, and scored
independently.  The dashboard shows all nodes in one view, accessible from any
phone, laptop, or Arduino Uno Q on the same WiFi network.

---

## Hardware

| Component | Notes |
|-----------|-------|
| Seeed XIAO ESP32-S3 | Dual-core LX7 @ 160 MHz, built-in WiFi, USB-C |
| INMP441 or ICS-43434 | External I2S MEMS microphone — wire to D1/D2/D3 |
| KX134 3-axis IMU | SPI, 25.6 kHz ODR — bolt to motor casing |
| 2.4 GHz AP / hotspot | Windows / Android / iPhone hotspot all work |

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
│   └── wifi_creds.h        # ← NOT IN REPO — create manually (Step 2)
│
├── components/
│   └── mic_capture/        # Reusable I2S MEMS capture component
│       ├── mic_capture.c
│       └── include/mic_capture.h
│
├── mic_tools/
│   ├── recv_verify.py      # Gateway: receive, score, alert, CSV log, plot, dashboard
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
- Python 3.9+

### 2. Create `src/wifi_creds.h`

Create this file manually — it is gitignored and will never be committed:

```c
// src/wifi_creds.h — gitignored, never in the repo
#pragma once
#define WIFI_SSID    "YourNetworkName"
#define WIFI_PASS    "YourPassword"
#define SERVER_IP    "192.168.137.1"   // gateway IP — see table below
#define SERVER_PORT  5100
```

Common gateway IPs by hotspot type:

| Hotspot type | Default gateway IP |
|--------------|--------------------|
| Windows Mobile Hotspot | `192.168.137.1` |
| Android hotspot        | `192.168.43.1`  |
| iPhone hotspot         | `172.20.10.1`   |
| macOS Internet Sharing | `192.168.2.1`   |
| Home router            | Run `ipconfig` (Windows) or `ip route get 1` (Linux/Mac) |

### 3. Build and Flash

**PlatformIO (recommended):**
```bash
# In the project root:
pio run --target upload --environment xiao_esp32s3
pio device monitor
```

**ESP-IDF directly:**
```bash
idf.py -p COM9 flash monitor    # adjust port for your system
```

Watch the serial output — it prints the WiFi connection status, IP address, and
per-frame stats once running.  The LED tells you the current state instantly.

---

## Quick Start — Gateway

### 1. Install Python dependencies

```bash
cd mic_tools
pip install -r requirements.txt
```

### 2. Start the gateway

```bash
# Basic — listens on 0.0.0.0:5100, opens a live FFT plot window
python recv_verify.py

# Headless — no plot window (SSH / Uno Q / server with no display)
python recv_verify.py --no-plot

# With shaft speed markers on all FFT panels (e.g. 1500 RPM motor)
python recv_verify.py --shaft-rpm 1500

# With bearing fault frequency markers (6205 bearing, 1500 RPM)
python recv_verify.py --shaft-rpm 1500 --bearing 6205

# With ML-based alerting (after running ml_trainer.py)
python recv_verify.py --model model/epm_model

# Override alert thresholds
python recv_verify.py --crest-warn 4.5 --crest-fault 9.0
```

### 3. Open the web dashboard

The terminal prints the exact URLs when it starts:

```
http://localhost:8080/     ← open on this machine
http://192.168.x.x:8080/  ← open on phone or any device on LAN
```

On Windows, the terminal also prints a one-time firewall command to run in an
elevated PowerShell so other devices can reach the dashboard.

The gateway will:
1. Accept connections from up to 16 satellites simultaneously
2. Calibrate a 30-frame adaptive baseline per satellite (z-score reference)
3. Score each frame: kurtosis + crest factor + high-band energy + z-score
4. Send `0x00` OK / `0x01` WARN / `0x02` FAULT back to each satellite
5. Log every frame to `mic_tools/logs/epm_<name>_<YYYYMMDD>.csv`
6. Serve a live dashboard with per-satellite health score and RUL estimate

---

## Testing Without Hardware — Satellite Simulator

```bash
# Terminal 1: start gateway
python recv_verify.py

# Terminal 2: simulate 3 healthy satellites
python satellite_sim.py 127.0.0.1 5100 3

# Inject fault conditions — test alert logic and LED patterns
python satellite_sim.py 127.0.0.1 5100 5 --fault 1 --warn 2
```

Each simulated satellite:
- Has a unique fake MAC (appears as a distinct node in the dashboard)
- Sends realistic FFT data with axis-distinct IMU signals (50/100/150 Hz tones)
- Injects bearing-resonance energy in the 2–4 kHz band for fault/warn modes
- Auto-reconnects if the gateway restarts
- Starts 0.35 s staggered so the gateway sees them arrive naturally

---

## Bearing Fault Analysis

`bearing_math.py` computes ISO standard bearing defect frequencies from geometry
and shaft speed.

```bash
# Print BPFO / BPFI / BSF / FTF for bearing 6205 at 1500 RPM
python bearing_math.py 6205 1500

# List all 18 built-in bearing geometries (6200-6210, 6304-6310)
python bearing_math.py 6205 1500 --list

# Custom geometry: n=9 balls, D=38.5 mm pitch, d=10.3 mm ball
python bearing_math.py 9,38.5,10.3 1500

# With contact angle (angular-contact bearings)
python bearing_math.py 9,38.5,10.3,15 1500
```

Run with the gateway for colored fault frequency markers on every FFT panel:

```bash
python recv_verify.py --shaft-rpm 1500 --bearing 6205
```

Color coding on the FFT plots:

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

Run `recv_verify.py` with real or simulated equipment for at least 30 minutes of
healthy operation.  Each satellite logs to `mic_tools/logs/epm_<name>_<YYYYMMDD>.csv`.

### 2. Train the model

```bash
cd mic_tools

# Train on all satellites (all CSVs in logs/)
python ml_trainer.py

# Train on one satellite only
python ml_trainer.py --satellite SAT-A3B4

# Tune expected fault fraction and tree count
python ml_trainer.py --contamination 0.03 --n-estimators 300

# Save to a custom prefix
python ml_trainer.py --output model/my_model
```

This writes `model/epm_model_iso.joblib` and `model/epm_model_meta.json`.

### 3. Deploy the model

```bash
python recv_verify.py --model model/epm_model
```

The ML model runs alongside the threshold detector — the more severe of the two
alerts is used.  Inference activates only after each satellite's 30-frame baseline.

### 4. Offline analysis

```bash
# Analyse all logs, compare ML vs threshold alerts
python ml_infer.py

# Show the 20 worst anomaly frames across all satellites
python ml_infer.py --top-anomalies 20

# Export per-frame predictions
python ml_infer.py --export anomaly_report.csv
```

---

## Physical AI on Arduino Uno Q

The Arduino Uno Q (dual Arm Cortex, 1 GB RAM, Python-capable) can run the full
gateway stack without a laptop.

```bash
# On the Uno Q — headless mode, no display needed
python recv_verify.py --no-plot --dashboard-port 8080
```

Open `http://<uno-q-ip>:8080/` from any phone or browser on the LAN.

The Uno Q receives satellite streams, runs anomaly scoring, logs CSVs, and serves
the dashboard — all standalone.  For ML training, copy the `logs/` directory to a
PC, run `ml_trainer.py`, then copy `model/` back to the Uno Q.

---

## Battery Efficiency on XIAO

The firmware calls `esp_wifi_set_ps(WIFI_PS_NONE)` — full power, best throughput.
Typical draw ~80–200 mA at 3.3 V on active WiFi.

Options for longer battery life:

| Change | Location | Effect |
|--------|----------|--------|
| `WIFI_PS_MIN_MODEM` | `wifi_task.c` line 377 | ~30% lower WiFi power, ≤100 ms extra latency |
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
WiFi not connecting.  Check SSID/password in `wifi_creds.h`.  Serial monitor shows
the reason code: 15/203 = wrong password, 200 = SSID not found.

**LED stuck on 3-tap WiFi blink:**
`SERVER_IP` doesn't match your hotspot type — see the IP table in Step 2.
Confirm `recv_verify.py` is running before the satellite boots.

**No satellites in dashboard / gateway shows no connects:**
Firewall blocking port 5100 or 8080.  Run the `New-NetFirewallRule` command the
gateway prints at startup (elevated PowerShell, once).
macOS/Linux: `sudo ufw allow 5100 && sudo ufw allow 8080`

**Plot window doesn't appear:**
Use `--no-plot` mode.  Required on SSH sessions, Uno Q, and WSL without an X server.

**`satellite_sim.py` prints "Connection refused":**
Start `recv_verify.py` first, then the simulator.

**`TG1WDT_SYS_RST` crash on boot:**
Mitigated by `sdkconfig.defaults` (`CONFIG_ESP_INT_WDT_TIMEOUT_MS=1200`).
If it recurs, verify that `wifi_rf_init()` is called before `imu_task_start()`
and `mic_task_start()` in `src/main.c` — that order is critical.

**Build error: `i2s_std.h: No such file`:**
PlatformIO platform is on ESP-IDF 4.x.  Add to `platformio.ini`:
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
- [x] Live web dashboard — health score, RUL estimate, alert chart, LAN access
- [x] ISO bearing fault frequency calculator — BPFO/BPFI/BSF/FTF (`bearing_math.py`)
- [x] IsolationForest ML anomaly model training (`ml_trainer.py`)
- [x] Offline ML inference and fleet anomaly report (`ml_infer.py`)
- [x] Remaining Useful Life (RUL) estimate via kurtosis trend regression
- [x] Headless gateway mode for Uno Q / SSH (`--no-plot`)
- [ ] KX134 IMU real SPI DMA driver (replace stub in `imu_task.c`)
- [ ] Envelope analysis on IMU data (amplitude demodulation of bearing impacts)
- [ ] NTC thermistor ADC channel for motor temperature trending
- [ ] Deep-sleep burst mode for LiPo battery field deployment

---

## Security Notes

- `src/wifi_creds.h` is gitignored and must **never** be committed.  The firmware
  falls back to clearly non-functional placeholder credentials if the file is absent.
- The firmware enforces `WIFI_AUTH_WPA2_PSK` only — WPA/TKIP is rejected because
  TKIP is cryptographically broken and trivially crackable.
- The gateway's web dashboard binds to `0.0.0.0:8080` — anyone on the LAN can view
  it.  Do not run on an untrusted public network without adding authentication.
