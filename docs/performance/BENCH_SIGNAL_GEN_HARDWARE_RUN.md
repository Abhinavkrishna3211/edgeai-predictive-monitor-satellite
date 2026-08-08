# Bench Signal-Gen — Real Hardware Run

**Date:** 2026-08-08
**Branch:** feat/base-station-interop
**Tool:** `tools/bench_signal_gen/` (ADR-035)
**Hardware:** real Seeed XIAO satellite (node_id `5ab004`), Lenovo Legion 5 Pro
built-in speaker and phone (browser tone generator) as excitation sources.

## Summary

Neither channel achieved a confident tone-frequency detection this session:

- **Mic:** no detection at 200 Hz or 1 kHz across 3 physical placements, 2
  excitation sources, and system volume from 80-100%. 4 kHz was found to be
  architecturally unobservable on this firmware's wire spectrum (0-1992 Hz
  ceiling), a genuine tool/protocol-scoping gap discovered this session, not
  a test failure at that frequency. A reproducible 257.8 Hz artifact of
  unconfirmed origin (likely chassis/fan vibration or phone haptics) appeared
  repeatedly but never matched a played tone frequency.
- **Accelerometer:** no confident 60 Hz lock on any axis (contact coupling
  against a resting, unclamped chassis) — peak bin wandered at noise-floor
  levels across all three axes; formal `capture_and_compare.py` deltas are
  large in both directions, most extremely `accel_z`'s kurtosis_excess
  (+29.5 actual vs. -1.5 expected).

Both results are most consistent with equipment/coupling limitations named
in ADR-035 (unclamped resting contact for accel; unknown/unverified mic
gain-staging and unlocated speaker grille for the laptop-speaker mic
attempts) rather than a confirmed DSP-pipeline defect — the DSP/FFT pipeline
itself is demonstrably functional, since it cleanly resolved the real
257.8 Hz artifact to a single dominant bin when one was physically present.
No pass/fail tolerance band can be calibrated from this run's numbers (per
ADR-035's stated plan) since no channel produced a clean positive detection
to calibrate against.

---

## Mic channel — tone sweep

### Setup

- Laptop speaker path: system volume 80% initially, later raised to 100%;
  ambient room noise checked before testing (fan noise only, no other
  sources) via `ambient_check.py`.
- Satellite tried at three physical placements relative to the laptop: 15 cm
  from a side USB port (open air), direct contact at the keyboard deck, and
  resting on the underside/bottom-right of the chassis — the laptop's actual
  speaker grille location was never conclusively identified.
- Phone path (after laptop attempts were exhausted): browser-based tone
  generator (szynalski.com/tone-generator, no app install), held near and
  then flush against the satellite's mic opening.
- All captures used a full-timeline scan (every frame's peak-bin frequency/
  dBFS/RMS across the entire capture window, not just the last frame) to
  rule out capture-timing artifacts — see methodology note below.

### Methodology note: capture timing hazard

