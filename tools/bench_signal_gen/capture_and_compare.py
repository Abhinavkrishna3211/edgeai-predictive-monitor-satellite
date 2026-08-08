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
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import gateway.common.telemetry_frame as telemetry_frame
import gateway.common.telemetry_schema as schema
from gateway.ingestion.mqtt_subscriber import DATA_TOPIC_FILTER, node_id_from_topic

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


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="Ground-truth manifest JSON from generate_and_play.py")
    parser.add_argument("--channel", required=True, choices=sorted(_CHANNEL_SCALAR_SUFFIX), help="Which decoded channel to read")
    parser.add_argument("--host", default="localhost", help="MQTT broker host (default localhost)")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port (default 1883)")
    parser.add_argument("--node-id", default=None, help="Only capture this satellite's node_id (default: first sender seen)")
    parser.add_argument("--window-s", type=float, default=8.0, help="Seconds to capture frames for (default 8)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
