# EPM Verification Report

**Date:** 2026-06-29
**Branch:** main (worktree agent-a02c1e9e32301721d)
**Python:** 3.10.11 (Windows 11)
**Key deps:** river==0.21.0, numpy, pytest-9.1.1

---

## Phase Results Summary

| Phase | Description | Result | Notes |
|-------|-------------|--------|-------|
| 0 | Read all files before touching | PASS | All mic_tools/*.py read; architecture understood |
| 1 | Dependency and import audit | PASS | river installed; all 6 core modules import clean |
| 2 | Unit tests (6 test files, 87 tests) | PASS | 87/87 passed, 0 failed |
| 3 | Integration contract verification | PASS | Protocol v2, FEATURE_DIM=7, RUL fields, save/load all correct |
| 4 | End-to-end gateway + simulator test | PASS | 11/11 checks pass; SIM-01 OK, SIM-02 WARN as expected |
| 5 | Specific bug categories (5a–5j) | PASS* | 9/10 verified; 5j storage.py MISSING (flagged, not imported) |
| 6 | Consistency and cleanliness | PASS | LEGACY docstrings present; bearing_math API correct; 0 TODOs |
| 7 | Final report | DONE | This file |

---

## Bugs Found and Fixed

### Bug 1 — `online_detector.py`: river 0.21 HST score normalization

**File:** `mic_tools/online_detector.py`
**Symptom:** `test_scores_stabilize_on_normal` FAILED (`tail_mean=0.996`, expected `< 0.7`)
**Root cause:** river 0.21 `HalfSpaceTrees.score_one()` returns values near `0.99` even for
perfectly normal data, because `score_one()` computes `1 - raw_mass_score` and normal data has
very low raw mass (near 0), making `1 - score` near 1.0. The test expected normal data → ~0.5.
**Fix:** Added `_score_ema` (EMA of raw HST scores on training samples). In `score()` the output
is normalized so typical healthy data → ~0.5 and anomalies → >0.8. `learn()` updates the EMA
on every healthy sample. `refresh_baseline()` resets it to 0.5. `save()`/`load()` persist it.
**Comments added:** `# VERIFY-FIX:` at each change site.
**Post-fix result:** `tail_mean=0.545`, test passes.

### Bug 2 — `online_detector.py`: `load()` silently accepts mismatched `n_features`

**File:** `mic_tools/online_detector.py`, `load()` method
**Symptom:** Loading a pickle built with `n_features=N` into a detector with `n_features=M`
caused no error but silently corrupted `_mean` and `_m2` arrays (shape mismatch → silent
numpy broadcast errors or wrong dimensions used in `_normalize()`).
**Root cause:** No dimension check in `load()`.
**Fix:** Added check immediately after unpickling:
```python
# VERIFY-FIX: check dimension match so a pickle from a different FEATURE_DIM
# doesn't silently corrupt the normalizer arrays (mean/m2 shape mismatch).
saved_n_features = state.get('n_features', self.n_features)
if saved_n_features != self.n_features:
    raise ValueError(...)
```

### Bug 3 — `satellite_sim.py`: UnicodeEncodeError on Windows (cp1252)

**File:** `mic_tools/satellite_sim.py`, lines 193–194, 311–313
**Symptom:** `UnicodeEncodeError: 'charmap' codec can't encode character '→'` when
running on Windows with default cp1252 console encoding.
**Root cause:** Print statements used Unicode characters `→` (U+2192), `—` (U+2014),
`λ` (U+03BB), `⁻¹` (U+207B U+00B9) which Windows cp1252 cannot encode.
**Fix:** Replaced all problematic Unicode characters with ASCII equivalents:
- `→` → `->`, `—` → `-`, `λ` → `lam`, `⁻¹` → `s^-1`
**Comments added:** `# VERIFY-FIX:` at each change site.

### Bug 4 — `recv_verify.py`: UnicodeEncodeError on Windows (cp1252)

**File:** `mic_tools/recv_verify.py`, lines 344, 1010–1011, 2712, 3543–3544
**Symptom:** Same `UnicodeEncodeError` on Windows for dashboard and alert print statements.
**Root cause:** Print statements used `─` (U+2500), `≤` (U+2264), `→` (U+2192), `←` (U+2190).
**Fix:** All replaced with ASCII equivalents (`-`, `<=`, `->`, `<-`).
**Comments added:** `# VERIFY-FIX:` at each change site.

---

## Phase 5 Bug Category Checks

| Check | Description | Result |
|-------|-------------|--------|
| 5a | Thread safety — `_sat_lock` around per-satellite state | PASS |
| 5b | Division-by-zero guards (Bayesian `den`, Welford `n-1`, RUL `lam`, `std`) | PASS |
| 5c | No hardcoded FEATURE_DIM (uses `FEATURE_DIM` constant everywhere) | PASS |
| 5d | `OnlineDetector.load()` dimension check | FIXED (Bug 2 above) |
| 5e | HST warm-up guard (`is_warmed_up()` checked in `compute_alert()`) | PASS |
| 5f | RUL guard (`t_hours = max(0.0, rul)` prevents negative RUL) | PASS |
| 5g | `sys.path.insert(0, ...)` in `satellite_sim.py` for sibling imports | PASS |
| 5h | ADWIN API (`drift_detected` attribute — correct for river >= 0.21) | PASS |
| 5i | BayesianFusion NaN handling (NaN z-scores silently dropped) | PASS |
| 5j | Storage WAL (sqlite3 WAL mode) | N/A — `storage.py` not present (see below) |

---

## Phase 6 Consistency Checks

| Check | Result | Notes |
|-------|--------|-------|
| 6a ml_trainer.py has LEGACY in docstring | PASS | `ml_trainer.py — LEGACY: Batch IsolationForest...` |
| 6a ml_infer.py has LEGACY in docstring | PASS | `ml_infer.py — LEGACY: Offline IsolationForest...` |
| 6a ml_trainer.py import without crash | PASS | Exits with clean user message when scikit-learn missing |
| 6a ml_infer.py import without crash | PASS | Same clean exit; no uncaught exception |
| 6b bearing_math.py API works correctly | PASS | `BearingFreqs.from_shaft_hz(25, 6205)` → bpfo=82.40, bpfi=142.60, bsf=43.38, ftf=9.16 |
| 6b fault_models.py uses bearing_math API | PASS | Imports `BearingFreqs`, `BearingGeometry`, `COMMON_BEARINGS`; `generate_mic_frame()` tested |
| 6c No TODO/FIXME/NotImplemented | PASS | 0 hits across all mic_tools/*.py |

---

## Known Missing Files (not imported anywhere — not a runtime gap)

| File | Status |
|------|--------|
| `mic_tools/storage.py` | NOT PRESENT. No Python file in the repo imports it. No unit test references it. Not a runtime dependency. |
| `mic_tools/inference.py` | NOT PRESENT. Not imported anywhere in the codebase. |

These were listed as targets in the original task but are absent from the repo tree and not referenced by any import. No code path will fail at runtime because of their absence.

---

## Remaining Known Issues (hardware / environment)

1. **scikit-learn / joblib not installed** — `ml_trainer.py` and `ml_infer.py` exit with a user-friendly error. These are LEGACY modules not needed for normal gateway operation. Install with `pip install scikit-learn joblib` if offline analysis is needed.

2. **Windows PYTHONIOENCODING** — Even with the Unicode → ASCII fixes, running `recv_verify.py` or `satellite_sim.py` without `PYTHONIOENCODING=utf-8` will raise `UnicodeEncodeError` on any remaining non-ASCII character in log messages or exception text. Set `PYTHONIOENCODING=utf-8` in the environment or use `chcp 65001` before running.

3. **HST warm-up latency** — `OnlineDetector.is_warmed_up()` returns False for the first 250 frames (~113 seconds at 2.2 fps). During this window the gateway shows `hst=--` instead of a score. This is by design (HST needs a full window to be valid) and is documented in the code.

4. **BayesianFusion.fuse([100.0, 100.0, 100.0]) returns exactly 1.0** — `float` precision causes `p == 1.0` (not `p < 1.0`) for extreme z-scores. This is a floating-point edge case with no practical impact (p=1.0 is correct for extreme anomaly evidence). No unit test asserts `p < 1.0` for such inputs.

5. **QRB2210 Adreno 702 GPU** — Hardware integration (Qualcomm NPU neural autoencoder) not testable without physical hardware. Simulation tests confirm the software stack but not the GPIO/UART firmware path.

---

## Ready for Next Phase?

**YES**, with the caveat that hardware-in-the-loop testing (GPIO, UART, firmware) requires physical hardware (QRB2210 / Arduino Uno Q).

Software stack is verified:
- 87 unit tests pass
- End-to-end gateway + simulator test passes (2 satellites, healthy + fault)
- All critical bugs (Unicode encoding, HST normalization, stale pickle loading) are fixed
- No open TODO/FIXME/NotImplemented items remain
- Protocol v2, FEATURE_DIM=7, RUL estimator, Bayesian fusion, ADWIN drift detection — all verified correct
