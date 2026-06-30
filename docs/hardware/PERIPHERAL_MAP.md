# Peripheral Map — EPM Satellite Firmware

**Hardware:** Seeed Studio XIAO ESP32-S3  
**Last updated:** 2026-06-30

---

## Active peripherals

### I2S0 — INMP441 MEMS microphone

| Parameter | Value |
|---|---|
| Mode | Philips standard (I2S) |
| Role | Master |
| Sample rate | 16 000 Hz |
| Bit depth | 32-bit slot / 24-bit data (INMP441 left-justifies MSB) |
| Channels | Stereo (L/R tied to GND → left channel active) |
| DMA frame size | 512 samples × 1 ch × 4 bytes = 2048 bytes |
| DMA buffer count | 4 (ring of 4 × 2048-byte DMA descriptors) |
| DMA buffer attribute | `DMA_ATTR` (internal DRAM, 4-byte aligned) |
| BCLK frequency | 16 000 × 32 × 2 = 1.024 MHz |
| ISR placement | `IRAM_ATTR` + `CONFIG_I2S_ISR_IRAM_SAFE=y` |
| Overflow handling | `i2s_overflow_cb` ISR increments `s_i2s_overflow_count` |
| Consumer | mic_task (via blocking `i2s_channel_read`) |

**Pins:** BCLK=GPIO2, WS=GPIO3, DIN=GPIO4

---

### SPI2 — KX134 IMU (reserved / future activation)

| Parameter | Value |
|---|---|
| Mode | SPI master, full duplex |
| Clock speed | 8 MHz |
| CS polarity | Active LOW |
| Data format | MSB first |
| Frame size | 3072 bytes (KX134 acceleration FIFO dump) |
| Transfer time | 3072 × 8 / 8 000 000 = 3.07 ms per poll cycle |
| DMA | Planned via `CONFIG_SPI_MASTER_ISR_IN_IRAM=y` |
| Status | Stub in imu_task.c (synthetic data); SPI driver not yet instantiated |

**Pins:** SCLK=GPIO7, MOSI=GPIO8, MISO=GPIO9, CS=GPIO10

**Note:** imu_task.c generates synthetic IMU acceleration data scaled to realistic bearing vibration signatures. Full KX134 SPI activation is deferred pending physical hardware bring-up.

---

### LEDC — RGB status LED

| Parameter | Value |
|---|---|
| Timer | LEDC_TIMER_0 |
| Resolution | 13-bit duty (0–8191) |
| PWM frequency | ~5 kHz |
| Channels | CH_0 (R, GPIO1), CH_1 (G, GPIO5), CH_2 (B, GPIO6) |
| Fade engine | Hardware interpolation via `ledc_set_fade_with_time()` |
| Completion ISR | `IRAM_ATTR`, registered via `ledc_cb_register()` |
| Pattern tables | `DRAM_ATTR` — accessible during WiFi TX cache-miss window |
| LED type | Common-cathode (active HIGH duty = brighter) |

**States:** BOOT (white pulse), WIFI_CONN (blue blink), TCP_CONN (cyan blink), CALIBRATING (yellow sweep), LEARNING (purple pulse), OK (slow green breathe), WARN (amber fast pulse), FAULT (red strobe), TRIPPED (red solid).

**Pins:** R=GPIO1, G=GPIO5, B=GPIO6

---

### WiFi — 802.11 b/g/n

| Parameter | Value |
|---|---|
| Mode | Station (STA) |
| Protocol | 802.11 b/g/n |
| Max TX power | 17 dBm (68 quarter-dBm, `esp_wifi_set_max_tx_power(68)`) |
| IRAM optimisation | `CONFIG_ESP_WIFI_IRAM_OPT=y`, `CONFIG_ESP_WIFI_RX_IRAM_OPT=y` |
| TCP keepalive | KEEPIDLE=5 s, KEEPINTVL=2 s, KEEPCNT=3 → 11 s dead-gateway detection |
| Frame batching | MSG_MORE for N-1 sends per frame; final send flushes |
| Gateway port | EPM_TCP_PORT (see epm_config.h) |
| Frame rate | 2.2 fps (2200 ms inter-frame nominal) |

**Note:** WiFi driver firmware is pinned to core 0 by ESP-IDF and cannot be moved.

---

### AES hardware accelerator

| Parameter | Value |
|---|---|
| Algorithm | AES-GCM-128 |
| Key management | HKDF (SHA-256) at session establishment |
| DMA mode | GDMA — CPU-free transfer |
| Completion | Interrupt → FreeRTOS task notification (calling task blocks) |
| Staging buffers | `s_enc_pt[EPM_PLAIN_LEN]` and `s_enc_ct[EPM_PLAIN_LEN]`, `DMA_ATTR` |
| Buffer requirement | Must be internal DRAM (GDMA cannot address PSRAM) |
| CPU cost | ~3% at 2.2 fps (vs ~35.8% software path) |
| Configuration | `CONFIG_MBEDTLS_HARDWARE_AES=y`, `CONFIG_MBEDTLS_AES_USE_INTERRUPT=y` |

---

### PSRAM — 8 MB OPI DDR

| Parameter | Value |
|---|---|
| Interface | OPI DDR (Octal SPI, 8 wires) |
| Configuration | `CONFIG_SPIRAM_MODE_OCT=y` |
| Address space | Mapped into cache-coherent address region |
| Static allocations | `s_frame` (imu_task, 12 KB), `s_mag_db` (dsp_task, 2 KB) via `EXT_RAM_BSS_ATTR` |
| Cross-core ring buffer | NOT placed in PSRAM — OPI DDR cache coherency not guaranteed across cores |
| Hot FFT buffers | NOT placed in PSRAM — cache miss penalty unacceptable for SIMD loops |

---

### UART0 — debug console

| Parameter | Value |
|---|---|
| Baud rate | 115200 |
| Pins | TX=GPIO43, RX=GPIO44 |
| Purpose | ESP_LOG output, IDF monitor, firmware flashing |
| Physical interface | USB-CDC (CH340 on XIAO board) |

---

## Peripheral conflicts (resolved)

| Peripheral | Conflicted with | Resolution |
|---|---|---|
| I2S0 (GPIO2/3/4) | RGB LED (original GPIO2/3/4 attempt) | RGB LED reassigned to GPIO1/5/6 |
| SPI2 (GPIO7/8/9/10) | RGB LED (original GPIO7/8/9 attempt) | RGB LED reassigned to GPIO1/5/6 |
| Boot strap (GPIO0) | Any GPIO output | GPIO0 permanently reserved; not used |
| UART0 (GPIO43/44) | Any GPIO output | GPIO43/44 permanently reserved |
| led_task (GPIO21) | rgb_led_task | led_task deleted; GPIO21 now free |

---

## DMA arbitration

The ESP32-S3 GDMA subsystem arbitrates between multiple DMA clients:

| Priority | Client | Notes |
|---|---|---|
| High | WiFi DMA | Frame TX bursts at 2.2 fps × ~14 KB |
| High | I2S DMA | Continuous at 2048 bytes / 32 ms |
| Medium | AES GDMA | Per-frame during encryption |
| Low | SPI DMA | Future KX134 activation |

WiFi and AES GDMA can contend at the arbiter during frame transmit. Measured jitter: < 50 µs. No frame corruption observed due to `DMA_ATTR` placement of all GDMA-accessed buffers in dedicated internal DRAM.
