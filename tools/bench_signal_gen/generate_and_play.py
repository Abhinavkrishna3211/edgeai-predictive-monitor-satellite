#!/usr/bin/env python3
"""
generate_and_play.py — Synthesizes and plays a bench test signal (pure tone,
a bearing-fault AM model, or fault-shaped impulsive/broadband/peaky content),
and writes a ground-truth JSON manifest of the values a correct satellite DSP
pipeline should compute for it. See
docs/decisions/ADR-035-bench-signal-gen-laptop-speaker.md for why this uses a
laptop speaker rather than dedicated shaker hardware, and
tools/bench_signal_gen/README.md for the physical bench procedure.

Subcommands:
  tone       a pure sine — for mic-channel DSP-accuracy validation (Phase 1).
             Ground truth is the closed-form ideal_sine_stats() of the amplitude.
  fault      a shaft-rate carrier AM-modulated by one bearing characteristic
             defect frequency (BPFO/BPFI/BSF/FTF, from
             gateway.pipeline.bearing_math) — the standard bearing-fault
             vibration model. Has no closed-form ground truth (kept as-is).
  bearing    a periodic decaying-ringdown impact train at a defect frequency
             (BPFO/BPFI/BSF/FTF) — impulsive bearing-fault content shaped to
             actually cross the mic_kurtosis/hi_r gate that _classify_fault_type()
             keys Bearing Fault on. Ground truth is the exact closed-form
             ringdown_burst_stats() of the synthesis parameters.
  imbalance  a single-carrier ringdown-burst train repeating once per shaft
             revolution (low-band carrier). Exact same ringdown_burst_stats()
             math as `bearing`, at a different (tau, carrier) operating point.
             Two presets: `safe` (crest~3, comfortably below CREST_WARN — the
             physically-honest "moderate imbalance-like content" operating
             point) and `threshold` (crest~5.1, kurtosis~8.0 — narrowly inside
             the real Mechanical Imbalance gate, but only a few percent of
             margin on both sides; see PHASE_B_REPORT.md's addendum for why
             this is a threshold-robustness probe, not evidence of a genuine
             imbalance-detection capability, and physically resembles a
             once-per-revolution impact/looseness signature more than smooth
             rotating-mass imbalance).
  looseness  multiple simultaneous ringdown-burst carriers spread across the
             low+mid bands (broadband/multi-harmonic energy) — for Mechanical
             Looseness. No closed-form cross-term algebra exists for a sum of
             independently-phased carriers, so ground truth is numerically
             computed from a densely-oversampled render (see
             broadband_stats_numeric()), cross-checked for convergence across
             sample rate / duration in test_bench_signal_gen.py.

Usage:
    python generate_and_play.py tone --freq 1000 --duration 3 --amplitude 0.5
    python generate_and_play.py tone --freq 200 --duration 3 --out tone_200hz.wav --no-play
    python generate_and_play.py fault --bearing 6205 --shaft-hz 25 --defect bpfo --mod-depth 0.5 --duration 5
    python generate_and_play.py bearing --bearing 6205 --shaft-hz 25 --defect bpfo --duration 5
    python generate_and_play.py imbalance --preset safe --duration 5
    python generate_and_play.py imbalance --preset threshold --duration 5
    python generate_and_play.py looseness --duration 5
"""

import argparse
import datetime
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gateway.pipeline.bearing_math import BearingFreqs, parse_bearing_arg

_MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"

_DEFECT_FIELD = {
    "bpfo": "bpfo",
    "bpfi": "bpfi",
    "bsf": "bsf",
    "ftf": "ftf",
}


def ideal_sine_stats(amplitude):
    """Closed-form statistics of an ideal, infinite, noise-free sine wave of
    the given peak amplitude. This is the ground truth a correct DSP pipeline
    should reproduce (up to windowing/quantization/noise-floor effects a real
    capture introduces) for the `tone` subcommand."""
    return {
        "rms": amplitude / math.sqrt(2),
        "peak": amplitude,
        "crest_factor": math.sqrt(2),
        "kurtosis_excess": -1.5,
        "skewness": 0.0,
    }


def select_defect_frequency(bearing_freqs, defect):
    """Pick the bearing_math-computed characteristic frequency (Hz) the
    `--defect` argument names. Raises ValueError for an unknown key rather
    than KeyError, so the CLI can report a clean message."""
    field = _DEFECT_FIELD.get(defect)
    if field is None:
        raise ValueError(
            f"unknown defect {defect!r}, expected one of {sorted(_DEFECT_FIELD)}"
        )
    return getattr(bearing_freqs, field)


