"""gateway/ingestion/mqtt_subscriber.py — MQTT ingestion adapter for the
section-list wire format (Phase 8a, Task 4; moved from mic_tools/mqtt_ingest.py
in Phase 8b3 task 1 — relocation + the _open_csv_for_node fix below, no other
behavior change).

Subscribes to `epm/+/data` over paho-mqtt (QoS 0, matching the firmware's
publish side), decodes each message with `telemetry_frame.decode_frame()`,
adapts the result into the exact frame-dict shape `recv_verify.parse_frame()`
(now `gateway.ingestion.tcp_legacy.parse_frame()`) already produces, and feeds
it into `recv_verify._process_satellite_frame()` -- the same per-frame
pipeline (alerting, HST/RUL/fusion, CSV logging, dashboard state) the TCP+AES
path uses. See PHASE_8A_PROMPT.md Tasks 2/4.

Modeled on the reference base-station implementation @
base-station/python/ingestion/mqtt_subscriber.py (paho v2 callback API,
connect_async()+loop_start(), node_id-from-topic) -- fetched live rather than
guessed, since paho-mqtt 2.x's Client() signature is a breaking change from 1.x.

Deliberate deviations from the TCP wire format, spelled out rather than
silently guessed (see PHASE_8A_PROMPT.md Task 4):

  Satellite identity: node_id (from the topic, `epm/<node_id>/data`) is used
  directly as the satellite registry key -- there is no hello-equivalent on
  this transport to recover a real MAC, firmware version, or device name.
  See docs/decisions/ADR-027-mqtt-synthetic-satellite-identity.md.

  imu_rms/imu_crest: the wire format has no combined tri-axial scalar -- the
  old TCP HEADER_FMT's imu_rms/imu_crest were computed on-device for that
  transport only (tcp_task.c, deleted Phase 7a). This adapter derives them
  from the three per-axis scalars the firmware does send (rms_x/y/z,
  crest_factor_x/y/z):
    imu_rms   = sqrt(rms_x^2 + rms_y^2 + rms_z^2)   -- vector-sum vibration energy
    imu_crest = max(crest_x, crest_y, crest_z)      -- worst-axis impulsiveness
  matching how compute_alert()/_classify_fault_type() already treat imu_crest
  as a single worst-case signal (`max(mic_crest, imu_crest)`, and imu_crest's
  own comparisons against CREST_WARN/CREST_FAULT) -- not an average, which
  would dilute a fault that shows up strongly on only one axis (e.g. the
  "Shaft Misalignment" case in _classify_fault_type).

  frame_id/ts_ms: the wire format carries neither. A per-node running counter
  stands in for frame_id (replay protection in _process_satellite_frame only
  needs monotonic increase within one node's stream, which a local counter
  trivially satisfies); ts_ms uses local receipt time in place of a
  device-clock timestamp.

  overflow_count: no wire equivalent (no I2S-overflow scalar in
  telemetry_schema.SCALAR_ID_BY_NAME) -- always 0 for MQTT-sourced frames.

`_open_csv_for_node()` resolves its logs/ directory from the `rv_module`
passed into `MqttIngestor` (`rv_module._BASE_DIR`, i.e. recv_verify.py's own
directory) rather than this file's own `__file__` — this module now lives in
gateway/ingestion/, whose directory is not where mic_tools/logs/ belongs;
a bare `__file__`-relative path here (as the pre-move version used, back when
this file and recv_verify.py were siblings in mic_tools/) would silently
write logs under gateway/ingestion/logs/ instead.
"""
import csv
import datetime
import logging
import math
import os
import threading
import time

import numpy as np
import paho.mqtt.client as mqtt

import gateway.common.telemetry_frame as telemetry_frame
import gateway.common.telemetry_schema as schema

log = logging.getLogger("mqtt_ingest")

DATA_TOPIC_FILTER = "epm/+/data"
QOS_DATA = 0

_SCALAR_TO_FLAT = {
    schema.SCALAR_ID_BY_NAME["rms_mic"]:          "mic_rms",
    schema.SCALAR_ID_BY_NAME["crest_factor_mic"]: "mic_crest",
    schema.SCALAR_ID_BY_NAME["kurtosis_mic"]:      "mic_kurtosis",
}
_AXIS_RMS_IDS   = [schema.SCALAR_ID_BY_NAME[f"rms_{a}"]          for a in ("x", "y", "z")]
_AXIS_CREST_IDS = [schema.SCALAR_ID_BY_NAME[f"crest_factor_{a}"] for a in ("x", "y", "z")]


