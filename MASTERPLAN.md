# EPM MASTERPLAN — Overnight Read-Only Audit

**Date:** 2026-07-04 · **Mode:** read-only static review + test run (no source edits, no flashing)
**Scope:** full firmware (`src/`, `components/mic_capture/`), protocol, gateway (`mic_tools/`),
config, docs, git hygiene.

> **Read this first.** Connectivity now works end-to-end for the first time
> (PROJECT_STATUS Session 7 RESOLVED). But a live test degraded after ~9 minutes
> into a permanent `connect()` failure (errno 119) as internal heap fell from
> **21140 → 2896 bytes** and never recovered. Treat connectivity as **"works until
> internal heap exhausts, then fails identically to the original multi-day bug,"**
> not as solved. This is the #1 action item.

---

## 0. Headline correction — the Phase 0 premise is stale

The audit request asked to trace a heap leak through `resolve_gateway_mdns()` /
`mdns_query_results_free()`. **That code no longer exists.** The working-tree
`wifi_task.c` (uncommitted, **+119 / −64 vs HEAD**) has mDNS **fully removed** — see
[wifi_task.c:161-166](src/wifi_task.c#L161-L166): *"mDNS discovery removed:
mdns_query_ptr() uses 5-8 KB of stack … Static SERVER_IP is always available."* The
committed HEAD (`bd1781f`, `e59826b`) still had mDNS; the local overhaul stripped it.

**Consequence:** the mDNS free-path leak hypothesis is moot, and the remaining
reconnect path shows **no statically visible leak** (§Phase 0). The real drain must be
localized at runtime. This divergence between the request's mental model and the
actual code is the single most important finding.

---

## 1. Solid-base checklist

| Area | Solid? | Basis |
|---|:--:|---|
| Gateway test suite | ✅ | **105 passed in 192 s** (`pytest -q`, this session) |
| Protocol framing (header/hello/v2) | ✅ | 48/24/8-byte `_Static_assert`s hold; gateway parse matches byte-for-byte |
| Gateway input robustness | ✅ | bounded `payload_bytes`, GCM `InvalidTag`→SECURITY, monotonic `frame_id` replay guard |
| PSRAM / memory placement | ✅ | SPIRAM enabled in active config; `EXT_RAM_BSS_ATTR` restored; DMA buffers correctly pinned to internal DRAM |
| Firmware concurrency | ✅ (1 minor) | single-producer/consumer queues; one best-effort snapshot torn-read |
| **Connectivity** | ⚠️ **NO** | works until internal heap exhausts → identical failure returns. Leak **not** root-caused (read-only; needs runtime soak) |
| Doc / comment accuracy | ⚠️ NO | several comments contradict the current (post-Session-7) config |
| Security (key management) | ⚠️ pre-deploy | build-time PSK, not NVS-provisioned (history is clean) |
| IMU data path | ⚠️ stub | `imu_task.c` is synthetic; real KX134 not integrated |

**Confirmed via static review:** all ✅ rows, all doc/comment findings, git hygiene,
protocol, memory placement.
**Needs runtime soak to confirm:** the heap drain (Phase 0), reconnect-frequency
coupling (Phase 4).

---

## 2. Findings by phase

Severity: **BLOCKER** (must fix before deploy) · **HIGH** · **MEDIUM** · **LOW**.

### Phase 0 — Heap drain · **HIGH** · needs-runtime-confirm

**Static verdict: the current reconnect path is clean — there is no app-level leak to
point at.**

- `tcp_connect()` [wifi_task.c:222](src/wifi_task.c#L222) `close()`s the socket on
  **every** failure path: immediate refuse (L280), `select()` timeout (L294),
  `SO_ERROR` set (L305).
- `connect_to_gateway()` closes on hello failure [wifi_task.c:418](src/wifi_task.c#L418);
  `drop_connection()` closes [wifi_task.c:426](src/wifi_task.c#L426).
- `send_frame()` / `encrypt_frame_data()` use only static `DMA_ATTR` buffers
  (`s_enc_pt`, `s_enc_ct`); `encrypt_init()` runs **once** before the loop.
- **No `malloc` / `heap_caps_*` anywhere in the reconnect loop.** No mDNS.

**Arithmetic flag (this reframes the diagnosis):** 18 KB over 9 min. At ~2 fps that is
~1080 frames → **~17 B/frame**; if it were per-reconnect (~6–9 disconnects at 60–90 s)
it would need ~120–180 cycles, which does **not** match the observed disconnect
cadence. The drain is therefore more likely **per-frame or fragmentation**, not the
per-reconnect path the request assumed.

**Suspects (cannot be confirmed read-only):**
1. esp-wifi driver `STA_DISCONNECTED → esp_wifi_connect()` internal allocation.
2. lwIP PCB/pbuf retention on **abnormal (RST) close** — tonight's `WinError 10054` is
   a gateway-side RST; RST closes can leak differently than clean FIN.
3. pbufs queued behind a 14 KB send aborted by `SO_SNDTIMEO` under the raised
   `CONFIG_LWIP_TCP_SND_BUF_DEFAULT=32768`.

**Prescribed instrumentation (next, write-enabled session):** at named points — after
`connect_to_gateway()`, after each `send_frame()`, after `read_gateway_alert()`, after
`drop_connection()` — log both `heap_caps_get_free_size(MALLOC_CAP_INTERNAL)` **and**
`heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)`, tagged with `frame_id`.
Temporarily change `diagnostics_task`'s 30 s interval to per-frame. Comparing *free*
vs *largest-block* over one 30-min soak distinguishes **true leak** (both fall) from
**fragmentation** (free stable, largest-block falls) and localizes per-frame vs
per-reconnect.

### Phase 1 — Firmware correctness & architecture

- **MEDIUM — stale task table in main.c** [main.c:8-18](src/main.c#L8-L18). The ASCII
  header claims `imu_task priority 5 stack 8192` and `dsp_task stack 16384`. Actual:
  `TASK_PRIO_IMU=3`, `TASK_STACK_IMU=3072`, `TASK_STACK_DSP=6144`
  ([epm_config.h:129-139](src/epm_config.h#L129-L139)). The comment contradicts the
  deliberate Session-7 fix (IMU below WiFi so the WiFi stack is never starved).
  *Fix:* correct the three values.
- **MEDIUM — stale summary table in epm_config.h**
  [epm_config.h:105-113](src/epm_config.h#L105-L113): lists `imu_task(5)` and legend
  `5 = mic/imu`, while the `#define` and its own inline comment at
  [epm_config.h:137](src/epm_config.h#L137) correctly say **IMU = 3**. Internal
  contradiction. *Fix:* update the table to `wifi(4), mic(5), imu(3)`.
- **MEDIUM (perf) — imu_task is a CPU-heavy stub**
  [imu_task.c:99-126](src/imu_task.c#L99-L126): `esp_random()` + `cosf()` per sample ×
  2048 × 3 axes + 3 FFTs, every 80 ms — the ~60–66 % duty noted in the request.
  Cheapest stopgap: precomputed sine LUT (phase-stepped) + a noise LUT or xorshift
  PRNG. But it emits **no real data** regardless; the real fix is KX134 integration
  (Phase-plan item d), so only invest in the stopgap if the stub must run longer.
- **Concurrency — verified sound.** `raw_q` ring buffer is single-producer
  (`mic_task`) / single-consumer (`dsp_task`); `dsp_q` and `imu_q` are depth-1
  `xQueueOverwrite`; the adaptive globals `g_adapt_overlap_pct` / `g_adapt_spec_avg_n`
  and `g_hst_warmed_up` are single-byte / bool `volatile` (atomic on Xtensa), written
  by one task and latched at cycle start by the reader.
  - **LOW — snapshot ring buffer torn read** [mic_capture.c:79-138](components/mic_capture/mic_capture.c#L79-L138):
    the ring **data** bytes are written outside the `portMUX`; only the head/count
    indices are guarded. If a `snapshot_send_tcp()` overlaps `mic_task` writes, sample
    bytes can tear. Acceptable for a best-effort pre-trigger diagnostic — *document it*
    (or double-buffer if the snapshot ever becomes load-bearing).
- **Memory placement — correct and well-justified.** `s_enc_pt/ct` (AES scratch),
  `raw_buf`, and the mic ring buffer are pinned to internal DRAM with `DMA_ATTR` /
  `DRAM_ATTR` for the documented GDMA-cache-coherency reason (still valid — GDMA can't
  safely reach PSRAM through cache during concurrent WiFi DMA). `s_mag_db`
  ([dsp_task.c:90](src/dsp_task.c#L90)) and `s_frame`
  ([imu_task.c:90](src/imu_task.c#L90)) are correctly in PSRAM via `EXT_RAM_BSS_ATTR`.
  No buffer needs moving; internal-DRAM headroom is adequate now.
- **Hardware utilization — LOW/opportunity.** The PSRAM pre-trigger ring is 128 KB /
  4 s in 8 MB PSRAM; it could grow substantially if a longer snapshot is wanted. Dual
  core split is balanced (capture+radio on core 0, FFT compute on core 1).

### Phase 2 — Protocol (`epm_protocol.h` ↔ `recv_verify.py`)

- **Framing matches byte-for-byte.** `epm_header_t` = 48 B (`_Static_assert`),
  parsed by `HEADER_FMT='<IIIHHffffBfffBBx'`
  ([recv_verify.py:166](mic_tools/recv_verify.py#L166)) = 48 B. `epm_hello_t` = 24 B ↔
  `HELLO_FMT='<I6sBB12s'`; `epm_alert_v2_t` = 8 B. All consistent.
- **MEDIUM — `overflow_count` is transmitted but discarded.** The firmware computes
  and sends the per-frame I2S DMA overflow delta
  ([wifi_task.c:469-473](src/wifi_task.c#L469-L473)), but the trailing `x` in
  `HEADER_FMT` makes the gateway skip byte 47. A useful "this frame may have an audio
  gap" signal is dropped. *Fix:* change `x`→`B`, unpack `overflow_count`, and flag /
  down-weight gappy frames — or explicitly document the byte as reserved.
- **Robustness confirmed.** `payload_bytes` is bounded to `[min_size .. max_payload]`
  before the read ([recv_verify.py:981](mic_tools/recv_verify.py#L981)); `parse_frame`
  raises on short **and** oversized payloads (guards `np.frombuffer` truncation); GCM
  `InvalidTag` logs a SECURITY event and keeps the connection
  ([recv_verify.py:988-996](mic_tools/recv_verify.py#L988-L996)); replay protection
  requires strictly increasing `frame_id` within a connection
  ([recv_verify.py:1000-1007](mic_tools/recv_verify.py#L1000-L1007)). `last_frame_id`
  resets to −1 per connection while the firmware's `frame_id` keeps counting across
  reconnects — correct (matches the observed #0→#142 numbering across ~15 reconnects).

### Phase 3 — Gateway / Python

- **105 / 105 pytest pass** (192 s). Suite covers baseline, drift, fusion, online
  detector, RUL, simulator, storage.
- **Pipeline (as-built):** `satellite_thread()` per connection →
  `OnlineDetector` (river HalfSpaceTrees, **n_trees=10**, Phase-2-sweep optimal) +
  `BayesianFusion` (multi-channel posterior) + per-satellite `AdaptiveBaseline`
  (**alpha=5e-05**) for `z_kurtosis / z_rms / z_hb / z_ae` + `RULEstimator`
  (exponential-degradation Kalman) → `storage.py` (SQLite `epm.db`, **WAL**) →
  dashboard.
- **LOW — dashboard is stdlib `http.server`, not Flask.** `_DashHandler(BaseHTTPRequestHandler)`
  + `HTTPServer` on port 8080 ([recv_verify.py:3786](mic_tools/recv_verify.py#L3786),
  [:3991](mic_tools/recv_verify.py#L3991)). No `Flask` / `app.route` usage anywhere →
  **`flask>=3.0` in requirements.txt appears unused**; drop it or confirm a consumer.
- **LOW — `plot_mic.py` marked LEGACY** → archive or delete.
- requirements pins otherwise sane: `numpy>=1.24,<2.0` (intentional cap for the array
  APIs used), others floor-pinned.

### Phase 4 — Reconnect ↔ heap interaction · needs-runtime-confirm

Residual disconnects (~60–90 s, `WinError 10054` = gateway-side RST) are
reliability-tier, distinct from the deterministic multi-day blocker (now fixed).
Keepalive is tuned 5/2/3 with `SO_SNDTIMEO=10 s`
([wifi_task.c:247-263](src/wifi_task.c#L247-L263)). **Hypothesis to test in the soak:**
as internal heap tightens, 14 KB frame sends stall sooner (fewer lwIP pbufs) → hit
`SO_SNDTIMEO` → more frequent drops → each reconnect (if leaky) compounds the drain.
**Sequence:** fix the drain first (Phase 0); then measure whether disconnect frequency
falls out on its own before adding a keepalive probe during the IMU wait.

### Phase 5 — Docs / structure / hygiene

- **MEDIUM — `sdkconfig.defaults` PSRAM comment is self-contradictory**
  [sdkconfig.defaults:73-97](sdkconfig.defaults#L73-L97): a large *"PSRAM DISABLED —
  boot-loop investigation pending"* block with re-enable instructions sits directly
  above `CONFIG_SPIRAM=y` (+ `USE_CAPS_ALLOC`, `IGNORE_NOTFOUND`, `ALLOW_BSS_SEG`),
  which are **already enabled and working**. The "restore `EXT_RAM_BSS_ATTR` on
  dsp_task/imu_task" note is also already done. *Fix:* rewrite the block to reflect the
  resolved state.
- **LOW — `src/test_blink.c`** is untracked, **not** in `CMakeLists.txt`
  ([src/CMakeLists.txt:2](src/CMakeLists.txt#L2)), so it is build-excluded dead
  scaffolding → delete.
- **LOW — `sdkconfig.seeed_xiao_esp32s3`** is dead/confusing. The active file is
  `sdkconfig.xiao_esp32s3` (matches `[env:xiao_esp32s3]`; contains `CONFIG_SPIRAM=y`
  @ L1084, `FLASHSIZE_8MB`, `partitions_simple_8mb.csv`). Both are gitignored, so this
  is **local** cleanup only.
- No stray build worktrees present.
- Any doc still asserting PSRAM-disabled, IMU priority 5, or `SPEC_AVG_N=16` is stale
  (build now sets `SPEC_AVG_N=4` in [platformio.ini:33](platformio.ini#L33)) —
  reconcile `docs/` during the doc-fix pass.

### Phase 6 — Security

- **Sound today.** AES-128-GCM with per-frame TRNG IV (`esp_fill_random`), ESP32-S3
  hardware accelerator (`CONFIG_MBEDTLS_HARDWARE_AES=y`), authenticated encryption,
  per-connection replay guard, and SECURITY logging on tag failure.
- **BLOCKER (pre-deployment only, restated) — build-time PSK.** The key is copied from
  the compile-time `EPM_PSK` macro when NVS has none
  ([wifi_task.c:170-189](src/wifi_task.c#L170-L189)); production should provision via
  `nvs_set_blob("epm_sec","psk",…)`. Not urgent for bench work; must be closed before
  field deployment.
- **History is clean:** `EPM_PSK` appears only as a *reference*
  (`memcpy(s_aes_key, EPM_PSK, …)`) — its value lives solely in gitignored
  `wifi_creds.h`, which `git log --all` shows was **never committed**.

### Phase 7 — Git / repo hygiene

- **Clean:** `wifi_creds.h` never committed; `.gitignore` excludes it, `.pio/`,
  `mic_tools/logs/`, `mic_tools/model/`, and both sdkconfig variants. No secret found
  in history.
- **LOW — dead managed dependency:** `src/idf_component.yml` still declares
  `espressif/mdns: ">=1.3.0"` though mDNS is gone from the source → remove it.
- **Add to `.gitignore`:** the local tool session directory, `build_log_stackfix.txt` (build artifact).
- **Everything from tonight is uncommitted** (13 modified `src/`+config files, plus
  untracked `PROJECT_STATUS.md`, partition CSVs, helper scripts).

---

## 3. Git commit plan (recommendation — do NOT auto-run)

Commit in this order so each change is bisectable:

1. **`feat(psram): enable Octal PSRAM + restore external BSS`**
   `sdkconfig.defaults` (SPIRAM lines + rewritten comment), `src/dsp_task.c`,
   `src/imu_task.c` (`EXT_RAM_BSS_ATTR` restored).
2. **`perf(tasks): lower imu priority below wifi, right-size stacks`**
   `src/epm_config.h` (IMU 5→3; DSP/IMU stack cuts; corrected summary table),
   `src/main.c` (corrected ASCII table).
3. **`refactor(wifi): remove mDNS, non-blocking connect, keepalive/timeout tuning`**
   `src/wifi_task.c` (+119/−64), `src/wifi_task.h`.
4. **`build(flash): 8MB, esp-builtin upload, COM7 monitor, SPEC_AVG_N=4`**
   `platformio.ini`, `partitions_simple_8mb.csv`, `src/CMakeLists.txt`.
5. **`refactor(capture): adaptive latch + RGB/mic tweaks`**
   `src/mic_task.c`, `src/dsp_task.c`, `components/mic_capture/mic_capture.c`,
   `src/rgb_led_task.c`.
6. **`feat(gateway): recv_verify updates`** — `mic_tools/recv_verify.py`.
7. **`chore(repo): drop dead files/deps, ignore session artifacts`**
   delete `src/test_blink.c`; drop `espressif/mdns` from `src/idf_component.yml`;
   add the local tool session directory + `build_log_stackfix.txt` to `.gitignore`;
   commit `PROJECT_STATUS.md`.

`wifi_creds.h` stays uncommitted (gitignored — correct). `partitions_8mb.csv` (the
OTA variant) is unused by the current build; keep or delete with the config commit.

---

## 4. Prioritized action plan

- **(a) Root-cause the heap drain, then confirm via soak — TOP PRIORITY.** Add the
  per-frame heap + largest-free-block instrumentation (Phase 0), run 30+ min, watch
  heap stay flat. Until proven flat, connectivity reads *"works until heap exhausts."*
- **(b) Commit tonight's work** per §3 (independent of (a); low risk).
- **(c) Reconnect-frequency hardening** — only after (a) resolves the drain.
- **(d) Real KX134 IMU integration** — replace the stub; the SPI-DMA driver TODOs are
  already spelled out in [imu_task.c:24-46](src/imu_task.c#L24-L46) (3a IRAM ISR, 3b
  DMA buffer in internal DRAM, 3c 8 MHz clock, 3d queued transactions).
- **(e) Model / accuracy tuning** — carry forward `KNOWN_ISSUES.md` WP-02 (HIGH_BAND_MIN
  sweep), WP-03 (time-based CAL_FRAMES), WP-05 (ADWIN delta), WP-08 (fault-model
  resonance calibration — needs hardware), WP-09 (kurtosis clip range).

---

## 5. Open questions (noted, non-blocking)

- Exact heap-drain source — unprovable read-only; instrumentation prescribed in Phase 0.
- Whether the ~60–90 s disconnects are heap-coupled — expected to fall out of the
  Phase 0 soak.
- Whether `flask` is genuinely unused (grep found no consumer) — confirm before
  removing from `requirements.txt`.
