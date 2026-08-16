# How this works, how to test it yourself, and what to tell Rahul

Two audiences for this doc: Part 1 is for Abhi (or anyone continuing this project) —
the real end-to-end pipeline and how to repeat the bench test on your own. Part 2 is a
short, factual explainer you could actually send Rahul or use as a project write-up —
no internal process notes, no comparison commentary, just what the system does and how
to try it.

---

## Part 1 — How the whole thing works, and how to test it

### The pipeline, end to end

1. **Satellite** (XIAO ESP32-S3): an I2S microphone (48kHz) and an SPI accelerometer
   (KX134, 25.6kHz, 3 axes) each feed a task that windows the signal, runs an FFT, and
   computes six scalar statistics (rms, crest factor, kurtosis, std, peak, skewness).
2. Every ~200ms, the satellite packs the latest averaged spectra + scalars into one
   binary frame and publishes it over WiFi/MQTT.
3. **Gateway** (a Python process on the laptop) subscribes to that MQTT topic, decodes
   the frame, and runs it through:
   - `_band_ratios()` — splits the mic spectrum into low/mid/high frequency-energy
     fractions.
   - `_fault_candidate_scores()` / `_classify_fault_type()` — pattern-matches those
     ratios plus crest/kurtosis against four fault signatures (Bearing Fault, Mechanical
     Imbalance, Shaft Misalignment, Mechanical Looseness) and picks whichever one's
     evidence is quantitatively strongest.
   - A Bayesian/anomaly-scoring layer (HST online detector + z-score) runs in parallel
     as a second, independent signal.
   - `bearing_corroboration.py` cross-checks any "Bearing Fault" label against the
     physics: does the mic's actual FFT peak line up with a real bearing defect
     frequency (BPFO/BPFI/BSF/FTF, computed from the bearing's geometry and shaft speed)?
4. Alerts, history, and a live dashboard come out the other end.

### Why the bench rig exists

There's no physically failing machine to test fault detection against. The workaround:
a laptop speaker plays a synthesized signal shaped like a real fault signature (an
impulsive "ringdown burst" train for Bearing Fault/Imbalance, broadband multi-tone for
Looseness), with the mic placed near the speaker and the accelerometer resting on/against
the laptop to pick up the vibration too. Each synthesized signal has an exact, closed-form
"ground truth" (the crest factor and kurtosis it's mathematically guaranteed to produce),
so a real capture can be checked against a known-correct answer instead of just "did an
alert fire."

### How to run it yourself

```
# 1. Position the rig: mic near the laptop speaker, accelerometer on/against the laptop.
# 2. Generate a test signal + its ground-truth manifest:
python tools/bench_signal_gen/generate_and_play.py bearing --bearing 6205 --shaft-hz 25 --defect bpfo --duration 5
python tools/bench_signal_gen/generate_and_play.py imbalance --preset safe --duration 5
python tools/bench_signal_gen/generate_and_play.py looseness --duration 5

# 3. While it plays, capture real satellite frames and compare against the manifest:
python tools/bench_signal_gen/capture_and_compare.py --manifest <path-to-manifest.json>
```

Read the printed comparison yourself — measured crest/kurtosis/band-ratios vs. the
manifest's expected values, and what the classifier actually labeled it. A real result
looks like numbers close to the manifest's ground truth and the expected fault label; a
bad rig position or a volume issue looks like near-silence or numbers nowhere close.

### A known limitation, current as of this doc

The classifier's frequency-band math currently excludes the lowest wire bin (0–187.5 Hz
at the current 48kHz mic rate) from every ratio it uses — and this rig's speaker has only
ever cleanly reproduced 15–90 Hz, which sits entirely inside that excluded bin. Whether
this is fixable or a hard rig/classifier mismatch is being actively investigated
(`docs/CONTINUE_PHASE_B_RIG_TEST_PROMPT.md` has the current state).

---

## Part 2 — What to tell Rahul (factual, no internal notes)

> Hey — wanted to share something that might be useful for your side too.
>
> I built a way to validate fault detection without needing an actual failing machine: a
> laptop speaker plays a signal synthesized to match a real fault's statistical
> signature (crest factor, kurtosis, frequency-band energy), with the ground truth known
> exactly ahead of time since it's closed-form math, not a recording. The mic and
> accelerometer pick it up like a real fault, and the pipeline gets checked against the
> known-correct answer end to end — real hardware, real MQTT frames, real classifier
> output, just a synthetic stimulus.
>
> Current fault categories covered: Bearing Fault (impulsive ringdown bursts at a real
> BPFO/BPFI/BSF/FTF defect frequency), Mechanical Imbalance, and Mechanical Looseness
> (broadband). The synthesis code is in `tools/bench_signal_gen/` if you want to try it
> against your own satellite + gateway — it only needs a speaker and your existing
> mic/accelerometer, no extra hardware.
>
> Also, separately from the fault-detection work: my satellite's default config now
> matches what I confirmed from your repo (broker address, wire protocol, node-ID
> scheme) — should be able to join your base station without any changes on your end,
> if you want to try pairing them for real at some point.

Feel free to trim or adjust the tone — this is meant as a starting draft, not a final
copy-paste.
