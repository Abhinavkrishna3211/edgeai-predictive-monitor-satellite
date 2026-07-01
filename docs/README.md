# EPM Satellite Documentation

This folder contains engineering decisions, performance baselines, and hardware documentation for the Edge Predictive Monitor (EPM) satellite firmware running on the Seeed Studio XIAO ESP32-S3.

---

## Folder structure

```
docs/
  decisions/          Architecture Decision Records (ADRs)
  performance/        Measured baselines and audit results
  hardware/           Pin allocation and peripheral map
  CHANGELOG.md        One entry per firmware change with measured impact
  README.md           This file
```

---

## Decision records (ADRs)

| ID | Title | Key decision |
|---|---|---|
| ADR-001 | Online learning — Half-Space Trees | HST over IsolationForest/autoencoder for O(Ψ·h) online update |
| ADR-002 | RUL estimation — Exponential + Kalman | Paris Law exponential model + Kalman filter over linear regression |
| ADR-003 | Sensor fusion — Bayesian product | Bayesian likelihood product over max()/mean() fusion |
| ADR-004 | Drift detection — ADWIN | ADWIN (O(log n) memory, bounded false alarm rate) over scheduled retrain |
| ADR-005 | ESP32-S3 task and core layout | I/O tasks on core 0, FFT compute on core 1 |
| ADR-006 | RGB LED via LEDC hardware fade | LEDC hardware engine over vTaskDelay/esp_timer (near-zero CPU) |
| ADR-007 | AES-GCM via hardware GDMA | Hardware AES+GDMA over software path (35.8% → 3% CPU) |
| ADR-008 | FFT via ESP-DSP | ESP-DSP vectorised over scalar Cooley-Tukey (3.8× speedup) |
| ADR-009 | PSRAM memory layout | Selective EXT_RAM_BSS_ATTR for cold-path output buffers only |
| ADR-010 | TCP frame protocol | MSG_MORE batching + keepalive 5/2/3 + 17 dBm TX cap |

---

## Performance files

| File | Contents |
|---|---|
| `performance/BASELINE.md` | Before/after tables with measured CPU%, stack HWM, heap free, FFT cycles |
| `performance/HARDWARE_AUDIT_RESULTS.md` | Full Phase 0 audit report, per-phase changes, Phase 10 summary table |
| `performance/PARAMETER_INVENTORY.md` | All 67 tunable parameters across the system with justification status |
| `performance/SIMULATION_BASELINE.md` | 3-seed baseline: cohen_d≈2.55, fp=0, detect@512; RUL accuracy table |
| `performance/SWEEP_RESULTS.md` | OVAT sweeps — HST hyperparameters, Bayesian fusion params, EMA alpha |
| `performance/NUMERICAL_STABILITY.md` | dBFS floor audit, float64 Kalman precision, edge-case NaN/Inf propagation |
| `performance/SCALE_TESTING.md` | Scale test N=1..50 satellites; long-duration stability; alert storm |
| `performance/COMPARATIVE_VALIDATION.md` | HST vs IsolationForest; Bayesian vs max(); Exponential+Kalman vs linear |
| `performance/WEAK_POINTS_AUDIT.md` | 9 weak points WP-01..09, ordered by severity (HIGH→LOW) |
| `performance/KNOWN_ISSUES.md` | Deferred weak points (WP-02/03/05/07/08/09) with resolution paths |

All numbers in the `performance/` directory are measured by `mic_tools/sim_sweep.py`.
No values are invented or hand-picked.

---

## Hardware files

| File | Contents |
|---|---|
| `hardware/PIN_ALLOCATION.md` | Every active GPIO, retired GPIO (21), free GPIOs, LEDC channel assignment, INMP441 wiring |
| `hardware/PERIPHERAL_MAP.md` | I2S0, SPI2, LEDC, WiFi, AES accelerator, PSRAM, UART0 configuration tables; GDMA arbitration |

---

## Update procedure

Follow these rules when making firmware changes:

### Always required
- Add a `CHANGELOG.md` entry with: what changed, why (ADR reference if applicable), measured impact (or explicit `<not yet measured — reason>`).

### For architectural decisions
- Create a new ADR in `decisions/` using the template in any existing ADR.
- Every ADR **must** include at least one of:
  - A formula with numerical evaluation
  - A measured value with the measurement method stated
  - A published citation (author, conference/journal, year)
  - A stated logical proof
  - "It seemed better" is never acceptable.

### For performance changes
- Add or update a row in `performance/BASELINE.md` with before/after values.
- State the measurement method (e.g., `vTaskGetRunTimeStats`, `esp_cpu_get_cycle_count`, `heap_caps_get_free_size`).

### For new GPIO or peripheral usage
- Update `hardware/PIN_ALLOCATION.md` — add the new GPIO to the allocated table, remove it from the free table.
- Update `hardware/PERIPHERAL_MAP.md` — add the peripheral configuration block.

### For deprecated GPIO or peripheral
- Move the GPIO to the "Retired" table in `PIN_ALLOCATION.md`; do not delete — history matters.

---

## Validation standard

Before any ADR or CHANGELOG entry is considered complete:

1. **No unresolved entries:** Every `.md` file in `docs/` must contain only completed, measured, or cited content. Files with deferred markers or unfilled stub sections must be resolved before merge.

2. **No stale single-LED task references:** The single-GPIO indicator task (`led_task`) was deleted; its handle variable and task name must not appear in `src/`. Only `docs/CHANGELOG.md` (retroactive migration entry) and `docs/hardware/PIN_ALLOCATION.md` (retired-pin note) are allowed to reference it by name for historical context.

3. **Every ADR must include** at least one of: a formula with numerical evaluation, a measured value with the measurement method stated, a published citation, or a stated logical proof.

---

## Hardware summary

**Device:** Seeed Studio XIAO ESP32-S3 (ESP32-S3FH4R2)  
**CPU:** Dual Xtensa LX7 @ 240 MHz  
**Internal SRAM:** 512 KB  
**PSRAM:** 8 MB OPI DDR (Octal SPI)  
**Flash:** 8 MB  
**IDF:** ESP-IDF 5.x  
**Microphone:** INMP441 MEMS (I2S, GPIO2/3/4, 16 kHz, 512-sample frames)  
**IMU:** KX134 (SPI, GPIO7/8/9/10) — driver stub active; hardware not yet connected  
**LED:** RGB common-cathode (LEDC, GPIO1/5/6)  
**Encryption:** AES-128-GCM (hardware accelerator + GDMA)  
**Transport:** TCP, 2.2 fps, ~14 KB/frame, 17 dBm TX