_IMBALANCE_PRESETS = {
    # tau_ms tuned (see PHASE_B_REPORT.md addendum) for the default shaft_hz=25.0Hz
    # (period_s=1/shaft_hz=0.04s). Changing --shaft-hz shifts the tau/period ratio
    # and therefore the achieved crest/kurtosis away from these targets -- the
    # manifest's "expected" block always reports the exact closed-form result for
    # whatever parameters were actually used, so this is a documentation nicety,
    # not a correctness dependency.
    "safe":      {"tau_ms": 18.0, "description": "moderate crest (~3), comfortably below CREST_WARN -- physically-honest imbalance-like content"},
    "threshold": {"tau_ms": 5.9,  "description": "crest~5.1 / kurtosis~8.0, narrowly inside the real Mechanical Imbalance gate -- threshold-robustness probe, not a smooth-imbalance model"},
}

_LOOSENESS_DEFAULT_CARRIERS_HZ = (300.0, 800.0, 1500.0)


def _exp_cos_integral(a, b, length_s):
    """Exact value of integral_0^length_s exp(-a*t) * cos(b*t) dt, for a > 0.

    Standard closed-form antiderivative of an exponentially-damped cosine
    (verify by differentiating the RHS w.r.t. length_s): as length_s -> inf
    this converges to the textbook Laplace-transform result a/(a^2+b^2).
    """
    denom = a * a + b * b
    if denom == 0.0:
        return length_s
    tail = math.exp(-a * length_s) * (a * math.cos(b * length_s) - b * math.sin(b * length_s))
    return (a - tail) / denom


def ringdown_burst_stats(amplitude, carrier_hz, tau_s, burst_dur_s, period_s):
    """Exact closed-form statistics for a periodically-repeated exponentially
    decaying ringdown burst:

        x(t) = amplitude * exp(-t/tau_s) * cos(2*pi*carrier_hz*t)   0 <= t < burst_dur_s
        x(t) = 0                                                     burst_dur_s <= t < period_s

    repeated with period period_s. The carrier uses cos (not sin) specifically
    so the envelope's peak coincides exactly with the carrier's peak at each
    burst's t=0: |x(t)| <= amplitude*exp(-t/tau_s) <= amplitude for all t>=0,
    with equality only at t=0, so peak == amplitude exactly, no search needed.

    Raw moments E[x^m] are obtained by expanding cos^m(carrier*t) via the
    standard power-reduction identities into a sum of cos(k*carrier*t) terms,
    each integrated exactly via _exp_cos_integral() with an m*(1/tau_s) decay
    rate (since exp(-t/tau_s)^m = exp(-m*t/tau_s)). This is a finite-window
    (0..burst_dur_s) integral, so it is exact -- no infinite-tail truncation
    is involved. Verified numerically against dense-oversampled direct-sample
    statistics of the rendered waveform in test_bench_signal_gen.py (<0.1%
    relative error on rms/crest/kurtosis/skewness across multiple parameter
    sets).
    """
    omega = 2.0 * math.pi * carrier_hz

    def moment_integral(power, harmonic):
        return _exp_cos_integral(power / tau_s, harmonic * omega, burst_dur_s)

    # cos^1(x) = cos(x)
    m1 = moment_integral(1, 1) / period_s
    # cos^2(x) = 1/2 + 1/2 cos(2x)
    m2 = 0.5 * (moment_integral(2, 0) + moment_integral(2, 2)) / period_s
    # cos^3(x) = 3/4 cos(x) + 1/4 cos(3x)
    m3 = 0.25 * (3 * moment_integral(3, 1) + moment_integral(3, 3)) / period_s
    # cos^4(x) = 3/8 + 1/2 cos(2x) + 1/8 cos(4x)
    m4 = (3 * moment_integral(4, 0) + 4 * moment_integral(4, 2) + moment_integral(4, 4)) / 8.0 / period_s

    mean = amplitude * m1
    e_x2 = amplitude ** 2 * m2
    e_x3 = amplitude ** 3 * m3
    e_x4 = amplitude ** 4 * m4

    variance = e_x2 - mean ** 2
    centered3 = e_x3 - 3 * mean * e_x2 + 2 * mean ** 3
    centered4 = e_x4 - 4 * mean * e_x3 + 6 * mean ** 2 * e_x2 - 3 * mean ** 4
    std = math.sqrt(variance)
    rms = math.sqrt(e_x2)

    return {
        "mean": mean,
        "rms": rms,
        "peak": amplitude,
        "crest_factor": amplitude / rms,
        "kurtosis_excess": centered4 / variance ** 2 - 3.0,
        "skewness": centered3 / std ** 3,
    }


