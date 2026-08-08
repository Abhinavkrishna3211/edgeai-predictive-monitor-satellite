#!/usr/bin/env python3
"""
capture_and_compare.py — Subscribes to a running satellite's MQTT data
topic, captures live frames for a window, and diffs the satellite-computed
values against the ground-truth manifest a generate_and_play.py run wrote.

Reuses gateway.ingestion.mqtt_subscriber's decode path (node_id_from_topic,
DATA_TOPIC_FILTER) and gateway.common.telemetry_frame.decode_frame() rather
than reimplementing frame parsing — this is the same section-list codec the
gateway itself uses, just consumed standalone (no recv_verify/SQLite/HST
pipeline needed for a bench diff tool).

Extracts the last frame captured for the target channel during the window
(the most settled sample, closest to the end of the capture) rather than
averaging across frames -- deliberately not smoothing over frame-to-frame
variance, since that variance is exactly what this first real run is meant
to characterize before any tolerance band is chosen (see
docs/decisions/ADR-035-bench-signal-gen-laptop-speaker.md).

No pass/fail tolerance is applied -- this prints actual vs. expected with
%delta only. Real capture has windowing/quantization/noise-floor effects the
closed-form manifest math doesn't model; a tolerance band gets calibrated
from real numbers once we have some, not guessed up front.

Usage:
    python capture_and_compare.py --manifest manifests/tone_1000hz_20260808_120000.json \
        --channel mic --host 192.168.1.50 --window-s 8
"""

import argparse
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import gateway.common.telemetry_frame as telemetry_frame
import gateway.common.telemetry_schema as schema
from gateway.ingestion.mqtt_subscriber import DATA_TOPIC_FILTER, node_id_from_topic

_GENERATE_AND_PLAY = Path(__file__).resolve().parent / "generate_and_play.py"

_CSV_FIELDS = [
    "channel", "requested_freq_hz", "requested_level", "peak_bin_freq_hz",
    "peak_bin_db", "noise_floor_db", "snr_db", "locked", "spec_avg_n", "notes",
]

_CHANNEL_SCALAR_SUFFIX = {
    "mic": "mic",
    "accel_x": "x",
    "accel_y": "y",
    "accel_z": "z",
}

# manifest ideal_sine_stats() key -> wire scalar base name (ADR-018: wire
# kurtosis is already excess/Fisher, same convention as kurtosis_excess).
_METRIC_TO_SCALAR_BASE = {
    "rms": "rms",
    "kurtosis_excess": "kurtosis",
    "crest_factor": "crest_factor",
    "skewness": "skewness",
}


def bin_index_to_freq_hz(bin_index, fs, fft_size):
    """Center frequency of wire bin `bin_index`, per telemetry_frame.py's
    documented convention: bin k covers (k*fs/fft_size, (k+1)*fs/fft_size],
    with no DC bin on the wire."""
    return (bin_index + 0.5) * fs / fft_size


def find_peak_bin(spectrum):
    """(freq_hz, magnitude_db) of the strongest bin in a ChannelSpectrum.
    Bins are dBFS (dsp_task.c/imu_task.c), so argmax on the raw values finds
    the same bin argmax on linear magnitude would."""
    bins = spectrum.bins
    peak_idx = max(range(len(bins)), key=lambda i: bins[i])
    return bin_index_to_freq_hz(peak_idx, spectrum.fs, spectrum.fft_size), bins[peak_idx]


def compute_noise_floor_db(bins, exclude_indices):
    """Median (not mean, so a few strong bins can't drag it up) of `bins`
    excluding `exclude_indices`."""
    others = [b for i, b in enumerate(bins) if i not in exclude_indices]
    return statistics.median(others)


