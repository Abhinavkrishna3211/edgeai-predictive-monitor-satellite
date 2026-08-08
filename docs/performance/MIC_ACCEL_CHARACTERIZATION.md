# Mic + Accelerometer Sensitivity & Frequency-Range Characterization

Empirical characterization of the satellite's microphone and accelerometer
channels on real hardware (XIAO ESP32-S3 satellite, node `5ab004`). Goal:
establish what the sensors/firmware can actually detect today, as input to a
future tuning pass — this document makes no tuning decisions itself (no
changes to `SPEC_AVG_N`, thresholds, or other defaults in `epm_config.h` /
`platformio.ini`).

Priority throughout: **recall over everything else**. A "ceiling" or "floor"
below is the point where lock reliability degrades, not a hard cutoff — when
in doubt, the data errs toward flagging real signal as detectable rather than
declaring a conservative-but-wrong limit.

## Methodology

- Tool: `tools/bench_signal_gen/capture_and_compare.py`'s `sweep` CLI mode
  (added in Stage 1 of this work, commit `81eff54`).
- Signal source: the laptop's own built-in speaker (mic) / chassis vibration
  from the same speaker with the accelerometer physically coupled to the
  underside of the laptop (accel) — see [ADR notes inline](#stage-3-accelerometer)
  for coupling caveats.
- Lock criterion (`is_locked()` in `capture_and_compare.py:93`): the loudest
  FFT bin must land within ±1 bin-width of the target frequency (for
  frequencies at or above one bin's width) **and** have SNR ≥ 6 dB over a
  noise floor computed from the remaining bins. Below one bin's width (e.g.
  low accel frequencies), the check instead looks for elevated energy in the
  lowest 1-2 bins only (presence, not exact location) since bin resolution
  can't localize a sub-bin-width tone.
- Each sweep point is attempted 3x; "reliable" = 3/3 (or noted otherwise).
- Wire spectrum convention: bin `k` covers `(k*fs/fft_size, (k+1)*fs/fft_size]`,
  center frequency `(k+0.5)*fs/fft_size`. Mic: `fs=16000 Hz`, pooled wire
  `fft_size=256` → 62.5 Hz/bin, true Nyquist 8000 Hz.
- Raw CSVs for every sweep live alongside this doc in
  `docs/performance/mic_accel_characterization/`.

## Stage 2: Microphone

### 2A — Frequency ceiling (100-8000 Hz), stock firmware (`SPEC_AVG_N=4`)

Coarse sweep (`mic_ceiling_coarse.csv`, step 250 Hz, 32 points × 3) and a fine
follow-up (`mic_ceiling_fine.csv`, step 100 Hz through 1900 Hz plus four
points near the true Nyquist edge 7900-8000 Hz) both point to the same
conclusion:

**There is no genuine upper-frequency ceiling within the tested range — the
mic stays reliable all the way to the true Nyquist edge (8000 Hz).**

| Band | Coarse sweep | Fine sweep |
|---|---|---|
| 100-1100 Hz | unreliable: 100 Hz (0/3), 350 Hz (1/3), 850 Hz (1/3) | scattered: 100/200 Hz (0/3), 300-1100 Hz mostly 1-2/3 |
| 1200-1900 Hz | not sampled at this resolution | solid 3/3 from 1200 Hz on |
| 1850-7600 Hz | reliable 3/3 throughout (SNR commonly 15-55 dB) | not sampled (coarse-only band) |
| 7850-8000 Hz | borderline: 7850 Hz (2/3) | solid: 7900-8000 Hz all 3/3, SNR 10.6-43.4 dB |
| 1600 Hz (coarse) | 1/3 — an outlier inside an otherwise-reliable band | — |

The low-band unreliability (100-1100 Hz, worst at exactly 100/200/1600 Hz) is
attributed to the **laptop speaker's own bass roll-off and room acoustics**,
not a satellite mic-side limitation — see the `SPEC_AVG_N=8` comparison below,
which resolves nearly all of this band without any change to the mic
hardware or sampling rate.

### 2B — Amplitude/sensitivity floor, stock firmware

Fixed-frequency amplitude sweep at the spec-mandated 1000 Hz
(`mic_floor.csv`, 11 amplitude steps 1.0 → 0.0316, i.e. down to ~-30 dB
relative to max, × 3 each):

**No clean monotonic floor found — because 1000 Hz itself is an
anomalously poor frequency in this room/speaker setup.** Even amp=1.0 (full
volume) only locked 0/3; reliability was scattered non-monotonically across
the whole amplitude range (best case 2/3 at amp=0.3547 and amp=0.0891,
otherwise 0-1/3). This matches the 1000 Hz weak point already visible in the
2A fine ceiling sweep (2/3 at amp=1.0 there too).

To separate "mic sensitivity floor" from "1000 Hz is a bad test frequency,"
a supplementary sweep was run at 1300 Hz — a frequency that was rock-solid
3/3 everywhere in 2A (`mic_floor_1300.csv`, same 11 amplitude steps):

**No amplitude floor found down to the bottom of the tested range
(amp=0.0316, ~-30 dB relative to max)** — locks stayed reliable (2-3/3,
SNR 3.8-36.1 dB) at every step. The true amplitude sensitivity floor of the
mic is much better than the 1000 Hz data alone would suggest; 1000 Hz should
not be used as a representative test frequency for this hardware/room combo.

### 2C — `SPEC_AVG_N` averaging comparison (temporary override, reverted)

`SPEC_AVG_N` (spectral block-averaging count, default 4, `epm_config.h`)
was temporarily overridden to 8 by directly editing `platformio.ini`'s
`-DSPEC_AVG_N=4` → `-DSPEC_AVG_N=8` build flag (env-var-based overrides were
tried first and abandoned — PlatformIO/SCons groups all `-D` flags together
regardless of source order, and ESP-IDF framework components build with
`-Werror=all`, so a duplicate/conflicting `-DSPEC_AVG_N` anywhere in the
flag set fails the whole build with a macro-redefinition error). The change
was reverted immediately after data collection (`git diff platformio.ini`
confirmed clean; stock firmware rebuilt, reflashed, and MQTT connectivity
reconfirmed).

**Fine ceiling sweep, avg=8 vs avg=4** (`mic_ceiling_fine_avg8.csv` vs
`mic_ceiling_fine.csv`, same 23 frequency points):

| Frequency | avg=4 | avg=8 |
|---|---|---|
| 100 Hz | 0/3, SNR 2.6-28.3 dB | 0/3, SNR 19.4-22.1 dB (peak still lands on bin 0, not the target bin — see below) |
| 200 Hz | 0/3, SNR 2.9-7.6 dB | **3/3, SNR 22.0-23.4 dB** |
| 300-1100 Hz | mostly 1-2/3, SNR as low as 2.4 dB | **3/3 at every point**, SNR 24.3-55.6 dB |
| 1200-1900 Hz | mostly 3/3 already, SNR 4.8-44.4 dB | **3/3 at every point**, SNR 30.2-53.2 dB (roughly doubled) |
| 7900-8000 Hz | 3/3, SNR 10.6-43.4 dB | 3/3, SNR 37.2-46.0 dB |

`SPEC_AVG_N=8` essentially **eliminates the intermittent low/mid-band lock
failures** seen at `SPEC_AVG_N=4` (200-1900 Hz goes from scattered 0-2/3 to
uniform 3/3) and roughly doubles SNR margins across the board. The one
holdout is 100 Hz, which fails 0/3 at both avg=4 and avg=8 — inspecting the
raw peak location shows the dominant energy consistently sits at bin 0
(≈31.25 Hz, near-DC) rather than the 100 Hz target bin, at both averaging
settings. This is a **physical limitation of the test signal** (speaker bass
rolloff / room rumble dominating over the true 100 Hz tone), not something
more averaging can fix, since averaging reduces noise variance but can't
move where the actual radiated energy peaks.

**Floor sweep, avg=8** (`mic_floor_avg8.csv` at 1000 Hz,
`mic_floor_1300_avg8.csv` at 1300 Hz):

- 1000 Hz: still 0/3 at every amplitude step, confirming this is a
  frequency-specific artifact (peak locked to bin 0, not amplitude/SNR
  limited) that averaging does not fix.
- 1300 Hz: **3/3 at every amplitude step down to amp=0.0316**, with SNR now
  pinned in a tight, stable 37.0-42.0 dB band — noticeably more consistent
  than the avg=4 result (2-3/3, SNR 3.8-36.1 dB scattered). Still no floor
  found within the tested range.

**`SPEC_AVG_N=16` was not tested.** Given avg=8 already drives the low/mid
band to near-universal 3/3 locks and the sole remaining failure (100 Hz) is
a peak-location/physical issue rather than an SNR shortfall, a further
averaging increase was judged unlikely to change the qualitative picture —
and would cost another full rebuild/flash/sweep/revert cycle. This is a
time-boxing call, not a finding; a future pass could still confirm it.

**Cost of `SPEC_AVG_N=8`**: frames are emitted every `SPEC_AVG_N` spectral
blocks, so doubling it from 4 to 8 doubles end-to-end detection/reporting
latency for the mic channel. Not evaluated here (out of scope per Stage 4
exclusion) — noted for the future tuning discussion.

## Stage 3: Accelerometer

*Pending — frequency sweep (20-150 Hz), amplitude floor sweep (≥100 Hz), and
chassis-coupling sanity check to be added in a follow-up commit.*

## What this means for future tuning (open questions, no decisions made here)

- The mic's practical ceiling in this test setup was **speaker/room-limited,
  not sensor-limited** — true hardware Nyquist (8000 Hz at `MIC_FS_HZ=16000`)
  was reliably reached. Any real-world ceiling should be re-validated with a
  proper reference signal source (not a laptop speaker) before drawing
  conclusions about the physical sensor's range.
- 1000 Hz is a poor representative test frequency for amplitude-floor work
  on this speaker/room combination; future floor testing should pick a
  frequency confirmed clean in a ceiling sweep first.
- `SPEC_AVG_N=8` shows a large, consistent reliability and SNR improvement
  over the current default of 4, at the cost of 2x reporting latency for the
  mic channel. Whether that trade is worth making — and whether 16 is worth
  testing — is a Stage 4 decision, not made here.
- Per-node detection ceilings can legitimately differ across the fleet's
  heterogeneous hardware (e.g. a base station on different MCU/ADC hardware
  sampling the same physical mic/accel parts at a different rate will have a
  different Nyquist ceiling than this XIAO ESP32-S3 satellite's
  `MIC_FS_HZ=16000` / `IMU_FS_HZ=25600` configuration) — this is a firmware
  sampling-rate choice per node type, not evidence of inferior sensor
  hardware on either side.