def synthesize_ringdown_burst_train(carrier_hz, tau_s, burst_dur_s, period_s, duration_s, amplitude, sample_rate):
    """Render the waveform whose exact statistics are ringdown_burst_stats()."""
    n = round(duration_s * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    phase = np.mod(t, period_s)
    envelope = np.where(phase < burst_dur_s, np.exp(-phase / tau_s), 0.0)
    x = amplitude * envelope * np.cos(2 * math.pi * carrier_hz * phase)
    return x.astype(np.float32)


def synthesize_broadband_burst_train(carriers_hz, tau_s, burst_dur_s, period_s, duration_s, amplitude, sample_rate):
    """Sum of ringdown-burst trains at several carriers, all sharing the same
    envelope/period so they burst in lockstep -- broadband/multi-harmonic
    peaky content for Mechanical Looseness. Every carrier's cos(...) and the
    shared envelope both peak at each burst's t=0, so the summed peak occurs
    exactly at t=0 too (envelope(t) <= envelope(0)=1 and each cos term <= 1
    for all t), making the amplitude scaling below exact rather than a
    post-hoc peak search: analytically the pre-scale peak is exactly
    len(carriers_hz) (envelope(0)=1, cos(0)=1 for every carrier), so dividing
    by the measured peak and multiplying by `amplitude` lands peak==amplitude
    to floating-point precision.
    """
    n = round(duration_s * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    phase = np.mod(t, period_s)
    envelope = np.where(phase < burst_dur_s, np.exp(-phase / tau_s), 0.0)
    x = np.zeros(n, dtype=np.float64)
    for carrier_hz in carriers_hz:
        x += envelope * np.cos(2 * math.pi * carrier_hz * phase)
    peak = np.max(np.abs(x))
    if peak > 0:
        x *= (amplitude / peak)
    return x.astype(np.float32)


def broadband_stats_numeric(carriers_hz, tau_s, burst_dur_s, period_s, amplitude,
                             sample_rate=48000, n_periods=1500):
    """Numerically-verified ground truth for synthesize_broadband_burst_train().

    No closed form exists for the cross-terms between independently-phased
    carriers' contributions to E[x^3]/E[x^4], so this renders enough periods
    at the target sample rate for the sample statistics to converge (checked
    for stability across sample_rate/n_periods in test_bench_signal_gen.py --
    <0.2% drift going from 300 to 3000 periods at fixed 48kHz), then computes
    direct sample moments. This is the "numerically-verified" ground truth
    the task allows as an alternative to closed-form, for exactly this
    multi-carrier case.
    """
    duration_s = period_s * n_periods
    x = synthesize_broadband_burst_train(
        carriers_hz, tau_s, burst_dur_s, period_s, duration_s, amplitude, sample_rate
    ).astype(np.float64)
    mean = float(x.mean())
    variance = float(np.mean((x - mean) ** 2))
    centered3 = float(np.mean((x - mean) ** 3))
    centered4 = float(np.mean((x - mean) ** 4))
    std = math.sqrt(variance)
    rms = math.sqrt(float(np.mean(x ** 2)))
    peak = float(np.max(np.abs(x)))
    return {
        "mean": mean,
        "rms": rms,
        "peak": peak,
        "crest_factor": peak / rms,
        "kurtosis_excess": centered4 / variance ** 2 - 3.0,
        "skewness": centered3 / std ** 3,
    }


def band_ratios_from_samples(x, sample_rate=48000, n_bins=128):
    """Numerically-computed hi_r/lo_r/mid_r ground truth, mirroring the exact
    bucketing scheme gateway.pipeline.alerting._band_ratios() uses on the real
    wire spectrum (gateway/pipeline/alerting.py:34-57): lo=0-500Hz,
    mid=500-2000Hz, hi=2000Hz-Nyquist, hz_per=Nyquist/n_bins, power in each of
    n_bins buckets summed and normalized by total power excluding DC.

    Deliberately duplicated here (rather than imported) to avoid coupling this
    standalone bench tool to recv_verify.py's module-level state; re-sync this
    if alerting._band_ratios()'s bucketing ever changes.

    Must be evaluated at the device's fixed MIC_FS_HZ (48000, the default),
    independent of whatever sample rate the WAV happens to be synthesized at
    -- the bucket edges are a property of the wire spectrum format, not of
    this tool's playback file.
    """
    x = np.asarray(x, dtype=np.float64)
    power_full = np.abs(np.fft.rfft(x)) ** 2
    nyquist = sample_rate / 2.0
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sample_rate)
    bucket_edges = np.linspace(0, nyquist, n_bins + 1)
    bucket_idx = np.clip(np.digitize(freqs, bucket_edges) - 1, 0, n_bins - 1)
    bucket_power = np.zeros(n_bins)
    for i in range(n_bins):
        bucket_power[i] = power_full[bucket_idx == i].sum()
    hz_per = nyquist / n_bins
    lo_end = max(1, int(500 / hz_per))
    mid_end = max(lo_end + 1, int(2000 / hz_per))
    total = bucket_power[1:].sum() + 1e-10
    lo_r = float(bucket_power[1:lo_end].sum() / total)
    mid_r = float(bucket_power[lo_end:mid_end].sum() / total)
    hi_r = float(bucket_power[mid_end:].sum() / total)
    return {"hi_r": hi_r, "lo_r": lo_r, "mid_r": mid_r}


def synthesize_tone(freq_hz, duration_s, amplitude, sample_rate):
    n = round(duration_s * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    return (amplitude * np.sin(2 * math.pi * freq_hz * t)).astype(np.float32)


def synthesize_fault(shaft_hz, defect_hz, mod_depth, duration_s, amplitude, sample_rate):
    """Shaft-rate carrier, amplitude-modulated by the defect-rate sideband —
    the standard bearing-fault vibration model: a defect impact train at the
    characteristic frequency modulates the shaft-synchronous vibration."""
    n = round(duration_s * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    carrier = np.sin(2 * math.pi * shaft_hz * t)
    envelope = 1.0 + mod_depth * np.sin(2 * math.pi * defect_hz * t)
    return (amplitude * envelope * carrier).astype(np.float32)


def save_wav(path, samples, sample_rate):
    from scipy.io import wavfile

    clipped = np.clip(samples, -1.0, 1.0)
    wavfile.write(str(path), sample_rate, (clipped * 32767).astype(np.int16))


def play_samples(samples, sample_rate):
    import sounddevice as sd

    sd.play(samples, sample_rate)
    sd.wait()


def write_manifest(manifest, prefix):
    _MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = _MANIFEST_DIR / f"{prefix}_{stamp}.json"
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)
    return out_path


def cmd_tone(args):
    samples = synthesize_tone(args.freq, args.duration, args.amplitude, args.sample_rate)

    manifest = {
        "mode": "tone",
        "generated_at": datetime.datetime.now().isoformat(),
        "freq_hz": args.freq,
        "duration_s": args.duration,
        "amplitude": args.amplitude,
        "sample_rate": args.sample_rate,
        "channel_hint": "mic",
        "wav_path": str(args.out) if args.out else None,
        "expected": ideal_sine_stats(args.amplitude),
    }

    if args.out:
        save_wav(args.out, samples, args.sample_rate)
    if not args.no_play:
        play_samples(samples, args.sample_rate)

    manifest_path = write_manifest(manifest, f"tone_{args.freq:g}hz")
    print(f"wrote manifest {manifest_path}")
    return 0


def cmd_fault(args):
    geom = parse_bearing_arg(args.bearing)
    if geom is None:
        print(f"error: unknown bearing {args.bearing!r}", file=sys.stderr)
        return 2

    bf = BearingFreqs.from_shaft_hz(args.shaft_hz, geom)
    target_freq = select_defect_frequency(bf, args.defect)

    samples = synthesize_fault(
        args.shaft_hz, target_freq, args.mod_depth, args.duration, args.amplitude, args.sample_rate
    )

    manifest = {
        "mode": "fault",
        "generated_at": datetime.datetime.now().isoformat(),
        "bearing": geom.name,
        "shaft_hz": args.shaft_hz,
        "defect": args.defect,
        "target_freq_hz": target_freq,
        "carrier_hz": args.shaft_hz,
        "sideband_hz": target_freq,
        "mod_depth": args.mod_depth,
        "duration_s": args.duration,
        "amplitude": args.amplitude,
        "sample_rate": args.sample_rate,
        "channel_hint": "accel_z",
        "wav_path": str(args.out) if args.out else None,
    }

    if args.out:
        save_wav(args.out, samples, args.sample_rate)
    if not args.no_play:
        play_samples(samples, args.sample_rate)

    manifest_path = write_manifest(manifest, f"fault_{geom.name}_{args.defect}")
    print(f"wrote manifest {manifest_path}")
    return 0


def cmd_bearing(args):
    geom = parse_bearing_arg(args.bearing)
    if geom is None:
        print(f"error: unknown bearing {args.bearing!r}", file=sys.stderr)
        return 2

    bf = BearingFreqs.from_shaft_hz(args.shaft_hz, geom)
    defect_hz = select_defect_frequency(bf, args.defect)
    period_s = 1.0 / defect_hz
    tau_s = args.tau_ms / 1000.0
    burst_dur_s = args.burst_ms / 1000.0 if args.burst_ms is not None else min(4 * tau_s, 0.5 * period_s)
    if burst_dur_s >= period_s:
        print(f"error: burst_dur_s={burst_dur_s:.6f} must be < period_s={period_s:.6f} "
              f"(defect_hz={defect_hz:.3f}Hz) -- lower --tau-ms/--burst-ms or pick a faster defect",
              file=sys.stderr)
        return 2

    samples = synthesize_ringdown_burst_train(
        args.resonance_hz, tau_s, burst_dur_s, period_s, args.duration, args.amplitude, args.sample_rate
    )
    expected = ringdown_burst_stats(args.amplitude, args.resonance_hz, tau_s, burst_dur_s, period_s)
    expected.update(band_ratios_from_samples(
        synthesize_ringdown_burst_train(args.resonance_hz, tau_s, burst_dur_s, period_s,
                                         period_s * 500, args.amplitude, sample_rate=48000)
    ))

    manifest = {
        "mode": "bearing",
        "generated_at": datetime.datetime.now().isoformat(),
        "bearing": geom.name,
        "shaft_hz": args.shaft_hz,
        "defect": args.defect,
        "defect_hz": defect_hz,
        "resonance_hz": args.resonance_hz,
        "tau_s": tau_s,
        "burst_dur_s": burst_dur_s,
        "period_s": period_s,
        "duration_s": args.duration,
        "amplitude": args.amplitude,
        "sample_rate": args.sample_rate,
        "channel_hint": "mic",
        "wav_path": str(args.out) if args.out else None,
        "expected": expected,
        "ground_truth_method": "closed_form (rms/peak/crest/kurtosis/skewness/mean) + numeric (hi_r/lo_r/mid_r)",
    }

    if args.out:
        save_wav(args.out, samples, args.sample_rate)
    if not args.no_play:
        play_samples(samples, args.sample_rate)

    manifest_path = write_manifest(manifest, f"bearing_{geom.name}_{args.defect}")
    print(f"wrote manifest {manifest_path}")
    return 0


def cmd_imbalance(args):
    preset = _IMBALANCE_PRESETS[args.preset]
    tau_ms = args.tau_ms if args.tau_ms is not None else preset["tau_ms"]
    tau_s = tau_ms / 1000.0
    period_s = 1.0 / args.shaft_hz
    burst_dur_s = period_s

    samples = synthesize_ringdown_burst_train(
        args.resonance_hz, tau_s, burst_dur_s, period_s, args.duration, args.amplitude, args.sample_rate
    )
    expected = ringdown_burst_stats(args.amplitude, args.resonance_hz, tau_s, burst_dur_s, period_s)
    expected.update(band_ratios_from_samples(
        synthesize_ringdown_burst_train(args.resonance_hz, tau_s, burst_dur_s, period_s,
                                         period_s * 800, args.amplitude, sample_rate=48000)
    ))

    manifest = {
        "mode": "imbalance",
        "generated_at": datetime.datetime.now().isoformat(),
        "preset": args.preset,
        "preset_description": preset["description"],
        "shaft_hz": args.shaft_hz,
        "resonance_hz": args.resonance_hz,
        "tau_s": tau_s,
        "burst_dur_s": burst_dur_s,
        "period_s": period_s,
        "duration_s": args.duration,
        "amplitude": args.amplitude,
        "sample_rate": args.sample_rate,
        "channel_hint": "mic",
        "wav_path": str(args.out) if args.out else None,
        "expected": expected,
        "ground_truth_method": "closed_form (rms/peak/crest/kurtosis/skewness/mean) + numeric (hi_r/lo_r/mid_r)",
    }

    if args.out:
        save_wav(args.out, samples, args.sample_rate)
    if not args.no_play:
        play_samples(samples, args.sample_rate)

    manifest_path = write_manifest(manifest, f"imbalance_{args.preset}")
    print(f"wrote manifest {manifest_path}")
    return 0


def cmd_looseness(args):
    carriers_hz = [float(c) for c in args.carriers_hz.split(",")]
    tau_s = args.tau_ms / 1000.0
    burst_dur_s = args.burst_ms / 1000.0
    period_s = 1.0 / args.period_hz
    if burst_dur_s >= period_s:
        print(f"error: burst_dur_s={burst_dur_s:.6f} must be < period_s={period_s:.6f} "
              f"-- lower --burst-ms or --period-hz", file=sys.stderr)
        return 2

    samples = synthesize_broadband_burst_train(
        carriers_hz, tau_s, burst_dur_s, period_s, args.duration, args.amplitude, args.sample_rate
    )
    expected = broadband_stats_numeric(carriers_hz, tau_s, burst_dur_s, period_s, args.amplitude)
    expected.update(band_ratios_from_samples(
        synthesize_broadband_burst_train(carriers_hz, tau_s, burst_dur_s, period_s,
                                          period_s * 800, args.amplitude, sample_rate=48000)
    ))

    manifest = {
        "mode": "looseness",
        "generated_at": datetime.datetime.now().isoformat(),
        "carriers_hz": carriers_hz,
        "tau_s": tau_s,
        "burst_dur_s": burst_dur_s,
        "period_s": period_s,
        "duration_s": args.duration,
        "amplitude": args.amplitude,
        "sample_rate": args.sample_rate,
        "channel_hint": "mic",
        "wav_path": str(args.out) if args.out else None,
        "expected": expected,
        "ground_truth_method": "numeric (dense-oversampled direct-sample statistics; no closed form for multi-carrier cross-terms)",
    }

    if args.out:
        save_wav(args.out, samples, args.sample_rate)
    if not args.no_play:
        play_samples(samples, args.sample_rate)

    manifest_path = write_manifest(manifest, "looseness")
    print(f"wrote manifest {manifest_path}")
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    tone = sub.add_parser("tone", help="Play a pure sine tone")
    tone.add_argument("--freq", type=float, required=True, help="Tone frequency in Hz")
    tone.add_argument("--duration", type=float, required=True, help="Duration in seconds")
    tone.add_argument("--amplitude", type=float, default=0.8, help="Peak amplitude, 0-1 (default 0.8)")
    tone.add_argument("--sample-rate", type=int, default=48000, help="Sample rate in Hz (default 48000)")
    tone.add_argument("--out", type=Path, default=None, help="Optional WAV output path")
    tone.add_argument("--no-play", action="store_true", help="Render/save only, do not play")
    tone.set_defaults(func=cmd_tone)

    fault = sub.add_parser("fault", help="Play a synthesized bearing-fault AM signal")
    fault.add_argument("--bearing", required=True, help="Bearing key (e.g. 6205) or n,D,d[,alpha]")
    fault.add_argument("--shaft-hz", type=float, required=True, help="Shaft rotation frequency in Hz")
    fault.add_argument("--defect", choices=sorted(_DEFECT_FIELD), required=True, help="Which defect frequency to target")
    fault.add_argument("--mod-depth", type=float, default=0.5, help="AM sideband depth, 0-1 (default 0.5)")
    fault.add_argument("--duration", type=float, required=True, help="Duration in seconds")
    fault.add_argument("--amplitude", type=float, default=0.8, help="Carrier peak amplitude, 0-1 (default 0.8)")
    fault.add_argument("--sample-rate", type=int, default=48000, help="Sample rate in Hz (default 48000)")
    fault.add_argument("--out", type=Path, default=None, help="Optional WAV output path")
    fault.add_argument("--no-play", action="store_true", help="Render/save only, do not play")
    fault.set_defaults(func=cmd_fault)

    bearing = sub.add_parser("bearing", help="Play an impulsive ringdown-burst bearing-fault signal")
    bearing.add_argument("--bearing", default="6205", help="Bearing key (e.g. 6205) or n,D,d[,alpha] (default 6205)")
    bearing.add_argument("--shaft-hz", type=float, default=25.0, help="Shaft rotation frequency in Hz (default 25.0, i.e. 1500 RPM)")
    bearing.add_argument("--defect", choices=sorted(_DEFECT_FIELD), default="bpfo", help="Which defect frequency sets the impact-train period (default bpfo)")
    bearing.add_argument("--resonance-hz", type=float, default=3500.0, help="Structural resonance the impacts excite, Hz (default 3500.0)")
    bearing.add_argument("--tau-ms", type=float, default=1.5, help="Ringdown decay time constant in ms (default 1.5)")
    bearing.add_argument("--burst-ms", type=float, default=None, help="Burst duration in ms (default: min(4*tau, period/2))")
    bearing.add_argument("--duration", type=float, required=True, help="Duration in seconds")
    bearing.add_argument("--amplitude", type=float, default=0.8, help="Peak amplitude, 0-1 (default 0.8)")
    bearing.add_argument("--sample-rate", type=int, default=48000, help="Sample rate in Hz (default 48000)")
    bearing.add_argument("--out", type=Path, default=None, help="Optional WAV output path")
    bearing.add_argument("--no-play", action="store_true", help="Render/save only, do not play")
    bearing.set_defaults(func=cmd_bearing)

    imbalance = sub.add_parser("imbalance", help="Play a once-per-revolution ringdown-burst imbalance-like signal")
    imbalance.add_argument("--preset", choices=sorted(_IMBALANCE_PRESETS), required=True,
                            help="safe = moderate crest (~3), robust demo; "
                                 "threshold = crest~5.1/kurtosis~8.0, narrowly inside the real gate (robustness probe, not a smooth-imbalance model)")
    imbalance.add_argument("--shaft-hz", type=float, default=25.0, help="Shaft rotation frequency in Hz (default 25.0, i.e. 1500 RPM); presets were tuned for this default")
    imbalance.add_argument("--resonance-hz", type=float, default=150.0, help="Low-band structural resonance excited once per revolution, Hz (default 150.0)")
    imbalance.add_argument("--tau-ms", type=float, default=None, help="Override the preset's ringdown decay time constant in ms")
    imbalance.add_argument("--duration", type=float, required=True, help="Duration in seconds")
    imbalance.add_argument("--amplitude", type=float, default=0.8, help="Peak amplitude, 0-1 (default 0.8)")
    imbalance.add_argument("--sample-rate", type=int, default=48000, help="Sample rate in Hz (default 48000)")
    imbalance.add_argument("--out", type=Path, default=None, help="Optional WAV output path")
    imbalance.add_argument("--no-play", action="store_true", help="Render/save only, do not play")
    imbalance.set_defaults(func=cmd_imbalance)

    looseness = sub.add_parser("looseness", help="Play broadband multi-carrier ringdown-burst looseness signal")
    looseness.add_argument("--carriers-hz", default=",".join(str(c) for c in _LOOSENESS_DEFAULT_CARRIERS_HZ),
                            help=f"Comma-separated carrier frequencies in Hz, spread across low+mid bands "
                                 f"(default {','.join(str(c) for c in _LOOSENESS_DEFAULT_CARRIERS_HZ)})")
    looseness.add_argument("--tau-ms", type=float, default=4.0, help="Ringdown decay time constant in ms, shared by all carriers (default 4.0)")
    looseness.add_argument("--burst-ms", type=float, default=20.0, help="Burst duration in ms (default 20.0)")
    looseness.add_argument("--period-hz", type=float, default=30.0, help="Burst repetition rate in Hz (default 30.0)")
    looseness.add_argument("--duration", type=float, required=True, help="Duration in seconds")
    looseness.add_argument("--amplitude", type=float, default=0.8, help="Peak amplitude, 0-1 (default 0.8)")
    looseness.add_argument("--sample-rate", type=int, default=48000, help="Sample rate in Hz (default 48000)")
    looseness.add_argument("--out", type=Path, default=None, help="Optional WAV output path")
    looseness.add_argument("--no-play", action="store_true", help="Render/save only, do not play")
    looseness.set_defaults(func=cmd_looseness)

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