def is_locked(spectrum, expected_freq_hz, tolerance_bins=1, min_snr_db=6.0):
    """Objective lock criterion for a captured spectrum against a target
    frequency. Returns (locked: bool, info: dict) where info always has
    peak_bin_freq_hz/peak_bin_db/noise_floor_db/snr_db, suitable for a sweep
    CSV row regardless of which branch below fired.

    When `expected_freq_hz` is below one wire bin's width (e.g. an accel
    target under the 100 Hz/bin resolution), exact bin location is
    meaningless -- per the wire-resolution note, this instead checks for
    elevated energy in the lowest 1-2 bins only (presence, not location)."""
    bins = spectrum.bins
    bin_width_hz = spectrum.fs / spectrum.fft_size
    peak_idx = max(range(len(bins)), key=lambda i: bins[i])
    peak_freq_hz = bin_index_to_freq_hz(peak_idx, spectrum.fs, spectrum.fft_size)
    peak_db = bins[peak_idx]

    if expected_freq_hz < bin_width_hz:
        low_indices = list(range(min(2, len(bins))))
        low_db = max(bins[i] for i in low_indices)
        floor_db = compute_noise_floor_db(bins, set(low_indices))
        snr_db = low_db - floor_db
        locked = snr_db >= min_snr_db
        return locked, {
            "peak_bin_freq_hz": peak_freq_hz,
            "peak_bin_db": low_db,
            "noise_floor_db": floor_db,
            "snr_db": snr_db,
        }

    floor_db = compute_noise_floor_db(bins, {peak_idx})
    snr_db = peak_db - floor_db
    freq_ok = abs(peak_freq_hz - expected_freq_hz) <= tolerance_bins * bin_width_hz
    locked = freq_ok and snr_db >= min_snr_db
    return locked, {
        "peak_bin_freq_hz": peak_freq_hz,
        "peak_bin_db": peak_db,
        "noise_floor_db": floor_db,
        "snr_db": snr_db,
    }


def pct_delta(actual, expected):
    """None when expected is exactly 0 -- %delta is undefined there, callers
    print the absolute delta instead."""
    if expected == 0:
        return None
    return (actual - expected) / expected * 100.0


class FrameCapture:
    """Collects (node_id, DecodedFrame) pairs off `epm/+/data` (or a single
    node's topic) for a fixed window. A thin standalone wrapper around
    paho-mqtt -- not MqttIngestor, which is wired to recv_verify's full
    per-frame pipeline (SQLite, HST, alerting) that this bench tool has no
    use for."""

    def __init__(self, host, port, node_id=None):
        import paho.mqtt.client as mqtt

        self._frames = []
        self._lock = threading.Lock()
        topic = f"epm/{node_id}/data" if node_id else DATA_TOPIC_FILTER
        self._topic = topic
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="epm-bench-capture")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(host, port)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe(self._topic, qos=0)

    def _on_message(self, client, userdata, msg):
        try:
            node_id = node_id_from_topic(msg.topic)
            decoded = telemetry_frame.decode_frame(msg.payload)
        except (IndexError, telemetry_frame.MalformedFrameError):
            return
        with self._lock:
            self._frames.append((node_id, decoded))

    def capture(self, window_s):
        self._client.loop_start()
        time.sleep(window_s)
        self._client.loop_stop()
        self._client.disconnect()
        with self._lock:
            return list(self._frames)


def select_frame(frames, channel, node_id=None):
    """Last captured frame (see module docstring) whose sender matches
    node_id (or the first sender seen, if node_id is None) and that actually
    carries the requested channel. Returns (node_id, DecodedFrame) or None."""
    target_node = node_id
    match = None
    for nid, decoded in frames:
        if target_node is None and channel in decoded.spectra:
            target_node = nid
        if nid == target_node and channel in decoded.spectra:
            match = (nid, decoded)
    return match


def read_actual_scalars(decoded, channel):
    suffix = _CHANNEL_SCALAR_SUFFIX[channel]
    actual = {}
    for manifest_key, scalar_base in _METRIC_TO_SCALAR_BASE.items():
        scalar_id = schema.SCALAR_ID_BY_NAME[f"{scalar_base}_{suffix}"]
        actual[manifest_key] = decoded.scalars.get(scalar_id)
    return actual


def expected_freq_hz(manifest):
    if manifest["mode"] == "tone":
        return manifest["freq_hz"]
    return manifest["sideband_hz"]


