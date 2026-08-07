# mic_tools/ — pytest harness

Regression tests for `mic_tools/`'s pure-logic analytics modules, run
against the code **as it exists today**, before the planned Phase 8
gateway restructure touches anything. These tests must (and
do) pass clean against unmodified `mic_tools/` — unlike the firmware side's
`tests/host/` harness, nothing here is an expected failure.

(Tests live as flat files directly in `mic_tools/`, not in a
`mic_tools/tests/` subdirectory — that restructuring is Phase 8's job, not
this phase's.)

## Prerequisites

```
pip3 install -r requirements.txt -r requirements-dev.txt
```

`requirements-dev.txt` adds `pytest` (not part of the deployed Uno Q's
install list, since it has no runtime role there — dev/test only).

## Commands

```
# Phase 1's required minimum set (bearing_math, rul_estimator,
# bayesian_fusion, adaptive_baseline):
python -m pytest mic_tools/test_bearing_math.py mic_tools/test_rul.py mic_tools/test_fusion.py mic_tools/test_baseline.py -v

# Everything under mic_tools/ (optional, broader, slower):
python -m pytest mic_tools/ -q
```

Run from the repo root — pytest auto-adds `mic_tools/` onto `sys.path`
when invoked this way (no `conftest.py`/`pyproject.toml` needed or present).

## What "pass" means

Plain pytest green / exit code 0. Every test in this harness is expected to
pass against current, unmodified source — there is no analog here to the
firmware side's one documented `EXPECTED-FAIL`.

## Module coverage

| Module | Test file | Status |
|---|---|---|
| `bearing_math.py` | `test_bearing_math.py` | **new**, added this phase |
| `rul_estimator.py` | `test_rul.py` | pre-existing |
| `bayesian_fusion.py` | `test_fusion.py` | pre-existing |
| `adaptive_baseline.py` | `test_baseline.py` | pre-existing |
| `online_detector.py` (ADWIN drift) | `test_drift.py`, `test_online_detector.py` | pre-existing, adjacent |
| `fault_models.py` | `test_simulator.py` | pre-existing, adjacent |
| `storage.py` | `test_storage.py` | pre-existing, adjacent |

Only `test_bearing_math.py` is new. The other three modules Phase 1 names
already had comprehensive coverage before this phase started.

## Expected runtime

- Required minimum set (4 files, 54 tests): confirmed **54 passed in
  ~12.5s** on this machine.
- Full `mic_tools/` directory (adds `test_drift.py`'s ADWIN simulation,
  `test_online_detector.py`, `test_storage.py`, `test_simulator.py`):
  confirmed **50 passed in ~105s** for the broader pre-existing set alone —
  optional, not part of Phase 1's minimum bar.

## Zero behavior change

No `mic_tools/*.py` source module is edited by this phase — only
`test_bearing_math.py`, this doc, and `requirements-dev.txt` are added.
