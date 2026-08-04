#pragma once

#include <stddef.h>
#include <stdint.h>

/*
 * Accelerometer contract for the 3-axis vibration path (KX134-1211,
 * matches the reference repo's satellite/include/hal/hal_accel.h
 * init/start/read_block/get_sample_rate/stop shape). Any code that needs
 * axis samples includes this header; components/epm_drivers/accel_stub.c
 * is the only implementation until the real SPI driver lands (Phase 9).
 *
 * Deliberately per-axis float, not the reference's interleaved raw
 * int32_t frame: the stub generates a distinct tuned synthetic signal per
 * axis (see accel_stub.c), and src/threads/imu_task.c's FFT pipeline
 * already consumes one axis at a time. The real KX134 driver can still
 * satisfy this contract (its FIFO burst-read naturally de-interleaves
 * into per-axis buffers before handing samples up).
 *
 * Pure C, zero ESP-IDF includes, per docs/MASTER_PLAN.md Part C.1/Part I.
 */

enum hal_accel_axis {
    HAL_ACCEL_AXIS_X = 0,
    HAL_ACCEL_AXIS_Y = 1,
    HAL_ACCEL_AXIS_Z = 2,
};

int hal_accel_init(void);
int hal_accel_start(void);

/* Fills out_samples[0..max_samples) with one axis' normalised-g samples
 * (oldest first). Returns the number of samples written, or a negative
 * errno. */
int hal_accel_read_block(enum hal_accel_axis axis, float *out_samples, size_t max_samples);

uint32_t hal_accel_get_sample_rate(void);
void hal_accel_stop(void);