def print_report(manifest, actual_scalars, actual_freq_hz, actual_peak_db, n_frames_captured):
    exp_freq = expected_freq_hz(manifest)
    freq_delta = pct_delta(actual_freq_hz, exp_freq)
    print(f"\nCapture: {n_frames_captured} frame(s) received in window")
    print(f"{'metric':<18} {'expected':>14} {'actual':>14} {'% delta':>12}")
    print("-" * 60)
    delta_str = f"{freq_delta:+.3f}%" if freq_delta is not None else "n/a"
    print(f"{'peak_bin_freq_hz':<18} {exp_freq:>14.3f} {actual_freq_hz:>14.3f} {delta_str:>12}")
    print(f"{'peak_bin_db':<18} {'--':>14} {actual_peak_db:>14.3f} {'--':>12}")

    if manifest["mode"] != "tone":
        print("\n(fault mode has no closed-form scalar ground truth -- frequency only)")
        return

    for metric, expected in manifest["expected"].items():
        if metric == "peak":
            continue  # not read from the wire -- see module docstring
        actual = actual_scalars.get(metric)
        if actual is None:
            print(f"{metric:<18} {expected:>14.6f} {'MISSING':>14} {'n/a':>12}")
            continue
        d = pct_delta(actual, expected)
        d_str = f"{d:+.3f}%" if d is not None else f"(abs delta {actual - expected:+.6f})"
        print(f"{metric:<18} {expected:>14.6f} {actual:>14.6f} {d_str:>12}")


def play_tone_background(freq_hz, amplitude, duration_s):
    """Launch generate_and_play.py tone as a subprocess and return it
    without waiting -- lets a sweep point's capture window run concurrently
    with playback (avoids the capture-timing hazard documented in
    docs/performance/BENCH_SIGNAL_GEN_HARDWARE_RUN.md: a capture window
    whose tail extends past when playback actually stopped reads silence as
    a false negative)."""
    cmd = [
        sys.executable, str(_GENERATE_AND_PLAY), "tone",
        "--freq", str(freq_hz), "--duration", str(duration_s),
        "--amplitude", str(amplitude),
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def run_sweep_attempt(channel, freq_hz, amplitude, duration_s, settle_s, capture_window_s,
                       host, port, node_id, tolerance_bins, min_snr_db):
    """One play+capture+lock-check attempt at a single test point. Returns a
    dict with all _CSV_FIELDS values except `notes` (caller fills that in),
    plus `_all_frames_count` for connectivity monitoring."""
    proc = play_tone_background(freq_hz, amplitude, duration_s)
    time.sleep(settle_s)
    capture = FrameCapture(host, port, node_id)
    frames = capture.capture(capture_window_s)
    try:
        proc.wait(timeout=max(duration_s - settle_s - capture_window_s, 0) + 5)
    except subprocess.TimeoutExpired:
        proc.kill()

    row = {
        "channel": channel, "requested_freq_hz": freq_hz, "requested_level": amplitude,
        "peak_bin_freq_hz": "", "peak_bin_db": "", "noise_floor_db": "", "snr_db": "",
        "locked": False, "_all_frames_count": len(frames),
    }
    match = select_frame(frames, channel, node_id)
    if match is None:
        return row
    _, decoded = match
    locked, info = is_locked(decoded.spectra[channel], freq_hz, tolerance_bins, min_snr_db)
    row.update(info)
    row["locked"] = locked
    return row


def append_csv_rows(out_path, rows):
    write_header = not out_path.exists()
    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in _CSV_FIELDS})


def cmd_sweep(args):
    with open(args.points) as f:
        points = json.load(f)

    all_rows = []
    for point in points:
        freq_hz = point["freq_hz"]
        amplitude = point.get("amplitude", 0.8)
        duration_s = point.get("duration_s", args.duration_s)
        repeats = point.get("repeats", args.repeats)
        base_notes = point.get("notes", "")

        locks = 0
        for attempt in range(1, repeats + 1):
            row = run_sweep_attempt(
                args.channel, freq_hz, amplitude, duration_s, args.settle_s, args.capture_window_s,
                args.host, args.port, args.node_id, args.tolerance_bins, args.min_snr_db,
            )
            if row["_all_frames_count"] == 0:
                conn_note = "NO FRAMES IN WINDOW -- possible satellite/WiFi/MQTT disconnect"
            else:
                conn_note = ""
            notes_parts = [p for p in (base_notes, f"attempt {attempt}/{repeats}", conn_note) if p]
            row["notes"] = "; ".join(notes_parts)
            row["spec_avg_n"] = args.spec_avg_n if args.spec_avg_n is not None else ""
            if row["locked"]:
                locks += 1
            print(
                f"[{freq_hz:g} Hz / amp {amplitude:g}] attempt {attempt}/{repeats}: "
                f"locked={row['locked']} snr_db={row.get('snr_db', 'n/a')} "
                f"peak={row.get('peak_bin_freq_hz', 'n/a')}"
                + (f"  ({conn_note})" if conn_note else "")
            )
            all_rows.append(row)

        reliable = locks >= 2 * repeats / 3 if repeats else False
        print(f"  -> {locks}/{repeats} locks, reliable={reliable}\n")

    append_csv_rows(args.out, all_rows)
    print(f"wrote {len(all_rows)} row(s) to {args.out}")
    return 0