`capture_and_compare.py`'s `select_frame()` deliberately picks the temporally
**last** matching frame in a capture window (ADR-035: not averaging away
frame-to-frame variance). This creates a hazard for a bench operator
following the literal README procedure ("play tone and let it finish, then
capture") — if the capture window's tail extends past when playback actually
stopped, the "last frame" reflects silence, not the tone, giving a false
negative. Worked around this session by playing tones in the background
(longer duration) and running a short foreground capture window that closes
well before playback ends, cross-checked with a full-timeline scan
(`idx / peak_hz / peak_dbfs / rms` for every frame) rather than trusting a
single frame. This ruled out timing as the explanation for the results
below.

### Results

| Attempt | Source | Freq (Hz) | Placement | Result |
|---|---|---|---|---|
| 1 | Laptop speaker | 200 | 15 cm, open air | No detection — peak stayed at ambient (7.8 Hz) throughout |
| 2 | Laptop speaker | 1000 | Closer/keyboard deck | No detection — brief 257.8 Hz transient at capture start, then ambient baseline for rest of window |
| 3 | Laptop speaker | 1000 | Bottom-right chassis, direct contact | Same as #2 |
| 4 | Laptop speaker | 1000 | Same, at 100% system volume + max synth amplitude | Same as #2 |
| 5 | Phone (browser tone gen) | 1000 | Near mic | No detection at the 1000 Hz bin (-104 dBFS, flat noise floor); a real, sustained, growing signal appeared at 257.8 Hz instead |
| 6 | Phone (browser tone gen) | 1000 | Flush against mic | Same as #5 — 1000 Hz bin still -103 dBFS noise floor |

**No attempt at 200 Hz or 1000 Hz produced a detectable peak at the played
frequency**, across 3 physical placements, 2 excitation sources (laptop
speaker and phone), and system volume from 80% to 100%.

### The 257.8 Hz artifact

A recurring, reproducible signal at exactly 257.8 Hz (FFT bin 16, this
device's 15.625 Hz/bin resolution) appeared in attempts 2-6, but its
behavior is inconsistent with being the played tone:

- It appeared **only** once the satellite was moved into contact with the
  laptop chassis (absent in attempt 1's open-air 15 cm placement) — points
  to chassis/fan vibration, not acoustic content.
- In laptop-speaker attempts (2-4) it was a **transient**: elevated for the
  first several frames of a capture window, then decayed to ambient baseline
  for the remainder of the window despite the tone still playing.
- In the phone attempts (5-6) it was instead **sustained and growing**
  (-64.7 -> -55.3 dBFS over the capture window) — a different character,
  plausibly the phone's own haptic/vibration motor or speaker-housing
  resonance at contact, not confirmed.

Root cause is **not confirmed**. Flagged here as an open item, not asserted
as diagnosed.

### 4 kHz — architecturally out of range, not tested

The wire spectrum for the mic channel covers only **0-1992 Hz** (128 bins x
15.625 Hz/bin), despite the reported 16 kHz sample rate (which would allow
FFT bins up to 8 kHz Nyquist). **4 kHz cannot appear in this telemetry
channel's spectrum at all** — this is a firmware/wire-protocol bandwidth
limit discovered during this run, not a result of any test. The original
sweep plan (200 Hz / 1 kHz / 4 kHz, per `tools/bench_signal_gen/README.md`)
included a target this tool's current wire format cannot observe; the
README's sweep guidance should be corrected in a follow-up (see "Follow-up"
below).

While probing this with the phone at a confirmed 4000 Hz setting, a sharp,
narrow (non-broadband) peak appeared at ~1000-1008 Hz (bins 63-64, -51 to
-44 dBFS, all neighboring bins at noise floor) — qualitatively different
from the broad 257.8 Hz artifact above, and structurally consistent with a
real tone-like signal. This is suspicious rather than confirmatory: the same
bin showed pure noise floor (-103 dBFS) when the phone was actually set to
1000 Hz (attempts 5-6). A signal appearing near 1000 Hz specifically when
4000 Hz is played, but not when 1000 Hz is played, suggests aliasing or
harmonic/subharmonic distortion somewhere in the chain (phone speaker
nonlinearity at 4 kHz, or anti-aliasing filter leakage ahead of the mic's
decimation stage) rather than genuine 4 kHz detection. **Not confirmed as
either a real detection or a specific mechanism** — noted as an anomaly
worth follow-up, not resolved here.

### Conclusion — mic channel

No successful validation of mic-channel tone detection was achieved this
session at any tested frequency, source, placement, or volume. This is a
genuine negative result, not a tooling bug: the capture-timing methodology
was independently validated (full-timeline scans covering entire playback
windows), MQTT client-ID reuse was ruled out as a cause, and the DSP/FFT
pipeline is demonstrably functional (it clearly resolves a real signal at
257.8 Hz to a sharp single bin when one is physically present). The most
likely explanation is a real-world SPL/mic-sensitivity mismatch — the tested
excitation sources' acoustic output at the tested distances did not exceed
whatever threshold this satellite's mic gain staging needs — but this has
not been isolated from other candidates (mic gain/AGC configuration, an
unexpectedly aggressive DSP noise gate, or a hardware fault) within this
session.

### Follow-up (not done this session)

- Correct `tools/bench_signal_gen/README.md`'s sweep guidance: either drop
  4 kHz from the recommended sweep points or note the mic wire spectrum's
  0-1992 Hz ceiling explicitly.
- Investigate mic gain/AGC and DSP noise-gate configuration in
  `mic_task.c`/`dsp_task.c` as a candidate explanation for the total lack of
  tone detection, independent of further bench attempts.
- A calibrated reference speaker or an SPL meter would remove the "was it
  loud enough" ambiguity entirely — not available this session.

---

## Accelerometer channel — contact coupling test

### Setup

Satellite board rested in direct contact with the underside of the Legion 5
Pro chassis (bottom-right), the same placement used for the final mic
attempts. Tone: 60 Hz, amplitude 1.0, per `tools/bench_signal_gen/README.md`'s
recommended ~20-150 Hz contact-coupling range. Wire spectrum resolution for
accel is much coarser than mic: 12.5 Hz/bin (vs. mic's 15.625 Hz/bin), so the
60 Hz target's nearest bin center is 56.2 Hz.

An initial full-timeline capture (43 frames, all three axes) caught a large,
simultaneous multi-axis spike at 6.2 Hz around frames 26-30 (`accel_x` RMS
1.02g -> 1.17g, `accel_y` 0.10g -> 0.38g, `accel_z` 0.07g -> 0.52g) — traced
to the satellite being physically repositioned mid-capture (confirmed with
the operator), not a signal or hardware artifact. Discarded; a second,
undisturbed full-timeline capture was taken after repositioning settled.

### Full-timeline scan result (undisturbed capture)

Across all three axes, 43 frames, the peak bin **wanders** between adjacent
bins (18.8 / 31.2 / 43.8 / 56.2 / 68.8 / 81.2 / 106.2 / 118.8 / 181.2 Hz
variously) at essentially flat dBFS levels (-49 to -54 dBFS throughout, all
axes) — no bin holds a consistent, dominant lock the way a real driven
sinusoid would produce (contrast with the mic channel's genuine 257.8 Hz
artifact earlier, which locked a single bin ~50 dB above its neighbors for
dozens of consecutive frames). `accel_y` does read the correct 56.2 Hz target
bin in 2 of 8 blocks, but at -51.6 dBFS it is not distinguishable from the
surrounding noise-floor wander. This reads as noise-floor-level content, not
a resolved 60 Hz response, on all three axes.

`accel_x`'s baseline RMS sits at ~1.0-1.02g throughout — this is gravity (X
is the board's vertical axis in this resting orientation), a large DC
component that may be masking a smaller genuine AC response if the FFT path
doesn't remove DC the way `mic_task.c` does for the mic channel (per
`docs/performance/HARDWARE_AUDIT_RESULTS.md` Phase 4) — not confirmed, flagged
as a candidate explanation only.

### Formal actual-vs-expected (`capture_and_compare.py`, manifest
`tone_60hz_20260808_185329.json`, amplitude 1.0)

**accel_x** (2.5s window, 13 frames):

| metric | expected | actual | % delta |
|---|---|---|---|
| peak_bin_freq_hz | 60.000 | 43.750 | -27.083% |
| peak_bin_db | -- | -53.233 | -- |
| rms | 0.707107 | 1.014928 | +43.532% |
| crest_factor | 1.414214 | 1.263848 | -10.632% |
| kurtosis_excess | -1.500000 | -1.995463 | +33.031% |
| skewness | 0.000000 | -0.562604 | (abs delta -0.562604) |

**accel_y** (2.5s window, 13 frames):

| metric | expected | actual | % delta |
|---|---|---|---|
| peak_bin_freq_hz | 60.000 | 18.750 | -68.750% |
| peak_bin_db | -- | -50.718 | -- |
| rms | 0.707107 | 0.096664 | -86.330% |
| crest_factor | 1.414214 | 4.525992 | +220.036% |
| kurtosis_excess | -1.500000 | -0.697105 | -53.526% |
| skewness | 0.000000 | -1.616033 | (abs delta -1.616033) |

**accel_z** (3.0s window, 16 frames):

| metric | expected | actual | % delta |
|---|---|---|---|
| peak_bin_freq_hz | 60.000 | 31.250 | -47.917% |
| peak_bin_db | -- | -53.288 | -- |
| rms | 0.707107 | 0.039929 | -94.353% |
| crest_factor | 1.414214 | 11.763922 | +731.835% |
| kurtosis_excess | -1.500000 | 29.495106 | -2066.340% |
| skewness | 0.000000 | 1.751229 | (abs delta +1.751229) |

**Not averaged and not cherry-picked** — `accel_z`'s kurtosis_excess
(+29.5 actual vs. -1.5 expected, a -2066% delta) is a genuine outlier from
this run's raw numbers, reported as observed. A value that extreme on a
supposedly-sinusoidal signal is itself informative: it indicates the
captured `accel_z` waveform is dominated by sparse, high-amplitude
transients (consistent with intermittent mechanical contact/rattle from a
board merely resting against a chassis, rather than a clean sinusoidal
vibration coupling), not a modeling error in `ideal_sine_stats()`.

### Conclusion — accelerometer channel

No axis showed a confident, locked detection of the 60 Hz tone. All three
peak-bin frequencies missed the 60 Hz target substantially (43.75 / 18.75 /
31.25 Hz actual vs. 60 Hz expected), and scalar deltas are large and
inconsistent in direction across axes (RMS over-reads on `accel_x`,
under-reads by >85% on `accel_y`/`accel_z`; crest_factor and kurtosis swing
wildly, especially `accel_z`). This is consistent with ADR-035's acknowledged
limitation: a bare laptop chassis under a resting (not clamped/bolted) board
is a poor, uncontrolled contact-vibration coupling path — cone excursion at
60 Hz through an unclamped resting contact is not guaranteed to transmit a
clean sinusoid, unlike the mic channel's air-coupling which at least
delivers an unambiguous (if apparently too-quiet) acoustic waveform. Root
cause is most likely coupling quality (equipment limitation, matching
ADR-035's stated trade-off), not the DSP pipeline itself — but this was not
isolated from a DSP/gain-path explanation within this session, same caveat
as the mic-channel conclusion above.

### Follow-up (not done this session)

- A cheap vibration motor or bass-shaker transducer, bolted/clamped directly
  to the enclosure (not resting), would remove the coupling-quality ambiguity
  entirely — already named in ADR-035 as the preferred future upgrade, not
  available this session.
- Investigate whether accel FFT removes DC before transform (candidate
  explanation for `accel_x`'s gravity-dominated spectrum swamping any real
  AC content) — code-level check, not a bench-test action.

---

## Addendum (2026-08-08): re-test after the wire fft_size fix

`docs/decisions/ADR-020-bin-count-downsampled-not-buffer-enlarged.md`'s
same-day addendum documents a wire-protocol bug found via this document's
"4 kHz — architecturally out of range" section above: `net_task.c` reported
the *native* fft_size (1024 mic / 2048 accel) instead of the pooled,
effective fft_size, so any consumer computing bin width via `fs / fft_size`
— including this tool's `capture_and_compare.py` — recovered the wrong bin
width: 15.625 Hz/bin instead of the true 62.5 Hz/bin for mic, 12.5 Hz/bin
instead of the true 100 Hz/bin for accel. Every search window in the
results above was scaled wrong by exactly that factor. This addendum
re-runs the same tone sweep against the fixed firmware to see how much of
the original negative results that explains. **Not assumed either
way going in** — re-tested and reported below.

### Pre-test blocker: stale MQTT broker host in NVS

Before any tone could be captured, the satellite failed to connect to its
MQTT broker (`select() timeout` against `10.42.0.1`) — a value seeded into
NVS from an earlier hotspot-based session and never updated for this
session's `MUTHIYATTIRI 2.4GHz` / `192.168.1.5` (Mosquitto on the laptop)
setup. The *compiled* default (`EPM_MQTT_BROKER_HOST` in `link_mqtt.c` /
`wifi_task.c`) is also still `10.42.0.1`, so a bare NVS erase alone would
not have fixed it — confirmed by reading the source before touching
hardware. Worked around by a full `pio run -t erase` (the device's
`node_id` is derived from chip MAC, not NVS-stored, so identity survived)
and reflash with `EPM_MQTT_BROKER_HOST=192.168.1.5` passed via
`PLATFORMIO_BUILD_FLAGS` for this session only — not committed to
`platformio.ini`. A gitignored `.env.local`-style override for the broker
host, matching `tools/devrig/.env.local`'s existing pattern for the
reference-repo URL, is a planned follow-up so a bench-network change
doesn't require a source edit and reflash every time.

### Verification the fix landed on the wire

Decoded a live frame directly, before running any tone test:

| channel | fs (Hz) | fft_size | bin_count | bin width |
|---|---|---|---|---|
| mic | 16000 | 256 | 128 | 62.5 Hz |
| accel_x / accel_y / accel_z | 25600 | 256 | 128 | 100.0 Hz |
| accel_x/y/z_envelope | 3200 | 256 | 128 | 12.5 Hz (unchanged — never pooled) |

Matches the fix's target values exactly.

### Mic channel — re-test

Setup differed from the original run in one respect: the tone was played
continuously by the operator (phone tone generator held near the mic,
same placement family as the original run's phone attempts) rather than
a timed one-shot, so `generate_and_play.py tone --no-play` was used to
produce the ground-truth manifest only, without double-sourcing the tone
through the tool's own laptop-speaker playback.

**200 Hz:**

| metric | expected | actual | % delta |
|---|---|---|---|
| peak_bin_freq_hz | 200.000 | 218.750 | +9.375% |
| peak_bin_db | -- | -69.662 | -- |
| rms | 0.565685 | 0.000414 | -99.927% |
| crest_factor | 1.414214 | 2.239069 | +58.326% |
| kurtosis_excess | -1.500000 | -0.886397 | -40.907% |
| skewness | 0.000000 | -0.049843 | (abs delta -0.049843) |

**1000 Hz:**

| metric | expected | actual | % delta |
|---|---|---|---|
| peak_bin_freq_hz | 1000.000 | 1031.250 | +3.125% |
| peak_bin_db | -- | -57.549 | -- |
| rms | 0.565685 | 0.001700 | -99.700% |
| crest_factor | 1.414214 | 1.816666 | +28.458% |
| kurtosis_excess | -1.500000 | -1.125915 | -24.939% |
| skewness | 0.000000 | -0.010187 | (abs delta -0.010187) |

Both `peak_bin_freq_hz` values land in the bin that structurally contains
the played frequency — 200 Hz falls in bin 3's (187.5, 250] Hz range
(reported as its 218.75 Hz center); 1000 Hz sits on the bin 15/16 boundary,
effectively a single-bin match. Both are clean, confident detections,
against a genuinely quiet signal (-70 to -58 dBFS) that the old,
wrongly-scaled search window would have had to find by accident. This is a
reversal from the original run's 0/6 detections at these same two
frequencies. `rms`/`crest_factor`/`kurtosis_excess` deltas remain large —
expected, since `ideal_sine_stats()`'s closed-form manifold models a clean
sine at the played amplitude, not a real captured signal at unmeasured
distance/volume through a real mic's noise floor; only `peak_bin_freq_hz`
is a frequency-domain claim this fix bears on.

**Conclusion: the wire fft_size mislabeling, not equipment/coupling, was
the dominant cause of the original mic-channel non-detections.**
`capture_and_compare.py` was reading `fs`/`fft_size` straight off the wire
the whole time (`gateway/common/telemetry_frame.py` already decoded them
correctly, per its own pooled-aware convention) — it was the *satellite*
misreporting `fft_size`, so every comparison in the original run was
checking the wrong bin's frequency label against the right raw data. The
original run's "equipment/coupling limitation" conclusion for the mic
channel is superseded by this finding. Original numbers above are left
unedited, per this doc's own convention.

### Accelerometer channel — re-test

Setup changed from the original run in a way this addendum does **not**
control for: rather than resting the board loosely against the underside
of the laptop chassis, the laptop was placed directly on top of the
satellite board this session — a firmer but uncontrolled contact path,
different from the original run's placement. This is a confound flagged
explicitly, not folded silently into the conclusion below.

60 Hz tone, amplitude 1.0:

**accel_x:**

| metric | expected | actual | % delta |
|---|---|---|---|
| peak_bin_freq_hz | 60.000 | 450.000 | +650.000% |
| peak_bin_db | -- | -52.214 | -- |
| rms | 0.707107 | 1.015044 | +43.549% |
| crest_factor | 1.414214 | 1.285832 | -9.078% |
| kurtosis_excess | -1.500000 | -1.994434 | +32.962% |
| skewness | 0.000000 | -3.749820 | (abs delta -3.749820) |

**accel_y:**

| metric | expected | actual | % delta |
|---|---|---|---|
| peak_bin_freq_hz | 60.000 | 50.000 | -16.667% |
| peak_bin_db | -- | -52.638 | -- |
| rms | 0.707107 | 0.097083 | -86.270% |
| crest_factor | 1.414214 | 5.912220 | +318.057% |
| kurtosis_excess | -1.500000 | 0.305227 | -120.348% |
| skewness | 0.000000 | -0.437151 | (abs delta -0.437151) |

**accel_z:**

| metric | expected | actual | % delta |
|---|---|---|---|
| peak_bin_freq_hz | 60.000 | 250.000 | +316.667% |
| peak_bin_db | -- | -51.391 | -- |
| rms | 0.707107 | 0.044455 | -93.713% |
| crest_factor | 1.414214 | 8.935349 | +531.825% |
| kurtosis_excess | -1.500000 | 21.882227 | -1558.815% |
| skewness | 0.000000 | -0.007263 | (abs delta -0.007263) |

`accel_y` now lands in the *structurally correct* bin: at 100 Hz/bin
resolution, 60 Hz falls inside bin 0's (0, 100] Hz range, reported as that
bin's 50 Hz center — a real, if coarse, detection. This matches the
original run's full-timeline scan finding that `accel_y` touched the
correct bin in 2 of 8 blocks even under the old mislabeling; here it is
reproduced in a formal single-frame capture. `accel_x` and `accel_z` still
miss. `accel_x` keeps the same gravity-DC-dominated baseline
(rms ~1.0-1.02g both sessions) already flagged as a candidate explanation
in the original run's conclusion, unrelated to this fix. `accel_z`'s miss
(250 Hz instead of 60 Hz) is new and not explained by the wire bug alone —
plausibly broadband mechanical noise from a full laptop (fan, chassis
resonance) now resting directly on the board, a difference introduced by
this session's firmer, uncontrolled coupling method, not isolated further
here.

**Conclusion: partial support for the same finding as the mic channel, but
weaker and confounded.** One axis (`accel_y`) flipped from
wandering-noise-floor to a genuine correct-bin detection under the fix;
`accel_x` and `accel_z` still show no lock, for reasons plausibly unrelated
to the wire bug (gravity DC on X; likely new broadband noise on Z from
this session's different, firmer coupling). Because the physical coupling
method changed between the original run and this one, this result cannot
cleanly separate "the wire fix helped" from "the new coupling method is
different." A follow-up holding the *exact* original resting-contact
placement constant, changing only the firmware, would isolate the two.

### Updated overall conclusion

The wire fft_size bug explains the *entirety* of the original mic-channel
non-detections, and at least *part* of the original accelerometer
non-detections (`accel_y`). The original run's "equipment/coupling
limitation" explanation, while still plausible for `accel_x`/`accel_z`'s
remaining misses, was not the right explanation for the mic channel or for
`accel_y` — those were the wire protocol reporting bin frequencies scaled
wrong by a factor the DSP pipeline itself never got wrong.

### Follow-up (not done this session)

- Parameterize `EPM_MQTT_BROKER_HOST` via a gitignored `.env.local`-style
  override (matching `tools/devrig/.env.local`'s pattern), so a bench
  network change doesn't require a source edit and reflash.
- Re-test the accelerometer channel holding the *exact* original
  resting-against-chassis placement constant, to separate the wire fix's
  effect from this session's laptop-on-board coupling change.
- Investigate `accel_z`'s new 250 Hz miss under the firmer coupling method
  used this session (candidate: broadband fan/chassis noise from direct
  laptop contact) — not investigated further here.
- Try acoustic (near-field speaker, no physical contact) excitation for the
  accelerometer channel rather than resting/direct-contact coupling —
  reported informally this session as having worked for a teammate on a
  separate setup. Untested here; airborne excitation driving a board's
  resonance is a plausible, cleaner alternative to both of this doc's
  contact-coupling methods, worth a dedicated re-test rather than folding
  into this addendum's numbers.
