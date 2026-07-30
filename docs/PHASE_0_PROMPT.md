# Phase 0 build prompt — paste into VS Code the AI coding assistant

Read MASTER_PLAN.md Part A, Part F, Part G Phase 0 first if not already loaded. Work in small diffs, terse output, one-line rationale per change, no restating unchanged code. Stop at the exit test below — do not opportunistically start Phase 1.

## Corrections to the plan (verified live against rahuljeyaraj/edgeai-predictive-monitor, do not re-derive these)

- Module path is `base-station/python/`, NOT `mpu/`. `docs/Running_Dashboard_And_Satellite_Sim.md` in his repo is itself stale (says `mpu/main.py`, `--data-dir`, old repo name) — ignore that doc's literal commands.
- **A working local dev rig already exists in his repo: `base-station/start_desktop_dashboard.sh`.** It launches `python/main.py` (dashboard) + N `python/tools/satellite_node_sim.py` processes against a local Mosquitto broker on port 1883, dashboard on port 8180. Use this script directly — do not build a parallel one.
- The sim node's real interface is `--captures-dir <dir of .npz files>` (falls back to synthetic captures via `tools/gen_synthetic_captures.py` if the dir is empty), NOT `--data-dir` pointing at a Kaggle CSV dataset as an earlier draft assumed. Each sim node exposes an HTTP control API (`/config`, `/online`, `/state`) and a browser UI at its `--ui-port`.
- Root deps: `base-station/python/requirements.txt` (torch, psutil, python-statemachine, ai-edge-litert) + `base-station/python/tools/requirements-desktop.txt` (fastapi, uvicorn, websockets, numpy, paho-mqtt). Torch install is slow — expect several minutes.

## Tasks

1. In **our repo** (`edgeai-predictive-monitor-satellite`):
   - `git checkout -b feat/base-station-interop`
   - `git tag baseline-working`
   (If already done in a prior session, verify with `git branch --show-current` and `git tag -l baseline-working` — do not recreate.)

2. Clone the reference-repo maintainer's repo **read-only**, sibling to ours, e.g. `../edgeai-predictive-monitor-ref/`. Never write to it. Add its path to `.gitignore` if inside our tree (it should not be).

3. Install Mosquitto locally (native install is fine on the dev machine — `apt-get install mosquitto mosquitto-clients` or platform equivalent). Confirm listening on 1883.

4. Run `base-station/start_desktop_dashboard.sh` unmodified from the cloned reference repo. Confirm:
   - Dashboard reachable at `http://127.0.0.1:8180/index.html`.
   - The synthetic-capture path works if no real `.npz` files are available (`gen_synthetic_captures.py` runs without error).
   - Sim node UI reachable at its `--ui-port`, "Go Online" makes it appear in the dashboard's fleet view.

5. Capture one raw published MQTT frame from the sim node to `tests/golden/sim_reference.hex` in **our** repo:
   - `mosquitto_sub -h localhost -t 'epm/#' -v` (confirm actual topic pattern against `base-station/python/ingestion/mqtt_subscriber.py` — do not assume `epm/` prefix without checking, this hasn't been verified against his subscriber source yet).
   - Save exactly one full raw payload as hex, plus a 3-line header comment noting date, sim node id, and which capture file it was replaying.

6. Create `tools/devrig/` in our repo with thin wrapper scripts (not a reimplementation) that:
   - Point at the sibling reference-repo clone.
   - Start Mosquitto (or check it's running).
   - Invoke his `start_desktop_dashboard.sh` with the right `--captures-dir`.
   - Document, in a `tools/devrig/README.md`, that this only orchestrates his unmodified script and never edits anything under the reference repo path.

## Exit test

- His sim node appears correctly on his **unmodified** dashboard, driven from our devrig scripts.
- `tests/golden/sim_reference.hex` exists with one captured real frame and a 3-line provenance header.
- `git log` in our repo shows no changes to anything outside `tools/devrig/`, `tests/golden/`, `.gitignore`.
- Confirm zero AI attribution: `git log --format='%B' | grep -iE 'claude|generated with|co-authored'` returns nothing.

Report back: exact MQTT topic string observed on the wire (for Phase 4's contract verification), and anything in `mqtt_subscriber.py` that contradicts Part D of the master plan.
