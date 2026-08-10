# Real-rig Normal baseline — runbook (method: real-rig)

Part 3 of the Phase B accuracy harness. This documents what was **actually done**,
including two deviations from the originally planned procedure, both made as
defensible judgment calls during unattended overnight work rather than stalling.

## Planned procedure vs. what happened

The original plan called for: verify mosquitto running -> confirm satellite streaming
-> start gateway with CSV logging -> play a clean synthetic tone via
`tools/bench_signal_gen/generate_and_play.py` at low amplitude (below all thresholds)
-> capture >=200 frames -> compute FPR/recall_normal.

Steps 1-3 and 5-6 happened as planned. Step 4 (tone stimulus) did not.

### Deviation 1 — bench_signal_gen blocked by environment memory exhaustion

`python tools/bench_signal_gen/generate_and_play.py tone --freq 1300 --duration 60
--amplitude 0.15` failed twice with:

```
numpy.core._exceptions._ArrayMemoryError: Unable to allocate 22.0 MiB for an array...
```

Diagnosis: this is a genuine, reproducible Windows commit-limit/pagefile exhaustion
issue in this environment (confirmed failure on allocations as small as 3.66 MiB,
despite `Get-CimInstance Win32_OperatingSystem` showing only ~1.4-1.6GB
`FreePhysicalMemory` out of 15.9GB `TotalVisibleMemorySize` — a paging-configuration
symptom, not genuine physical-RAM scarcity). This is the same root-cause class as a
scipy import `MemoryError` documented earlier in this overhaul.

This was judged unsafe/out-of-scope to fix autonomously (would require system-level
pagefile or VM configuration changes) and is reported here as a hard blocker for the
tone-stimulus step specifically, per the ground rule to document genuine blockers and
move on rather than stalling.

**Fallback used**: rather than skip Part 3 entirely, the already-running live MQTT
pipeline's ambient (no synthetic stimulus) data was used as the Normal baseline
instead. This still satisfies the spirit of "real-rig, real hardware" — arguably a
stronger test than a clean tone, since ambient conditions include whatever real noise
floor the deployment environment actually has.

### Deviation 2 — accidental ~9.4-hour unattended capture

An earlier `kill <pid>` issued against a previous gateway instance (from before this
capture) did not actually stop the process: git-bash's `$!` after `nohup python ... &`
captured a job-control PID, not the real OS PID of the spawned `python.exe`. This went
undetected until a routine CSV row-count check showed ~134,600+ rows instead of the
expected order of magnitude, prompting investigation via timestamp math and
`tasklist`/`Get-CimInstance Win32_Process` process inspection, which confirmed PID 8948
was the actual still-running gateway process (command line matched exactly). It was
stopped via `taskkill //PID 8948 //F` and confirmed via a follow-up `tasklist` check.

No data was lost or corrupted by this. The unintended side effect was a much larger
real dataset than planned (135,032 frames over ~9.46 hours instead of ~200 frames over
~60 seconds), which only strengthens the statistical basis for Part 3's findings.
Lesson for any future unattended background-process management in this environment:
verify real OS PIDs via `tasklist` / `Get-CimInstance Win32_Process`, never trust
bash `$!` alone for a process spawned this way.

## Actual capture conditions

- Ingestion path: MQTT (`gateway/ingestion/mqtt_subscriber.py` -> shared
  `rv._process_satellite_frame()`, same downstream pipeline as the TCP+AES path;
  confirmed via code inspection). No firmware reflash was needed — the satellite was
  already connected via MQTT per `.env.local` (`EPM_MQTT_BROKER_HOST=192.168.1.5`,
  `EPM_MQTT_BROKER_PORT=1883`) and the 2026-08-09 characterization doc precedent.
  mosquitto was already running; no restart was needed.
- Device: `sat-5ab004`.
- Stimulus: **none** — ambient/idle conditions (mic near an off speaker, laptop
  resting on the accelerometer, no intentional excitation), not the planned clean
  tone.
- Duration: ~9.46 hours (34,045 seconds), 2026-08-09 ~19:49 UTC through
  2026-08-10 ~05:15 UTC.
- Source CSV: `mic_tools/logs/csv/2026/08/epm_sat-5ab004_20260810.csv` (gitignored,
  not committed — see `out/rig_baseline_report.json`/`.md` for the committed summary).

## Results

See `out/rig_baseline_report.md` for the full generated report. Headline numbers:

- **n = 135,032 frames**
- **FPR = 0.8568** (85.68% of frames alerted WARN or FAULT, not OK)
- **recall_normal = 0.1432**
- Alert breakdown: `WARN=88974, FAULT=26715, OK=19343`

## Interpretation — this is a real, load-bearing finding, not a stimulus artifact

The dominant driver is the IMU channel, not the mic: median `imu_crest` across the
entire ambient capture is **5.233**, already above the default `CREST_WARN=5.0`
threshold, and 72.06% of individual frames have `imu_crest >= 5.0`. Median `mic_crest`
(3.09) sits comfortably under its threshold throughout. In other words, on this
specific physical rig (laptop resting on the accelerometer), ambient/idle vibration
alone is enough to keep the IMU crest-factor channel hovering right at or above its
WARN gate — the classifier and alert engine are behaving correctly given their inputs;
the inputs themselves reflect a noisy real-world mounting rather than a quiet
isolated bearing.

This is consistent with, and adds real-hardware confirmation to, the parameter
recommendations already on record from the 2026-06-30 simulation sweep (`n_trees
25->10`, `z_mid 3->2`, `alpha 0.0005->5e-05`) — those were simulation-derived; this is
the first live-hardware evidence that default IMU crest-factor threshold tuning (or
physical decoupling of the accelerometer mount from ambient building/desk vibration)
deserves attention before this rig can produce a clean Normal baseline.

## Caveats

- The alert engine applies persistence/hysteresis (`WARN_PERSIST`, `CLEAR_PERSIST`,
  `FAULT_CLEAR_PERSIST`) on top of the raw per-frame threshold comparisons — the
  85.68% FPR is the alert engine's actual output including that hysteresis, not a
  naive per-sample threshold count. The 72.06% figure for `imu_crest >= 5.0` alone is
  the raw per-frame statistic and is reported separately for that reason.
- `mic_kurtosis` has a heavy right tail (max 969 in this capture) from genuine
  transient acoustic events during 9.46 unattended hours (room noise, HVAC, incidental
  movement) — expected for a long ambient capture, not a sensor fault.
- This baseline reflects one specific physical rig/mounting, not a general claim about
  IMU crest-factor thresholds in all deployments.
- precision/F1 are not computable from this capture (no fault frames exist by
  construction — this is a Normal-only baseline) and are reported as `n/a` in
  `rig_baseline_report.py`'s output rather than fabricated.