def cmd_compare(args):
    with open(args.manifest) as f:
        manifest = json.load(f)

    capture = FrameCapture(args.host, args.port, args.node_id)
    print(f"capturing on {capture._topic} for {args.window_s}s...")
    frames = capture.capture(args.window_s)

    result = select_frame(frames, args.channel, args.node_id)
    if result is None:
        print(f"error: no frames carrying channel {args.channel!r} captured in the window", file=sys.stderr)
        return 1
    node_id, decoded = result

    freq_hz, peak_db = find_peak_bin(decoded.spectra[args.channel])
    actual_scalars = read_actual_scalars(decoded, args.channel) if manifest["mode"] == "tone" else {}

    print(f"using last matching frame from node {node_id!r}")
    print_report(manifest, actual_scalars, freq_hz, peak_db, len(frames))
    return 0


def parse_args(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    # Old flat CLI (no subcommand) stays valid -- default to "compare" so
    # existing invocations (README, muscle memory) keep working unchanged.
    if not argv or argv[0].startswith("-"):
        argv = ["compare"] + list(argv)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    compare = sub.add_parser("compare", help="Single-shot manifest vs. capture comparison (original mode)")
    compare.add_argument("--manifest", type=Path, required=True, help="Ground-truth manifest JSON from generate_and_play.py")
    compare.add_argument("--channel", required=True, choices=sorted(_CHANNEL_SCALAR_SUFFIX), help="Which decoded channel to read")
    compare.add_argument("--host", default="localhost", help="MQTT broker host (default localhost)")
    compare.add_argument("--port", type=int, default=1883, help="MQTT broker port (default 1883)")
    compare.add_argument("--node-id", default=None, help="Only capture this satellite's node_id (default: first sender seen)")
    compare.add_argument("--window-s", type=float, default=8.0, help="Seconds to capture frames for (default 8)")
    compare.set_defaults(func=cmd_compare)

    sweep = sub.add_parser("sweep", help="Drive generate_and_play.py across test points, apply the lock criterion, log to CSV")
    sweep.add_argument("--points", type=Path, required=True, help="JSON file: list of {freq_hz, [amplitude], [duration_s], [repeats], [notes]}")
    sweep.add_argument("--channel", required=True, choices=sorted(_CHANNEL_SCALAR_SUFFIX), help="Which decoded channel to test")
    sweep.add_argument("--host", default="localhost", help="MQTT broker host (default localhost)")
    sweep.add_argument("--port", type=int, default=1883, help="MQTT broker port (default 1883)")
    sweep.add_argument("--node-id", default=None, help="Only capture this satellite's node_id (default: first sender seen)")
    sweep.add_argument("--out", type=Path, required=True, help="CSV path to append rows to (header written once if new)")
    sweep.add_argument("--duration-s", type=float, default=6.0, help="Default tone duration per point in seconds (default 6, overridable per-point)")
    sweep.add_argument("--settle-s", type=float, default=1.0, help="Delay after playback starts before capture begins (default 1s)")
    sweep.add_argument("--capture-window-s", type=float, default=3.0, help="Capture window length; must finish before playback ends (default 3s)")
    sweep.add_argument("--repeats", type=int, default=3, help="Default repeats per point (default 3, overridable per-point)")
    sweep.add_argument("--min-snr-db", type=float, default=6.0, help="Lock SNR threshold in dB (default 6.0)")
    sweep.add_argument("--tolerance-bins", type=int, default=1, help="Bin tolerance for frequency lock (default 1)")
    sweep.add_argument("--spec-avg-n", type=int, default=None, help="Label recorded in the CSV's spec_avg_n column for this run -- informational only, not enforced by this tool (set the firmware build yourself)")
    sweep.set_defaults(func=cmd_sweep)

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
