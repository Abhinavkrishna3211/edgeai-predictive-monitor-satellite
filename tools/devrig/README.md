# tools/devrig

Thin wrappers around the reference repository's own
`base-station/start_desktop_dashboard.sh` so we can drive its
**unmodified** local dashboard rig from this repo without ever touching its
tree. Nothing here reimplements the reference script's logic -- `devrig.sh`
only finds a broker, snapshots the reference repo's tracked-file status, and
invokes its script verbatim.

## Prerequisites

- WSL with an `Ubuntu-24.04` distro (`wsl --install -d Ubuntu-24.04`), with
  `mosquitto`, `mosquitto-clients`, `python3-venv`, `python3-pip`, `git`
  installed inside it.
- A read-only clone of the reference repo, sibling to this repo:
  ```
  git clone --depth 1 https://github.com/rahuljeyaraj/edgeai-predictive-monitor \
      /mnt/c/Users/abhin/Documents/edgeai-predictive-monitor-ref
  ```
  Pinned reference commit used for Phase 0: `ab2d89e22da977c705845e0c1d85c172ecab1089`.

## Usage

From Windows PowerShell:
```
tools\devrig\devrig.ps1 --nodes 1 --port 8180 --captures-dir "" --auto-online
```

From inside WSL directly:
```
bash tools/devrig/devrig.sh --nodes 1 --port 8180 --captures-dir "" --auto-online
```

All arguments are passed straight through to the reference repository's
`base-station/start_desktop_dashboard.sh` -- see that script's own header
comments for the full flag list. `--captures-dir ""` triggers its synthetic
capture generator (`python/tools/gen_synthetic_captures.py`) when no real
`.npz` captures are present.

Override the reference repo location with the `EPM_REF_REPO` env var
(defaults to `/mnt/c/Users/abhin/Documents/edgeai-predictive-monitor-ref`).

## What this does NOT do

- Never edits, patches, or copies logic out of the reference script --
  `devrig.sh` calls it verbatim as `bash <ref>/base-station/start_desktop_dashboard.sh`.
- Never writes into the reference repo except what the reference script
  creates there itself (`base-station/python/.venv/`, `base-station/.cache/`) --
  untracked build artifacts, not source changes. `devrig.sh` diffs
  `git status --porcelain --untracked-files=no` before/after each run and
  warns (does not fail) if a tracked file changed, as a tripwire.

## capture_golden_frame.sh

Captures one raw MQTT frame from a running sim node (topic `epm/+/data`)
to `tests/golden/sim_reference.hex` with a 3-line provenance header. Run it
while a `devrig.sh`/`devrig.ps1` session is up and a sim node is online.
