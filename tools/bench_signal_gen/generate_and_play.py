#!/usr/bin/env python3
"""
generate_and_play.py — Synthesizes and plays a bench test signal (pure tone,
or a bearing-fault AM model), and writes a ground-truth JSON manifest of the
values a correct satellite DSP pipeline should compute for it. See
docs/decisions/ADR-035-bench-signal-gen-laptop-speaker.md for why this uses a
laptop speaker rather than dedicated shaker hardware, and
tools/bench_signal_gen/README.md for the physical bench procedure.

Two subcommands:
  tone   a pure sine — for mic-channel DSP-accuracy validation (Phase 1).
         Ground truth is the closed-form ideal_sine_stats() of the amplitude.
  fault  a shaft-rate carrier AM-modulated by one bearing characteristic
         defect frequency (BPFO/BPFI/BSF/FTF, from
         gateway.pipeline.bearing_math) — the standard bearing-fault
         vibration model. For Phase 2 (fault-detection accuracy); built now
         because it shares all synthesis/playback plumbing with `tone`.

Usage:
    python generate_and_play.py tone --freq 1000 --duration 3 --amplitude 0.5
    python generate_and_play.py tone --freq 200 --duration 3 --out tone_200hz.wav --no-play
    python generate_and_play.py fault --bearing 6205 --shaft-hz 25 --defect bpfo --mod-depth 0.5 --duration 5
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

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
