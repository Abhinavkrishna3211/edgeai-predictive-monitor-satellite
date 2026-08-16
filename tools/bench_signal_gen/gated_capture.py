#!/usr/bin/env python3
"""Waits for the link to be confirmed live (steady message flow) before
triggering playback, using the SAME already-connected MQTT client for both
the gating check and the actual capture, so there's no reconnect gap between
"confirmed live" and "start capturing" that could itself land in a stall.

v2 fix (2026-08-16): generate_and_play.py's cmd_tone() calls play_samples()
(which blocks for the full --duration via sd.wait()) BEFORE writing the
manifest and printing "wrote manifest" -- so that line only appears AFTER
playback has already finished. v1 of this script waited for that line before
opening its "official" capture window, which meant the window opened right
as the tone ended and only ever saw post-tone silence (confirmed against real
hardware: peak_bin_freq_hz landed near 0Hz / noise floor every time despite
per-frame timelines showing a rock-solid ~1000Hz lock for the ~29s during the
readline() wait). Fix: capture continuously for the subprocess's entire
lifetime (frames.append already happens in the paho loop thread regardless of
what we read from stdout), then pick the analysis frame from just before the
subprocess exited -- not the temporally last frame in an arbitrarily-timed
post-hoc window."""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import paho.mqtt.client as mqtt
import gateway.common.telemetry_frame as telemetry_frame
from gateway.ingestion.mqtt_subscriber import DATA_TOPIC_FILTER, node_id_from_topic

import capture_and_compare as cc

FREQ = float(sys.argv[1])
PLAY_DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
TRAILING_CAPTURE_S = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
GATE_TIMEOUT = float(sys.argv[4]) if len(sys.argv) > 4 else 180.0
HOST = "192.168.1.5"

frames = []


def on_connect(c, u, f, rc, p=None):
    c.subscribe(DATA_TOPIC_FILTER, qos=0)


def on_message(c, u, msg):
    try:
        node_id = node_id_from_topic(msg.topic)
        decoded = telemetry_frame.decode_frame(msg.payload)
    except (IndexError, telemetry_frame.MalformedFrameError):
        return
    frames.append((time.time(), node_id, decoded))


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="epm-bench-gated")
client.on_connect = on_connect
client.on_message = on_message
client.connect(HOST, 1883, keepalive=10)
client.loop_start()

print(f"gating: waiting up to {GATE_TIMEOUT}s for the link to show steady live data...")
gate_start = time.time()
recent_window_s = 3.0
required_in_window = 8
gated_ok = False
while time.time() - gate_start < GATE_TIMEOUT:
    time.sleep(1.0)
    now = time.time()
    recent = [f for f in frames if now - f[0] <= recent_window_s]
    elapsed = now - gate_start
    print(f"  t={elapsed:>5.1f}s  recent_msgs({recent_window_s:.0f}s window)={len(recent)}  total={len(frames)}")
    if len(recent) >= required_in_window:
        gated_ok = True
        break

if not gated_ok:
    print(f"\nGATE FAILED: link never showed steady live data within {GATE_TIMEOUT}s.")
    client.loop_stop()
    client.disconnect()
    sys.exit(1)

print(f"\nlink confirmed live ({len(recent)} msgs in last {recent_window_s:.0f}s) -- starting playback immediately")
frames.clear()

gen_script = Path(__file__).resolve().parent / "generate_and_play.py"
play_start = time.time()
proc = subprocess.Popen(
    [sys.executable, "-u", str(gen_script), "tone", "--freq", str(FREQ),
     "--duration", str(PLAY_DURATION), "--amplitude", "0.9"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
)

print(f"playback subprocess launched (blocks for ~{PLAY_DURATION}s then writes manifest) -- capturing throughout...")
try:
    stdout_data, _ = proc.communicate(timeout=PLAY_DURATION + 20)
except subprocess.TimeoutExpired:
    proc.kill()
    stdout_data, _ = proc.communicate()
play_end = time.time()
print(f"playback subprocess exited after {play_end - play_start:.1f}s")

manifest_path = None
for line in stdout_data.splitlines():
    if "wrote manifest" in line:
        manifest_path = line.strip().split("wrote manifest", 1)[1].strip()
        break

if manifest_path is None:
    print("FATAL: never saw 'wrote manifest' in subprocess output:")
    print(stdout_data)
    client.loop_stop()
    client.disconnect()
    sys.exit(1)

print(f"manifest: {manifest_path}")

print(f"capturing {TRAILING_CAPTURE_S:.1f}s more to see the post-tone transition...")
time.sleep(TRAILING_CAPTURE_S)

client.loop_stop()
client.disconnect()

captured = list(frames)
print(f"\ntotal frames captured: {len(captured)}")

if not captured:
    print("\nNO FRAMES AT ALL during playback -- link stalled again mid-capture.")
    sys.exit(1)

print("\nper-frame mic peak-bin timeline (idx, t_rel_to_play_start_s, node, peak_hz, peak_db):")
for i, (ts, nid, decoded) in enumerate(captured):
    marker = "  <-- proc exited here" if abs(ts - play_end) < 0.25 else ""
    if "mic" in decoded.spectra:
        freq_hz, db = cc.find_peak_bin(decoded.spectra["mic"])
        print(f"  [{i:>3}] t={ts - play_start:>6.1f}s node={nid} peak={freq_hz:>9.3f}Hz  {db:>8.3f}dB{marker}")
    else:
        print(f"  [{i:>3}] t={ts - play_start:>6.1f}s node={nid} (no mic channel){marker}")

with open(manifest_path) as f:
    manifest = json.load(f)

# Pick the last frame captured while the subprocess was still alive (i.e.
# timestamp <= play_end) -- this is "during real playback, near the end",
# matching what capture_and_compare.py normally gets when its window is
# timed to close right as playback ends. Falling back to the true last frame
# only if somehow nothing qualifies.
during_play = [(nid, decoded) for ts, nid, decoded in captured if ts <= play_end]
frames_for_select = during_play if during_play else [(nid, decoded) for _, nid, decoded in captured]
match = cc.select_frame(frames_for_select, "mic", None)
if match is None:
    print("\nerror: no frame carrying 'mic' channel during the playback window")
    sys.exit(1)
node_id, decoded = match
freq_hz, peak_db = cc.find_peak_bin(decoded.spectra["mic"])
actual_scalars = cc.read_actual_scalars(decoded, "mic")
print(f"\n=== standard compare (last in-playback frame, node={node_id!r}) ===")
cc.print_report(manifest, actual_scalars, freq_hz, peak_db, None, len(captured))
