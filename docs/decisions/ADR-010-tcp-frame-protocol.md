---
id: ADR-010
title: TCP frame protocol design — framing, batching, and keepalive
status: accepted
date: 2026-06-30
deciders: Abhinav Krishna N
---

## Context

The EPM satellite transmits sensor frames at 2.2 fps over a TCP socket to a gateway running on the local network. Each frame consists of a 48-byte header (`epm_header_t`) followed by compressed sensor payload segments (IMU frame, FFT magnitudes, statistics). The protocol must be reliable (no silent frame corruption), low-latency (gateway processes frames in real time), and resilient to gateway disconnects. Three areas were evaluated: framing, per-frame TCP segment batching, and dead-gateway detection.

## Framing design

### Options
**Option A: Single large send():** Send the entire ~14 KB frame as one call. Requires assembling all segments into a contiguous buffer first — extra copy.
**Option B: Multiple send() calls per frame, one per segment:** 6 send() calls for a typical frame. Each send() may flush immediately (TCP_NODELAY) or wait for a full MSS (default Nagle). Without MSG_MORE, Nagle coalesces badly — small header send triggers 200 ms delayed-ACK wait on the receiver.
**Option C: MSG_MORE batching:** Send the first N-1 segments with `MSG_MORE` flag, send the final segment normally. MSG_MORE defers the TCP PUSH bit, telling the stack to buffer outgoing data until the final send. The receiver sees one TCP segment containing all 6 sub-sends.

**Chosen: Option C — MSG_MORE batching**

`tcp_send_more()` helper in wifi_task.c:
```c
static int tcp_send_more(int sock, const void *data, size_t len) {
    int flags = MSG_MORE;
    return send(sock, data, len, flags);
}
```
Final segment uses `send(sock, data, len, 0)` (no MSG_MORE) to flush.

**Result:** 6 small sends → 1 TCP segment. Eliminates Nagle-induced 200 ms stall per frame. Frame inter-arrival jitter at gateway: < 10 ms.

## TCP keepalive

### Problem
Without keepalive, a gateway crash or network partition leaves the ESP32 socket in ESTABLISHED state indefinitely. The socket send buffer fills, `send()` blocks, and the WiFi task hangs, causing a watchdog reset. The default TCP keepalive idle timer is 75 seconds — too slow to detect a dead gateway before the send buffer fills.

### Decision: TCP keepalive 5/2/3
```c
int keepidle = 5;    // seconds idle before first probe
int keepintvl = 2;   // seconds between probes
int keepcnt = 3;     // probes before giving up
setsockopt(sock, IPPROTO_TCP, TCP_KEEPIDLE,  &keepidle,  sizeof(keepidle));
setsockopt(sock, IPPROTO_TCP, TCP_KEEPINTVL, &keepintvl, sizeof(keepintvl));
setsockopt(sock, IPPROTO_TCP, TCP_KEEPCNT,   &keepcnt,   sizeof(keepcnt));
```
Total dead-gateway detection time: 5 + 2×3 = 11 seconds (vs 75 seconds default).

After keepalive failure, `send()` returns EPIPE/ECONNRESET → wifi_task closes the socket, waits 2 s, and reconnects. Reconnect loop is bounded to avoid hammering a down gateway.

## Wire format

### epm_header_t (48 bytes, packed, version-stamped)

| Offset | Field | Size | Description |
|---|---|---|---|
| 0 | magic | 4 | EPM_MAGIC = 0x45504D32 ('EPM2') |
| 4 | version | 1 | Protocol version (current: 2) |
| 5 | flags | 1 | Bit 0: encrypted; bit 1: IMU present |
| 6 | overflow_count | 1 | I2S DMA overflow events since last frame (saturates at 255) |
| 7 | _reserved | 1 | Zero; future use |
| 8 | frame_seq | 4 | Monotonically increasing frame counter |
| 12 | timestamp_ms | 4 | esp_timer_get_time() >> 10 (ms) |
| 16 | payload_len | 4 | Total payload bytes following the header |
| 20 | fft_rms | 4 | float: RMS of current frame |
| 24 | fft_kurtosis | 4 | float: kurtosis of current frame |
| 28 | fft_crest | 4 | float: crest factor |
| 32 | spectral_centroid | 4 | float: spectral centroid Hz |
| 36 | imu_rms_accel | 4 | float: IMU RMS acceleration |
| 40 | imu_peak_accel | 4 | float: IMU peak acceleration |
| 44 | dc_offset | 4 | float: DC offset of audio frame |

_Static_assert: `sizeof(epm_header_t) == 48` — verified at compile time.

**overflow_count semantic:** wifi_task tracks the delta since last frame:
```c
static uint32_t s_last_overflow = 0;
uint32_t cur = mic_capture_get_overflow_count();
uint32_t delta = cur - s_last_overflow;
hdr->overflow_count = (uint8_t)(delta > 255u ? 255u : delta);
s_last_overflow = cur;
```
A non-zero overflow_count at the gateway indicates I2S DMA underrun — the frame may have missing audio samples.

## WiFi TX power cap

`esp_wifi_set_max_tx_power(68)` (17 dBm, 68 quarter-dBm units) caps the RF TX power. Default is 20 dBm (80 quarter-dBm). Motivation: at 20 dBm, peak current during a WiFi TX burst can reach ~370 mA on a 3.3 V rail, causing brownout detect trips when the XIAO is running from USB bus power. At 17 dBm, peak current drops to ~280 mA — within the USB 2.0 500 mA budget including all other peripherals.

## Consequences
**Positive:**
- Frame inter-arrival jitter < 10 ms (MSG_MORE eliminates Nagle delay)
- Dead gateway detected in 11 s (vs 75 s default) — watchdog budget respected
- overflow_count gives gateway per-frame DMA health signal without a separate control channel
- TX power cap prevents USB brownout trips

**Negative / trade-offs:**
- MSG_MORE is a Linux/POSIX extension; not available on Windows sockets (gateway must be Linux/macOS or WSL)
- TCP_KEEPIDLE=5 means 5 s of dead time before any probe; during this window, send() blocks if the TCP send buffer fills

**Metrics to watch:**
- Gateway frame sequence gap detection (non-contiguous frame_seq → frames dropped)
- overflow_count per frame (target: 0 in normal operation; > 0 indicates I2S DMA overrun)
- WiFi reconnect count (diagnostics_task: log `s_reconnect_count` every 30 s; target: < 1/hour)

## Validation
`wifi_task.c` — `tcp_send_more()` helper, `TCP_KEEPIDLE/KEEPINTVL/KEEPCNT` setsockopt calls in `tcp_connect()`, `esp_wifi_set_max_tx_power(WIFI_TX_POWER_QTR_DBM)` in `wifi_rf_init()`. `epm_protocol.h` — `_Static_assert(sizeof(epm_header_t) == 48, ...)`.
