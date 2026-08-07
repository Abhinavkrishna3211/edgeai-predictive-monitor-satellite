# Base Station Contract — verified live against the reference base-station repository (private — ask the maintainer for access), main branch, 2026-08-04

Supersedes `MASTER_PLAN.md` Part D as the wire-contract reference. Fetched directly via GitHub raw content (no local sibling clone in this session) — see the file list at the bottom for exact sources and verify against a local clone before Phase 5+ if anything here seems to have moved on.

**Methodological note, important for future phases:** `docs/Appendix_B_Wire_Protocol_Specification.md` in his repo is **stale** relative to the actual code — it still documents the old fixed `spectrum_fused_payload` struct and a `[TYPE:1B]` envelope on the data direction. The code itself (`wire_protocol.py`'s own module docstring) says that codec "was removed." `docs/SENSOR_TELEMETRY_FRAME_PLAN.md` is the current, self-aware design doc (status: "Phase A implemented + verified on hardware... T7 (Phase B collapse)... likely never happens — treat Phase A as the durable format"). **Trust the code and SENSOR_TELEMETRY_FRAME_PLAN.md over Appendix B if they ever disagree.**

---

## Confirmed unchanged from Part D's draft

| Item | Confirmed against |
|---|---|
| Transport: MQTT to Mosquitto | `mqtt_subscriber.py`, `mqtt_publisher.py` |
| Publish topic `epm/<node_id>/data`, QoS 0 | `mqtt_subscriber.py`: `DATA_TOPIC_FILTER = "epm/+/data"` |
| Subscribe topic `epm/<node_id>/cmd`, QoS 1 | `mqtt_publisher.py`: `CMD_TOPIC_FMT = "epm/{node_id}/cmd"`, `qos=1` |
| `node_id` = last 6 hex chars of WiFi MAC (3 octets) | Appendix B ("last 6 hex characters"); test fixtures use `"a4cf12"` (6 chars) — matches Phase 0.5's already-confirmed 3-octet convention exactly |
| Frame: `[num_sections u8]` + repeated `[source_id u8][channel_id u8][data_kind u8][section_len u16][body]` | `telemetry_frame.py`'s `_SECTION_HEADER_FMT = "<BBBH"` |
| `source_id`: satellite=1, base_station=0 | `telemetry_schema.json`: `"sources": {"base_station": 0, "satellite": 1}` |
| SPECTRUM body `[fs f32][fft_size u16][bin_count u16][bins f32...]` | `telemetry_frame.py`'s `_SPECTRUM_HEAD_FMT = "<fHH"` |
| SCALAR_SET body `[count u8][ids u16...][values f32...]` | `telemetry_frame.py`'s `encode_scalar_body`/`_decode_scalar_set` |
| Endianness: little-endian | every `struct` format string is `<...` |
| Complete-frame rule | `PipelineManager._validate_frame_bins` (referenced in `mqtt_subscriber.py`'s module docstring) |
| First-frame-commits rule | `mqtt_subscriber_test.py::test_subscriber_routes_to_pipeline_manager_like_spi` — commits `sensor_config`/`input_dim` from frame 1's actual content |
| Zero-fill rule (present + real bin_count + all-zero = real data; `bin_count=0` = channel absent) | `telemetry_frame_test.py::test_zero_fill_present_section_is_real_data` + `::test_bin_count_zero_omits_channel`, both passing |
| Sensor params: mic 48000Hz/2048-pt, accel 6400Hz/1024-pt (nominal), 128 bins | `satellite_node_sim.py`: `NOMINAL_MIC_FS_HZ/FFT_SIZE`, `NOMINAL_ACCEL_FS_HZ/FFT_SIZE`, `DEFAULT_BIN_COUNT` — unchanged since 2026-07-30's check |
| PERF/health scalars ride `channel_id=255` as a `SCALAR_SET` | `telemetry_schema.json`: `"perf_channel_id": 255` |
| Per-channel `input_dim` = 134 (128 spectral bins + 6 scalars) | `registry.py`'s `_DIM_BY_CHANNEL`; matches Phase 0.5's independently-confirmed `input_dim=536` for 4 channels (4×134) |

---

## CORRECTION — kurtosis convention (reverses ADR-014, see new ADR-018)

**Part D drafted excess/Fisher (Gaussian ≈ 0). ADR-014 (Phase 2) chose RAW/Pearson (Gaussian ≈ 3.0) instead, based on evidence from our own side, explicitly flagging this for Phase 4 to re-check against the reference's real source once available.** That check is now done, and it reverses ADR-014:

```python
# base-station/python/common/raw_features.py
def kurtosis(x: np.ndarray) -> float:
    std = x.std()
    if std <= 0:
        return 0.0
    return float(np.mean(((x - x.mean()) / std) ** 4) - 3.0)  # excess kurtosis
```

This is explicit, commented, unambiguous: **the reference computes excess kurtosis** (subtracts 3.0), matching Part D's original draft, not ADR-014's RAW/Pearson choice. `raw_features.py` documents itself as sharing the exact math `fuser.cpp` (the actual on-device scalar computation) uses, so this isn't a display-only or offline-tool-only convention — it's the wire convention.

**Action:** new `docs/decisions/ADR-018-kurtosis-convention-reversed-to-excess.md` supersedes ADR-014 (not deleted, per Part I). `src/threads/mic_task.c` needs a `- 3.0f` added to its kurtosis computation, and `tests/host/test_scalar_stats.c`'s Gaussian expectation needs to flip from ≈3.0 to ≈0.0. **Not done in this phase** (Phase 4 is planning-only) — this is a real code change for a future phase (recommend folding into Phase 6, which already touches the scalar-computation-adjacent fuser/encoder work, or its own quick fix phase if Phase 6 is far off).

---

## New findings, not in Part D's draft at all

1. **Peak convention — signed max, not absolute value:**
   ```python
   def peak(x: np.ndarray) -> float:
       return float(x.max())
   ```
   Confirms Part D's own caution ("his satellite historically used signed max, known failure mode on negative-going impacts") — this is base-station-side Python evidence, documented as matching `fuser.cpp`'s on-device computation, not independently verified against satellite firmware C++ directly (no sibling clone this session to read `satellite/`'s own scalar_stats). **Decision still needed, own ADR**, whichever phase next touches our peak computation: match his signed-max (simpler, inherits his known failure mode) or diverge and use `abs(x).max()` (fixes the failure mode, breaks bit-for-bit parity with his values). Flag for whoever owns this — not decided here.

2. **Skewness scalar exists on the wire, we likely don't compute it.** `telemetry_schema.json`'s scalar set is 6 per group, not the fewer implied by earlier drafts: `rms, kurtosis, crest_factor, peak, std, skewness` — global (ids 1–6, unsuffixed) and per-channel-suffixed (`_x/_y/_z/_mic`, ids 7–30). Our own Phase 1 test findings only confirmed `mic_task.c` computes RMS/crest/kurtosis (3 of 6). **Gap, not urgent** — our current frames don't need to carry every scalar the schema knows about (schema is a superset both sides draw from), but if full parity/completeness matters later, `crest_factor`/`peak`/`std`/`skewness` need adding to our scalar computation. Formula (for whenever this is picked up): `skewness(x) = mean(((x - mean(x))/std(x))**3)`.

3. **Global (unsuffixed) scalar ids 1–6 are legacy/base-station-only, not something a satellite needs to send.** `satellite_node_sim.py` (the actual satellite stand-in) only ever computes the per-channel suffixed set — confirms our own golden frame's ids 7–30 (Phase 0 finding) is correct as-is, nothing to add there.

4. **The combined `accel` channel (`channel_id=1`) is real, still on the wire, but explicitly demoted to display-only** — `registry.py`'s `SensorChannel` enum is `{MIC, ACCEL_X, ACCEL_Y, ACCEL_Z}` only; a combined-accel section lands in `SensorFrame.display_bins`, not `.bins` (`mqtt_subscriber_test.py::test_combined_accel_channel_lands_in_display_bins_not_bins`). **Confirms our per-axis-only approach (Part B/D.1) is correct and current** — no combined channel needed from our satellite.

5. **New topic, not applicable to us:** `epm/<node_id>/outputs` (QoS 1, JSON, not binary) — a rig host's retained self-description for machinery-protection trip outputs (`mqtt_subscriber.py`'s `OUTPUTS_TOPIC_FILTER`). This is for a base-station-attached rig with a motor to stop, not a sensing satellite. No action needed, just noting it exists so it isn't mistaken for something we're missing.

6. **New command type, not applicable to us:** `MqttMsgType.MOTOR_STOP = 0x09` alongside `STATUS_LED = 0x08` — same machinery-protection feature. A satellite's `cmd` handler should keep ignoring unrecognized TYPE bytes (already true per Phase 0.5's spec) rather than needing to implement this — just avoid `0x09` if we ever define our own additional command type later.

---

## Exact ID tables (for Phase 6's encoder, copy verbatim — do not re-derive)

**Channels** (`telemetry_schema.json`):
| name | id | kind | Relevant to our satellite? |
|---|---|---|---|
| mic | 0 | SPECTRUM | yes |
| accel | 1 | SPECTRUM | no — legacy/display-only, per finding 4 above |
| accel_x_raw..mic_raw | 2–5 | TIME_SERIES | no — his raw-capture debug mode only |
| accel_x | 6 | SPECTRUM | yes |
| accel_y | 7 | SPECTRUM | yes |
| accel_z | 8 | SPECTRUM | yes |

**Scalars** (id, per-channel suffix pattern `_x/_y/_z/_mic`): rms=1/7/13/19/25, kurtosis=2/8/14/20/26, crest_factor=3/9/15/21/27, peak=4/10/16/22/28, std=5/11/17/23/29, skewness=6/12/18/24/30 (first id = unsuffixed/global, not needed by us per finding 3).

---

## Sources fetched (raw.githubusercontent.com, reference base-station repository, main branch)
- `base-station/python/ingestion/mqtt_subscriber.py`
- `base-station/python/ingestion/mqtt_publisher.py`
- `base-station/python/common/telemetry_frame.py`
- `base-station/python/common/wire_protocol.py`
- `base-station/python/common/raw_features.py`
- `base-station/python/registry/registry.py`
- `base-station/telemetry_schema.json`
- `base-station/python/tools/satellite_node_sim.py`
- `base-station/tests/telemetry_frame_test.py`
- `base-station/tests/mqtt_subscriber_test.py`
- `docs/Appendix_B_Wire_Protocol_Specification.md` (stale, see methodological note above)
- `docs/SENSOR_TELEMETRY_FRAME_PLAN.md` (current, authoritative design doc)
