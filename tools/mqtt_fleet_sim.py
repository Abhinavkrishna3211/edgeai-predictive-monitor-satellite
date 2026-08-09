#!/usr/bin/env python3
"""
mqtt_fleet_sim.py — Simulates N EPM satellites publishing MQTT telemetry, for
network-load testing against a real Mosquitto broker.

tools/satellite_sim.py speaks the old raw TCP+AES protocol (port 5100,
epm_protocol.h) that the firmware no longer implements (removed per
docs/decisions/ADR-023-transport-adrs-superseded.md, replaced by MQTT per
ADR-011). This is the MQTT-native replacement referenced by
gateway/common/telemetry_frame.py's "used by tests, and by a future
satellite_sim.py MQTT port" comment: it publishes real section-list frames
(gateway.common.telemetry_frame) to epm/<node_id>/data on the same broker
and topic filter (epm/+/data) the real satellite and gateway use, so it
genuinely exercises the broker + network path a real fleet would.

Each simulated node publishes a frame shaped like the real firmware's (see
src/epm_config.h EPM_NET_FRAME_BUF_BYTES comment): 7 SPECTRUM sections
(mic, accel x/y/z raw, accel x/y/z envelope) at EPM_MODEL_SPECTRUM_BINS bins
each, plus 1 SCALAR_SET section, at EPM_NET_PUBLISH_INTERVAL_MS cadence.

Usage (run from the repo root):
  python tools/mqtt_fleet_sim.py --host 192.168.1.5 --port 1883 --nodes 8
  python tools/mqtt_fleet_sim.py --nodes 20 --interval-ms 200 --duration-s 3600
"""
import argparse
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paho.mqtt.client as mqtt

from gateway.common.telemetry_frame import (
    encode_frame, encode_section, encode_spectrum_body, encode_scalar_body,
)
from gateway.common.telemetry_schema import (
    SOURCE_ID, DATA_KIND, CHANNEL_ID_BY_NAME, SCALAR_ID_BY_NAME,
)

# Matches src/epm_config.h defaults.
SPECTRUM_BINS = 128
MIC_FS_HZ = 48000
IMU_FS_HZ = 25600
PUBLISH_INTERVAL_MS = 200

SPECTRUM_CHANNELS = [
    ("mic", MIC_FS_HZ, 256),
    ("accel_x_raw", IMU_FS_HZ, 512),
    ("accel_y_raw", IMU_FS_HZ, 512),
    ("accel_z_raw", IMU_FS_HZ, 512),
    ("accel_x_envelope", IMU_FS_HZ, 256),
    ("accel_y_envelope", IMU_FS_HZ, 256),
    ("accel_z_envelope", IMU_FS_HZ, 256),
]

SCALAR_NAMES = [
    "rms", "kurtosis", "crest_factor", "peak", "std", "skewness",
    "rms_x", "kurtosis_x", "crest_factor_x", "peak_x", "std_x", "skewness_x",
    "rms_y", "kurtosis_y", "crest_factor_y", "peak_y", "std_y", "skewness_y",
    "rms_z", "kurtosis_z", "crest_factor_z", "peak_z", "std_z", "skewness_z",
]


def build_frame(rng: random.Random) -> bytes:
    """One section-list frame shaped like the real firmware's (see module
    docstring): 7 SPECTRUM sections + 1 SCALAR_SET, random-but-plausible
    values (payload content doesn't matter for a network/broker load test,
    only realistic shape/size does)."""
    sections = []
    for name, fs, fft_size in SPECTRUM_CHANNELS:
        bins = tuple(rng.uniform(-120.0, -20.0) for _ in range(SPECTRUM_BINS))
        body = encode_spectrum_body(fs, fft_size, bins)
        sections.append(encode_section(SOURCE_ID["satellite"], CHANNEL_ID_BY_NAME[name],
                                        DATA_KIND["SPECTRUM"], body))

    scalars = {SCALAR_ID_BY_NAME[name]: rng.uniform(0.0, 10.0) for name in SCALAR_NAMES}
    sections.append(encode_section(SOURCE_ID["satellite"], 0, DATA_KIND["SCALAR_SET"],
                                    encode_scalar_body(scalars)))

    return encode_frame(sections)


class SimNode:
    def __init__(self, node_id: str, host: str, port: int, interval_ms: int):
        self.node_id = node_id
        self.topic = f"epm/{node_id}/data"
        self.interval_s = interval_ms / 1000.0
        self.rng = random.Random(node_id)
        self.publishes = 0
        self.publish_failures = 0
        self._stop = threading.Event()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=node_id)
        self.client.on_disconnect = self._on_disconnect
        self.disconnects = 0
        self.host = host
        self.port = port

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.disconnects += 1

    def start(self):
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        next_tick = time.monotonic()
        while not self._stop.is_set():
            payload = build_frame(self.rng)
            info = self.client.publish(self.topic, payload, qos=0)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                self.publish_failures += 1
            else:
                self.publishes += 1

            next_tick += self.interval_s
            delay = next_tick - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.monotonic()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.client.loop_stop()
        self.client.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("EPM_MQTT_BROKER_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("EPM_MQTT_BROKER_PORT", "1883")))
    ap.add_argument("--nodes", type=int, default=5, help="number of simulated satellites")
    ap.add_argument("--interval-ms", type=int, default=PUBLISH_INTERVAL_MS,
                     help="publish interval per node, matches EPM_NET_PUBLISH_INTERVAL_MS")
    ap.add_argument("--duration-s", type=float, default=0,
                     help="run for this long then exit (0 = run until Ctrl+C)")
    ap.add_argument("--node-prefix", default="sim", help="node_id = <prefix><NNN>, 6 chars total")
    ap.add_argument("--report-interval-s", type=float, default=60.0)
    args = ap.parse_args()

    nodes = []
    for i in range(args.nodes):
        node_id = f"{args.node_prefix}{i:03d}"
        if len(node_id) != 6:
            sys.exit(f"node_id '{node_id}' must be 6 chars (NODE_ID_LEN) — "
                     f"shorten --node-prefix or use < 1000 nodes")
        nodes.append(SimNode(node_id, args.host, args.port, args.interval_ms))

    print(f"[mqtt_fleet_sim] connecting {len(nodes)} simulated satellites to "
          f"{args.host}:{args.port}, publishing every {args.interval_ms} ms each "
          f"({1000.0 / args.interval_ms * len(nodes):.1f} frames/s aggregate)")

    for n in nodes:
        n.start()

    start = time.monotonic()
    last_report = start
    try:
        while args.duration_s <= 0 or (time.monotonic() - start) < args.duration_s:
            time.sleep(1)
            now = time.monotonic()
            if now - last_report >= args.report_interval_s:
                last_report = now
                total_pub = sum(n.publishes for n in nodes)
                total_fail = sum(n.publish_failures for n in nodes)
                total_disc = sum(n.disconnects for n in nodes)
                elapsed = now - start
                print(f"[mqtt_fleet_sim] t={elapsed:.0f}s publishes={total_pub} "
                      f"failures={total_fail} disconnects={total_disc}")
    except KeyboardInterrupt:
        print("\n[mqtt_fleet_sim] stopping (Ctrl+C)...")

    for n in nodes:
        n.stop()

    total_pub = sum(n.publishes for n in nodes)
    total_fail = sum(n.publish_failures for n in nodes)
    total_disc = sum(n.disconnects for n in nodes)
    print(f"[mqtt_fleet_sim] done: publishes={total_pub} failures={total_fail} "
          f"disconnects={total_disc}")


if __name__ == "__main__":
    main()