def node_id_from_topic(topic: str) -> str:
    """`epm/<node_id>/data` -> node_id. Raises IndexError on a malformed topic."""
    return topic.split("/")[1]


def decoded_to_frame_dict(decoded: "telemetry_frame.DecodedFrame",
                          frame_id: int, ts_ms: int) -> dict:
    """Adapt a DecodedFrame into the frame-dict shape parse_frame() produces.
    See module docstring for the imu_rms/imu_crest/frame_id/overflow_count
    deviations from the TCP wire format."""
    mic_fft = np.asarray(decoded.bins.get('mic', ()), dtype=np.float32)
    imu_x   = np.asarray(decoded.bins.get('accel_x', ()), dtype=np.float32)
    imu_y   = np.asarray(decoded.bins.get('accel_y', ()), dtype=np.float32)
    imu_z   = np.asarray(decoded.bins.get('accel_z', ()), dtype=np.float32)

    env_x_spec = decoded.spectra.get('accel_x_envelope')
    env_y_spec = decoded.spectra.get('accel_y_envelope')
    env_z_spec = decoded.spectra.get('accel_z_envelope')
    imu_env_x = np.asarray(env_x_spec.bins if env_x_spec else (), dtype=np.float32)
    imu_env_y = np.asarray(env_y_spec.bins if env_y_spec else (), dtype=np.float32)
    imu_env_z = np.asarray(env_z_spec.bins if env_z_spec else (), dtype=np.float32)
    # All three envelope channels share one decimated fs (one IMU_ENVELOPE_DECIM
    # for every axis, per net_task.c) -- read it off whichever section arrived
    # rather than assuming the 3200 Hz design value.
    imu_env_fs = next((s.fs for s in (env_x_spec, env_y_spec, env_z_spec) if s is not None), 0.0)

    frame = {flat: decoded.scalars[sid]
             for sid, flat in _SCALAR_TO_FLAT.items() if sid in decoded.scalars}
    frame.setdefault('mic_rms', 0.0)
    frame.setdefault('mic_crest', 0.0)
    frame.setdefault('mic_kurtosis', 0.0)

    rms_axes   = [decoded.scalars[i] for i in _AXIS_RMS_IDS   if i in decoded.scalars]
    crest_axes = [decoded.scalars[i] for i in _AXIS_CREST_IDS if i in decoded.scalars]
    imu_rms    = math.sqrt(sum(v * v for v in rms_axes)) if rms_axes else 0.0
    imu_crest  = max(crest_axes) if crest_axes else 0.0

    frame.update(dict(
        frame_id=frame_id, ts_ms=ts_ms,
        mic_bins=len(mic_fft), imu_bins=len(imu_x), imu_axes=3,
        imu_rms=imu_rms, imu_crest=imu_crest,
        mic_fft=mic_fft, imu_x=imu_x, imu_y=imu_y, imu_z=imu_z,
        imu_env_x=imu_env_x, imu_env_y=imu_env_y, imu_env_z=imu_env_z,
        imu_env_fs=imu_env_fs,
        overflow_count=0,
        errors=[],
    ))
    return frame


def _open_csv_for_node(node_id: str, rv_module):
    """Mirrors satellite_thread's CSV setup (gateway/ingestion/tcp_legacy.py)
    for a synthetic `sat-<node_id>` name -- one file per node, appended across
    process restarts, dated subdirectory layout for the existing rotation job."""
    name     = f"sat-{node_id}"
    log_dir  = os.path.join(rv_module._BASE_DIR, 'logs')
    now_dt   = datetime.datetime.now()
    csv_dir  = os.path.join(log_dir, 'csv', now_dt.strftime('%Y'), now_dt.strftime('%m'))
    os.makedirs(csv_dir, exist_ok=True)
    date_str = now_dt.strftime('%Y%m%d')
    csv_path = os.path.join(csv_dir, f"epm_{name}_{date_str}.csv")
    is_new   = not os.path.exists(csv_path)
    csv_f    = open(csv_path, 'a', newline='')
    csv_w    = csv.writer(csv_f)
    if is_new:
        csv_w.writerow(['wall_time', 'frame_id', 'device_ms',
                        'mic_rms', 'mic_crest', 'mic_kurtosis',
                        'imu_rms', 'imu_crest',
                        'high_band_ratio', 'z_score', 'p_fault', 'alert',
                        'overflow_count'])
    return csv_w, csv_f


