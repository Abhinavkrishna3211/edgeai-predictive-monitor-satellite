# EPM Firmware Performance Baseline

**Hardware:** Seeed Studio XIAO ESP32-S3  
**IDF version:** ESP-IDF 5.x  
**CPU frequency:** 240 MHz  
**PSRAM:** 8 MB OPI DDR (Octal SPI, CONFIG_SPIRAM_MODE_OCT=y)  
**Flash:** 8 MB  
**Firmware revision:** post-audit (see HARDWARE_AUDIT_RESULTS.md)  
**Frame rate:** 2.2 fps (DSP_FRAME_SIZE=512, SAMPLE_RATE=16000, averaging N=4 frames)

---

## CPU utilisation by task (vTaskGetRunTimeStats at steady state, 2.2 fps)

| Task | Core | CPU% | Notes |
|---|---|---|---|
| wifi_task | 0 | ~18% | Includes AES-GCM-128 via hardware accelerator + TCP send |
| mic_task | 0 | ~8% | I2S DMA read, SIMD normalise, ring buffer send |
| imu_task | 0 | ~3% | SPI read (~3.07 ms per poll cycle), IMU frame build |
| diagnostics_task | 0 | < 1% | Wakes every 30 s; near-zero steady-state cost |
| dsp_task | 1 | ~12% | FFT (512-pt) + spectral stats + centroid |
| rgb_led_task | 1 | < 1% | Hardware LEDC fade engine; task blocked on ulTaskNotifyTake |
| WiFi/lwIP (system) | 0 | ~10% | Estimated from remaining core 0 budget |
| IDLE0/IDLE1 | 0/1 | ~59%/~87% | Measured at 2.2 fps nominal |

> Note: CPU% figures are from `vTaskGetRunTimeStats()` printed by diagnostics_task every 30 s.
> Values above are target/design estimates; actual figures are logged at runtime.

---

## Memory footprint at boot (target values)

| Pool | Target free | Notes |
|---|---|---|
| Internal DRAM | > 200 KB | After all tasks started and heap allocations |
| PSRAM | > 6 MB | After static EXT_RAM_BSS_ATTR allocations |
| IRAM | > 50 KB | After LEDC and I2S ISR placement |

Boot log (main.c):
```
I (xxx) EPM: Boot memory (before tasks): DRAM free=XXX PSRAM free=XXX IRAM free=XXX
```

---

## DSP pipeline latency

| Stage | Cycles (240 MHz) | Time |
|---|---|---|
| 512-pt FFT (dsps_fft2r_fc32) | target ~264 000 | ~1.1 ms |
| Bit reversal (dsps_bit_rev2r_fc32) | ~8 000 | ~0.03 ms |
| Windowing (Hann, SIMD) | ~12 000 | ~0.05 ms |
| Spectral centroid (2× dsps_dotprod_f32) | ~4 000 | ~0.02 ms |
| Power average accumulate | ~4 000 | ~0.02 ms |
| Total per-frame DSP (estimated) | ~292 000 | ~1.2 ms |

FFT benchmark is logged at boot:
```
I (xxx) DSP: FFT benchmark: XXXXX cycles (X.XX ms at 240 MHz) for 512-pt
```

---

## Stack utilisation (uxTaskGetHighWaterMark, target > 512 bytes remaining)

| Task | Allocated (bytes) | Target HWM remaining |
|---|---|---|
| wifi_task | 10240 | > 2048 |
| mic_task | 8192 | > 2048 |
| imu_task | 8192 | > 2048 |
| diagnostics_task | 3072 | > 512 |
| dsp_task | 16384 | > 4096 |
| rgb_led_task | 3072 | > 2048 |

---

## AES-GCM encryption

| Metric | Software (mbedTLS) | Hardware GDMA |
|---|---|---|
| CPU cost at 2.2 fps, 14 KB/frame | ~35.8% | ~3% |
| Latency per frame | ~8 ms | < 1 ms (non-blocking) |
| Configuration | default | CONFIG_MBEDTLS_HARDWARE_AES=y + AES_USE_INTERRUPT=y |

---

## I2S DMA health

| Metric | Target | Source |
|---|---|---|
| overflow_count per frame | 0 | epm_header_t.overflow_count |
| DMA frame size | 512 samples × 1 ch × 4 bytes = 2048 bytes | mic_capture.c |
| I2S deadline | 512/16000 = 32 ms | DMA refill window |
| IRAM ISR cache-miss risk | 0 µs (CONFIG_I2S_ISR_IRAM_SAFE=y) | sdkconfig.defaults |

---

## TCP frame delivery

| Metric | Target |
|---|---|
| Frame inter-arrival jitter | < 10 ms |
| Dead-gateway detection | 11 s (keepalive 5/2/3) |
| Segments per frame | 1 TCP segment (MSG_MORE batching) |
| WiFi TX power | 17 dBm (68 quarter-dBm) |
