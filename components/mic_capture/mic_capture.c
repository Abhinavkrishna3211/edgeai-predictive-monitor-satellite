/*
 * mic_capture.c — Stage 1: raw I2S DMA capture + signal verification.
 *
 * No FFT, no esp-dsp, no BLE here on purpose. The goal of this stage is
 * just to prove the mic is wired correctly and the I2S/DMA path is
 * gap-free, before any compute or radio work is layered on top.
 *
 * Uses the new ESP-IDF v5.x I2S driver (driver/i2s_std.h), which is what
 * PlatformIO's `framework = espidf` builds against for IDF 5.x. If your
 * platformio.ini pins an IDF 4.x release this header won't exist — check
 * `platform_packages`/`framework` versions if the build complains about
 * missing i2s_std.h.
 *
 * HARDWARE NOTE: This driver targets EXTERNAL I2S microphones (INMP441,
 * ICS-43434).  The built-in PDM microphone on the XIAO ESP32-S3 Sense
 * board requires i2s_pdm_rx_config_t instead of i2s_std_config_t.
 * If using the onboard PDM mic, replace i2s_channel_init_std_mode with
 * i2s_channel_init_pdm_rx_mode and set the appropriate PDM GPIO pins.
 */

#include "mic_capture.h"
#include "freertos/FreeRTOS.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include <math.h>
#include <string.h>

static const char *TAG = "mic_capture";

static i2s_chan_handle_t s_rx_chan = NULL;

esp_err_t mic_capture_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(MIC_I2S_PORT, I2S_ROLE_MASTER);

    // ESP32-S3 DMA descriptor max is 4092 bytes = 1023 samples at 32-bit.
    // MIC_RAW_BLOCK_SAMPLES=1024 would hit that limit and trigger a driver
    // warning. Using half the block size (512) keeps each descriptor within
    // the hardware limit; i2s_channel_read() spans multiple descriptors
    // transparently. Total ring = dma_desc_num × 512 = 2048 samples = 2 blocks.
    chan_cfg.dma_desc_num  = MIC_DMA_DESC_NUM;
    chan_cfg.dma_frame_num = MIC_RAW_BLOCK_SAMPLES / 2;
    chan_cfg.auto_clear    = true;

    esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &s_rx_chan);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_new_channel failed: %s", esp_err_to_name(err));
        return err;
    }

    i2s_std_config_t std_cfg = {
        .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(MIC_SAMPLE_RATE_HZ),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                         I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = MIC_I2S_BCLK_PIN,
            .ws   = MIC_I2S_WS_PIN,
            .dout = I2S_GPIO_UNUSED,
            .din  = MIC_I2S_DATA_IN_PIN,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv   = false,
            },
        },
    };

    // INMP441/ICS-43434 output on the LEFT slot when L/R is tied to
    // GND. If you've wired SEL to VDD instead, flip this to SLOT_RIGHT —
    // otherwise you'll capture silence (the unselected channel).
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

    err = i2s_channel_init_std_mode(s_rx_chan, &std_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_init_std_mode failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "mic_capture init: %d Hz, block=%d samples, dma_desc=%d",
              MIC_SAMPLE_RATE_HZ, MIC_RAW_BLOCK_SAMPLES, MIC_DMA_DESC_NUM);
    return ESP_OK;
}

/* Call from mic_task_fn() running on CPU1 so the I2S DMA interrupt is
 * allocated to CPU1, away from the WiFi driver task on CPU0. */
esp_err_t mic_capture_enable(void)
{
    esp_err_t err = i2s_channel_enable(s_rx_chan);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2s_channel_enable failed: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t mic_capture_read_block(int32_t *out_raw, float *out_normalized,
                                  size_t block_len)
{
    if (s_rx_chan == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    static int32_t raw_buf[MIC_RAW_BLOCK_SAMPLES];
    size_t want_bytes = block_len * sizeof(int32_t);
    size_t got_bytes  = 0;

    // Blocking read — fine for Stage 1. portMAX_DELAY is safe here since
    // the DMA ring keeps filling in the background; we're not at risk of
    // deadlocking the I2S peripheral itself, only of falling behind it
    // (which is exactly what we're instrumenting via mic_capture_compute_stats).
    esp_err_t err = i2s_channel_read(s_rx_chan, raw_buf, want_bytes, &got_bytes, portMAX_DELAY);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "i2s_channel_read failed: %s", esp_err_to_name(err));
        return err;
    }
    size_t n = got_bytes / sizeof(int32_t);

    if (got_bytes != want_bytes) {
        ESP_LOGW(TAG, "short read: got %u of %u bytes — zero-padding remainder",
                 (unsigned)got_bytes, (unsigned)want_bytes);
        /* Zero-pad so the FFT never sees stale samples from the previous call. */
        if (out_normalized && n < block_len) {
            memset(out_normalized + n, 0, (block_len - n) * sizeof(float));
        }
        if (out_raw && n < block_len) {
            memset(out_raw + n, 0, (block_len - n) * sizeof(int32_t));
        }
    }

    for (size_t i = 0; i < n; i++) {
        // INMP441/ICS-43434 send 24-bit samples left-justified in the
        // 32-bit slot. Shift right by 8 to land the 24-bit value in the
        // low bits, sign-extending correctly since this is a signed
        // right shift on a signed type.
        int32_t sample24 = raw_buf[i] >> 8;

        if (out_raw) {
            out_raw[i] = sample24;
        }
        if (out_normalized) {
            out_normalized[i] = (float)sample24 / 8388608.0f; // 2^23
        }
    }

    return ESP_OK;
}

void mic_capture_compute_stats(const float *normalized_block, size_t len,
                                mic_block_stats_t *out_stats)
{
    if (len == 0 || normalized_block == NULL || out_stats == NULL) {
        return;
    }

    float sum = 0.0f;
    float sum_sq = 0.0f;
    float minv = normalized_block[0];
    float maxv = normalized_block[0];
    uint32_t clipped = 0;

    // Treat anything within ~1 LSB of full scale as clipped — at 24-bit
    // depth that's an extremely tight margin, so this only fires on
    // genuine clipping (gain too high, or mic overloaded by a loud
    // transient), not on normal signal swings.
    const float clip_thresh = 1.0f - (1.0f / 8388608.0f) * 2.0f;

    for (size_t i = 0; i < len; i++) {
        float v = normalized_block[i];
        sum    += v;
        sum_sq += v * v;
        if (v < minv) minv = v;
        if (v > maxv) maxv = v;
        if (fabsf(v) >= clip_thresh) clipped++;
    }

    out_stats->dc_offset      = sum / (float)len;
    out_stats->rms            = sqrtf(sum_sq / (float)len);
    out_stats->min_sample      = (int32_t)(minv * 8388608.0f);
    out_stats->max_sample      = (int32_t)(maxv * 8388608.0f);
    out_stats->clipped_count   = clipped;
}

void mic_capture_deinit(void)
{
    if (s_rx_chan) {
        i2s_channel_disable(s_rx_chan);
        i2s_del_channel(s_rx_chan);
        s_rx_chan = NULL;
    }
}