class _NodeState:
    """Per-node_id state the MQTT path needs that the TCP path instead gets
    from its per-connection thread locals + hello handshake: a running
    frame_id counter (see module docstring), and the streak/alert state
    _process_satellite_frame() threads through call to call."""
    __slots__ = ('frame_counter', 'warn_streak', 'ok_streak',
                 'sent_alert', 'last_frame_id', 'csv_w', 'csv_f')

    def __init__(self, csv_w, csv_f, ok_alert_const):
        self.frame_counter = 0
        self.warn_streak    = 0
        self.ok_streak      = 0
        self.sent_alert     = ok_alert_const
        self.last_frame_id  = -1
        self.csv_w = csv_w
        self.csv_f = csv_f


class MqttIngestor:
    """Owns one paho-mqtt client subscribed to epm/+/data, decoding and
    routing every message into recv_verify's existing per-frame pipeline.
    `rv` is the recv_verify module, passed in rather than imported at module
    level to keep this file importable/testable on its own."""

    def __init__(self, rv_module, host: str, port: int = 1883,
                client_id: str = "epm-gateway-mqtt"):
        self._rv    = rv_module
        self._host  = host
        self._port  = port
        self._nodes: dict = {}
        self._nodes_lock = threading.Lock()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def start(self):
        """Non-blocking: connect_async() defers the TCP attempt to
        loop_start()'s background thread, which also dispatches on_message."""
        self._client.connect_async(self._host, self._port)
        self._client.loop_start()
        log.info("MQTT ingestor starting -- broker %s:%s, topic %s",
                 self._host, self._port, DATA_TOPIC_FILTER)

    def stop(self):
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = (reason_code == 0) or (getattr(reason_code, "value", 1) == 0)
        if ok:
            client.subscribe(DATA_TOPIC_FILTER, qos=QOS_DATA)
            log.info("MQTT connected to %s:%s, subscribed %s",
                     self._host, self._port, DATA_TOPIC_FILTER)
        else:
            log.error("MQTT connect failed: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            node_id = node_id_from_topic(msg.topic)
        except IndexError:
            log.warning("MQTT message on malformed topic %r -- dropped", msg.topic)
            return

        try:
            decoded = telemetry_frame.decode_frame(msg.payload)
        except telemetry_frame.MalformedFrameError as exc:
            log.warning("[%s] malformed MQTT frame (%d B): %s",
                       node_id, len(msg.payload), exc)
            return

        rv = self._rv
        with self._nodes_lock:
            node = self._nodes.get(node_id)
            if node is None:
                sat = rv._sat_register(node_id, f"sat-{node_id}", None, None,
                                       (self._host, self._port))
                csv_w, csv_f = _open_csv_for_node(node_id, rv)
                node = _NodeState(csv_w, csv_f, rv.EPM_ALERT_OK)
                self._nodes[node_id] = node
                log.info("Satellite registered (MQTT): %s  node_id=%s", sat.name, node_id)
            else:
                sat = rv._satellites[node_id]

        node.frame_counter += 1
        frame = decoded_to_frame_dict(decoded, node.frame_counter,
                                      int(time.time() * 1000))

        result = rv._process_satellite_frame(
            sat, frame, node_id, node.csv_w, node.csv_f,
            node.warn_streak, node.ok_streak, node.sent_alert, node.last_frame_id)
        if result is None:
            return   # replay -- already logged inside _process_satellite_frame
        (_alert, _p_fault, _now,
         node.warn_streak, node.ok_streak, node.sent_alert,
         node.last_frame_id) = result


def start(rv_module, host: str, port: int = 1883) -> MqttIngestor:
    """Entry point for recv_verify.main()'s --mqtt-host wiring."""
    ingestor = MqttIngestor(rv_module, host, port)
    ingestor.start()
    return ingestor
