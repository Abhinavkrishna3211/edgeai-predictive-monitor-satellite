# EdgeAI Predictive Monitor — Satellite Node

A wireless bearing-fault detection system using an XIAO ESP32-S3 microphone + KX134 IMU as a satellite sensor node, with a Python gateway running on any laptop. Designed to detect motor bearing faults before they fail by streaming spectral data over WiFi and scoring it with statistical anomaly detection.

---

## System Overview

```
  ┌─────────────────────────────────────────────────┐
  │  XIAO ESP32-S3 (satellite node)                 │
  │                                                 │
  │  MEMS mic ─► I2S ─► FFT (1024-pt, ×4 avg)     │
  │  KX134 IMU ─► SPI ─► FFT (2048-pt, ×4 avg) ×3 │
  │                          ↓                     │
  │  led_task ◄── alert byte ◄── TCP ──► gateway   │
  └─────────────────────────────────────────────────┘
                         WiFi / TCP
  ┌─────────────────────────────────────────────────┐
  │  Laptop / Uno Q (gateway)                       │
  │                                                 │
  │  recv_verify.py                                 │
  │   ├─ calibrate 30-frame baseline                │
  │   ├─ z-score + kurtosis + high-band energy      │
  │   ├─ CSV log  (mic_tools/logs/)                 │
  │   ├─ live FFT / waterfall plot                  │
  │   └─ 1-byte alert reply (OK / WARN / FAULT)    │
  └─────────────────────────────────────────────────┘
```

Multiple satellites can connect simultaneously. Each is tracked independently; the gateway keeps a live table of all connected nodes.

---

## Hardware

| Component | Notes |
|-----------|-------|
| Seeed XIAO ESP32-S3 | Dual-core LX7 @ 160 MHz, built-in WiFi |
| MEMS microphone (I2S) | Built-in PDM mic on XIAO ESP32-S3 Sense |
| KX134 3-axis IMU | SPI, 25.6 kHz ODR — bolt to motor casing |
| Any 2.4 GHz AP / hotspot | Windows/Android/iPhone hotspot works |

The KX134 IMU is not yet wired (stub driver in `src/imu_task.c`). The microphone path is fully functional today.

---

## Repository Layout

```
edgeai-predictive-monitor-satellite/
├── src/
│   ├── main.c              # app_main: system init, task start order
│   ├── led_task.c/h        # 7-state LED state machine (100 ms esp_timer)
│   ├── mic_task.c/h        # I2S capture + windowed FFT, kurtosis
│   ├── imu_task.c/h        # KX134 SPI driver (stub — real driver pending)
│   ├── wifi_task.c/h       # WiFi STA + TCP streaming + alert RX
│   ├── epm_config.h        # All compile-time tunables
│   ├── epm_protocol.h      # Binary wire format structs (48-byte header)
│   └── idf_component.yml   # IDF Component Manager manifest
│
├── mic_tools/
│   ├── recv_verify.py      # Gateway: receive frames, score, log CSV, live plot
│   ├── satellite_sim.py    # Simulator: test gateway without hardware
│   ├── plot_mic.py         # LEGACY serial tool (Phase 1 only — do not use)
│   └── requirements.txt
│
├── CMakeLists.txt          # Root ESP-IDF project file
├── platformio.ini          # PlatformIO build config
├── sdkconfig.defaults      # ESP-IDF KConfig overrides (performance tuning)
└── .gitignore
```

> `src/wifi_creds.h` is **not in this repo** (gitignored). You must create it — see below.

---

## Quick Start — Satellite Firmware

### 1. Prerequisites

