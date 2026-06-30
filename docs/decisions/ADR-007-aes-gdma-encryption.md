---
id: ADR-007
title: AES-GCM-128 via ESP32-S3 hardware accelerator and GDMA
status: accepted
date: 2026-06-30
deciders: Abhinav Krishna N
---

## Context

Each EPM frame is ~14 KB (EPM_PLAIN_LEN bytes of protocol header + payload). Frames are encrypted with AES-GCM-128 before TCP transmission to protect sensor data in transit. At the target frame rate of 2.2 fps, encryption runs continuously. Three implementation strategies were evaluated for CPU cost and correctness.

## Options considered

### Option A: Software AES-GCM (mbedTLS default)
**Evidence:** mbedTLS ships a portable C implementation of AES-GCM. When hardware acceleration is not configured, mbedTLS falls back to this software path automatically.
**Pros:** Zero configuration; works on any ESP32 variant.
**Cons:** Measured at 2.2 fps with 14 KB frames: ~35.8% of core 0 CPU consumed by AES-GCM alone (vTaskGetRunTimeStats). This leaves only 64% for WiFi driver, I2S DMA handling, IMU SPI polling, and TCP socket operations — all on the same core. Under peak concurrent load (WiFi TX burst + I2S DMA refill + AES encrypt), this causes measurable frame drops.

### Option B: Hardware AES with CPU-driven DMA
**Evidence:** ESP32-S3 has a hardware AES accelerator (128/192/256-bit). Enabling CONFIG_MBEDTLS_HARDWARE_AES=y routes mbedTLS AES-GCM through the hardware engine. Without AES_USE_INTERRUPT, the CPU still polls the hardware completion register.
**Pros:** ~4–5× throughput improvement over software.
**Cons:** CPU still spins waiting for hardware to signal completion. For a 14 KB block, this is a 100–200 µs busy wait per frame, preventing lower-priority tasks from running during encryption. Not truly zero-copy.

### Option C: Hardware AES + GDMA + interrupt
**Evidence:** CONFIG_MBEDTLS_HARDWARE_AES=y + CONFIG_MBEDTLS_AES_USE_INTERRUPT=y routes AES-GCM through the GDMA subsystem. The DMA controller transfers plaintext to the AES hardware and ciphertext back to the output buffer without CPU involvement. A DMA completion interrupt wakes the calling task (mbedTLS task notification).

CPU cost measured: ~3% of core 0 at 2.2 fps with 14 KB frames (diagnostics_task vTaskGetRunTimeStats, stable reading over 60 s).

**DMA buffer placement requirement:** AES GDMA requires source and destination buffers in internal SRAM (not PSRAM). If buffers are placed in PSRAM, the GDMA engine either fails silently or generates a cache coherency error during concurrent WiFi GDMA access. Buffers `s_enc_pt` and `s_enc_ct` (~14 KB each) are marked `DMA_ATTR` in wifi_task.c:
```c
static DMA_ATTR uint8_t s_enc_pt[EPM_PLAIN_LEN];
static DMA_ATTR uint8_t s_enc_ct[EPM_PLAIN_LEN];
```
`DMA_ATTR` expands to `__attribute__((aligned(4))) DRAM_ATTR`, ensuring 4-byte alignment and internal DRAM placement.

CONFIG_MBEDTLS_HARDWARE_SHA=y routes SHA-256 (used for HKDF key derivation at session startup) through the same hardware accelerator to reduce startup latency.

**Pros:** CPU cost drops from ~35.8% to ~3% for encryption. GDMA transfer overlaps with other CPU work. Interrupt-driven completion is compatible with FreeRTOS task notification — calling task blocks rather than spinning.
**Cons:** Requires DMA_ATTR on both staging buffers (~28 KB of internal DRAM consumed). GDMA and WiFi GDMA share the same DMA arbiter — concurrent bursts are serialised at the arbiter, adding latency jitter (< 50 µs measured).

## Decision
**Chosen: Option C — hardware AES + GDMA + interrupt**

**Justification:** The 35.8% → 3% CPU reduction is the decisive factor. At 2.2 fps, software AES was consuming over one-third of core 0, starving WiFi and I2S servicing. Option C brings the encryption cost below the measurement noise floor of diagnostics_task's 10 ms sampling interval.

**Configuration flags** (in sdkconfig.defaults):
```
CONFIG_MBEDTLS_HARDWARE_AES=y
CONFIG_MBEDTLS_HARDWARE_SHA=y
CONFIG_MBEDTLS_AES_USE_INTERRUPT=y
```

## Consequences
**Positive:**
- Encryption CPU cost: ~3% (from ~35.8%) at 2.2 fps
- Frame drops during WiFi TX bursts eliminated
- WiFi and I2S ISR latency unaffected by encryption timing

**Negative / trade-offs:**
- `s_enc_pt` and `s_enc_ct` consume ~28 KB of internal DRAM permanently
- GDMA arbiter serialises WiFi GDMA and AES GDMA — a 50 µs jitter window exists when both fire simultaneously
- CONFIG_MBEDTLS_AES_USE_INTERRUPT=y requires FreeRTOS task notification support in mbedTLS (default in ESP-IDF ≥ 5.0)

**Metrics to watch:**
- wifi_task CPU% in vTaskGetRunTimeStats (includes AES; target: < 20% combined)
- Heap free after task start (DMA_ATTR buffers reduce internal free heap by ~28 KB vs PSRAM placement)
- Frame timestamp jitter at gateway (target: < 10 ms variation at 2.2 fps)

## Validation
`sdkconfig.defaults` — three MBEDTLS flags present. `wifi_task.c` — `DMA_ATTR` on both staging buffers confirmed. `diagnostics_task` logs heap free and CPU% every 30 s; cpu% for wifi_task target < 20%.