- [PlatformIO](https://platformio.org/) with the Espressif32 platform installed, **or**
- ESP-IDF v5.x (`idf.py` in PATH)
- Python 3.9+ (for mic_tools)

### 2. Create `src/wifi_creds.h`

This file is gitignored and must never be committed. Create it manually:

```c
// src/wifi_creds.h — NOT committed, gitignored
#pragma once
#define WIFI_SSID    "YourNetworkName"
#define WIFI_PASS    "YourPassword"
#define SERVER_IP    "192.168.137.1"   // gateway IP (laptop hotspot, Android, etc.)
#define SERVER_PORT  5100
```

Common gateway IPs by hotspot type:

| Hotspot | Default gateway IP |
|---------|--------------------|
| Windows | `192.168.137.1` |
| Android | `192.168.43.1` |
| iPhone  | `172.20.10.1` |
| macOS   | `192.168.2.1` |

### 3. Build and Flash (PlatformIO)

```bash
# In the project root:
pio run --target upload --environment xiao_esp32s3
pio device monitor
```

Or with ESP-IDF directly:

```bash
idf.py -p COM9 flash monitor    # adjust port as needed
```

The LED will start solid ON immediately, then blink once connected — see [LED Indicator](#led-indicator) below.

---

## Quick Start — Gateway

### 1. Install dependencies

```bash
cd mic_tools
pip install -r requirements.txt
```

### 2. Run the gateway

```bash
python recv_verify.py
```

Default: listens on `0.0.0.0:5100`. Options:

```
python recv_verify.py --port 5100
python recv_verify.py --fft-mic-n 1024 --fft-imu-n 2048
python recv_verify.py --shaft-hz 50     # mark shaft harmonics on FFT plot (e.g. 3000 RPM)
```

The gateway will:
1. Accept connections from any satellite
2. Run a 30-frame calibration baseline per satellite (measures RMS + kurtosis distribution)
3. Send `0x00` OK / `0x01` WARN / `0x02` FAULT back to the satellite after every frame
4. Log every frame to `mic_tools/logs/epm_<name>_<YYYYMMDD>.csv`
5. Show a live FFT spectrum + waterfall plot

---

## Testing Without Hardware — Satellite Simulator

`satellite_sim.py` emulates N satellites on your machine. Use this to test the gateway, alert logic, and multi-satellite behaviour before hardware arrives:

```bash
# 3 satellites all healthy
python satellite_sim.py 127.0.0.1 5100 3

# 5 satellites: sat-1 at FAULT level, sat-2 at WARN level, rest OK
python satellite_sim.py 127.0.0.1 5100 5 --fault 1 --warn 2
```

Each simulated satellite:
- Gets a unique fake MAC (`AA:BB:CC:DD:00:XX`)
- Sends realistic FFT data (pink noise baseline + bearing resonance for fault/warn)
- Auto-reconnects if the gateway restarts
- Staggered start (0.35 s apart) so the gateway sees real-looking sequential connects

---

## LED Indicator

GPIO21 on the XIAO ESP32-S3 (active-low: LOW = ON). All patterns are generated by a 100 ms `esp_timer` — no FreeRTOS task needed.

All patterns are **rhythm-based** — distinguishable by counting taps, not estimating speed.

| State | Pattern | Period | Meaning |
|-------|---------|--------|---------|
| `LED_BOOT` | Solid ON | — | Power-on, starting up |
| `LED_WIFI_CONN` | **3 quick taps** then 0.7 s dark | 1.0 s | Scanning for WiFi AP |
| `LED_TCP_CONN` | Slow 0.5 Hz blink (1 s ON / 1 s OFF) | 2.0 s | WiFi ok, connecting to gateway |
| `LED_CALIBRATING` | **2 quick taps** then 1.8 s dark | 2.0 s | Connected, learning baseline (first 30 frames) |
| `LED_OK` | Single 100 ms blip, mostly off | 3.0 s | Healthy, normal vibration |
| `LED_WARN` | Steady 1 Hz blink (500/500 ms) | 1.0 s | Elevated kurtosis / crest factor |
| `LED_FAULT` | Continuous 5 Hz strobe (uncountable) | 0.2 s | Bearing fault detected |

**How to tell them apart:** BOOT=solid · WIFI=count 3 taps · TCP=slow lazy blink · CAL=count 2 taps · OK=rare blip · WARN=car-hazard 1 Hz · FAULT=rapid strobe alarm

---

## Wire Protocol

All multi-byte fields are **little-endian**.

### Satellite → Gateway (per frame)

```
[uint32_t  payload_bytes]     4 bytes   (does not count itself)
[epm_header_t header]        48 bytes
[float mic_fft[512]]       2048 bytes   (FFT_MIC_N/2 bins, dBFS)
[float imu_x_fft[1024]]    4096 bytes   (radial A)
[float imu_y_fft[1024]]    4096 bytes   (radial B)
[float imu_z_fft[1024]]    4096 bytes   (axial)
```

Total per frame: **~14.3 KB**

### Hello packet (sent once on connect, before first frame)

```
[uint32_t  magic]    4 bytes   0xEA1D0000
[uint8_t   mac[6]]   6 bytes   WiFi STA MAC
[uint8_t   fw_major] 1 byte
[uint8_t   fw_minor] 1 byte
[char      name[12]] 12 bytes  null-padded, e.g. "SAT-A3B4"
```

### Gateway → Satellite (1 byte after each frame)

| Value | Meaning |
|-------|---------|
| `0x00` | OK |
| `0x01` | WARN |
| `0x02` | FAULT |

---

## Alert Thresholds

Thresholds are in `mic_tools/recv_verify.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CAL_FRAMES` | 30 | Frames to collect before classifying (baseline learning) |
| `CREST_WARN` | 5.0 | Mic crest factor threshold for WARN |
| `CREST_FAULT` | 8.0 | Mic crest factor threshold for FAULT |
| `K_WARN` | 6.0 | Kurtosis threshold for WARN (Gaussian noise ≈ 3) |
| `K_FAULT` | 12.0 | Kurtosis threshold for FAULT (advanced bearing damage) |
| `HIGH_BAND_MIN` | 0.12 | Minimum fraction of mic energy in 2–8 kHz band to count alert |
| `WARN_PERSIST` | 2 | Consecutive WARN frames before raising alert |
| `CLEAR_PERSIST` | 3 | Consecutive OK frames before clearing alert |

`HIGH_BAND_MIN` prevents factory floor rumble (< 500 Hz) from triggering false bearing alerts — bearing defects excite 2–8 kHz resonances.

---

## ML Training Data

Every frame is logged to `mic_tools/logs/epm_<name>_<YYYYMMDD>.csv`:

```
wall_time, frame_id, device_ms,
mic_rms, mic_crest, mic_kurtosis,
imu_rms, imu_crest,
high_band_ratio, z_score, alert
```

One file per satellite per calendar day — reconnects append, not overwrite.

Once you have a few hours of data (healthy + fault conditions), train an anomaly detector:

```python
# Example (in ml_trainer.py — coming soon)
import pandas as pd
from sklearn.ensemble import IsolationForest

df = pd.read_csv('logs/epm_SAT-A3B4_20260627.csv')
X  = df[['mic_rms', 'mic_kurtosis', 'high_band_ratio', 'z_score']].values
clf = IsolationForest(contamination=0.05).fit(X)
# Deploy clf to Arduino Uno Q as ml_infer.py
```

---

## Roadmap

- [x] MEMS microphone capture (I2S, 16 kHz, 1024-pt FFT)
- [x] Kurtosis, crest factor, high-band energy scoring
- [x] Binary TCP streaming protocol (48-byte header + FFT arrays)
- [x] Multi-satellite gateway with CSV logging
- [x] 7-state LED indicator (active-low, timer-driven)
- [x] Multi-satellite simulator (`satellite_sim.py`)
- [ ] KX134 IMU SPI DMA driver (bearing frequency detection: BPFI/BPFO/FTF/BSF)
- [ ] Envelope analysis on IMU data
- [ ] IsolationForest training script (`ml_trainer.py`)
- [ ] Deploy trained model to Arduino Uno Q (`ml_infer.py`)
- [ ] NTC thermistor ADC channel for motor temperature trending

---

## Security Note

`src/wifi_creds.h` contains WiFi credentials and must **never** be committed. It is in `.gitignore`. The firmware falls back to placeholder values (`EPM_Hotspot` / `epm12345`) if the file is absent — these are clearly non-functional defaults. Do not add real credentials to `epm_config.h`.
