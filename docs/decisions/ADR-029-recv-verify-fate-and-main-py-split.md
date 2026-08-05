---
id: ADR-029
title: recv_verify.py kept as a stateful module, entrypoint wiring split into gateway/main.py
status: accepted
date: 2026-08-06
deciders: Abhinav Krishna N
---

## Context

Before Phase 8b3, `mic_tools/recv_verify.py`'s `main()` did two jobs at once: (1) argument parsing and startup wiring (building `BayesianFusion`/`InferenceEngine`/`Storage` instances, registering mDNS, starting the dashboard and TCP-accept threads, deciding plot-vs-headless), and (2) owning the module-level state (`CREST_WARN`, `_storage`, `_decryptor`, `_bayesian_fusion`, `_ae_engine`, `_FACTORY_NAME`, etc.) and the shared pipeline functions (`_process_satellite_frame`, `_sat_register`, `_log_security_event`, `run_plot`, `get_local_ip`) that both ingestion paths and every `gateway/*` module already reach via lazy `import recv_verify as _rv`. Phase 8b3's mandate is to give the gateway a real `main.py` entrypoint under `gateway/`, but `recv_verify.py`'s state and shared functions can't simply move wholesale — `gateway/api/*.py`, `gateway/pipeline/alerting.py`, `gateway/pipeline/ml_scoring.py`, and `gateway/registry/satellite_state.py` all already depend on lazily importing `recv_verify` specifically to read/write that module's globals, and rewriting all of them to import a relocated state module instead is a much larger, riskier change than a structural phase should attempt.

Three shapes were available for `recv_verify.py`'s own fate:

1. **Keep `recv_verify.py` exactly as-is, `main()` included** — add `gateway/main.py` as a pure re-export/alias with no real content, purely to satisfy "gateway has a main.py" as a checkbox.
2. **Retire `recv_verify.py` as an entrypoint entirely** — move all its state and functions into `gateway/` proper (e.g. `gateway/state.py`), update every lazy-import call site across `gateway/api/`, `gateway/pipeline/`, `gateway/registry/` to point at the new location, and update `mic_tools/Dockerfile`/`docker_start.py`/`docker-compose.yml` to invoke `gateway/main.py` directly instead of `recv_verify.py`.
3. **Hybrid: keep `recv_verify.py`'s state and shared functions exactly where they are** (so every existing lazy `import recv_verify as _rv` across `gateway/` keeps working unchanged), **move only the argparse-and-wiring logic out of its `main()`** into a new `gateway/main.py` that imports `recv_verify` at module level and drives it via `rv.NAME = value` assignments (equivalent to `global NAME` executed from inside `recv_verify.py` itself), and **reduce `recv_verify.py`'s own `main()` to a one-line forwarding shim** (`from gateway.main import main as _main; _main()`).

## Decision

**Option 3.** `gateway/main.py` now does the real argument parsing and startup wiring — all of the old `main()`'s `argparse.ArgumentParser` setup, the port-in-use guard, autoencoder/auth/notification/factory-name/bearing/SQLite-storage/mDNS setup, and final dashboard/thread/plot wiring — importing `recv_verify` at module level (`import recv_verify as rv`) and mutating its globals via `rv.CREST_WARN = args.crest_warn`, `rv._storage = Storage(...)`, `rv._bayesian_fusion = BayesianFusion(...)`, etc. `recv_verify.py`'s own `main()` shrinks to:

```python
def main():
    from gateway.main import main as _main
    _main()
```

`recv_verify.py` keeps every module-level global, `_process_satellite_frame`, `_sat_register`, `_log_security_event`, `run_plot`, `_DisplayState`, and `get_local_ip()` exactly where they were.

Reasoning:

- **Option 1 was rejected** as dishonest structure — a `gateway/main.py` that's a bare re-export doesn't give the gateway package a real entrypoint, it just adds a file to satisfy Phase 8's exit criteria on paper. The actual goal (argparse + wiring living under `gateway/`, matching where the rest of the gateway logic now lives) requires the logic to actually move.
- **Option 2 was rejected for this phase** because of blast radius disproportionate to a structural-only phase: it would require touching every lazy-import call site in `gateway/api/*.py`, `gateway/pipeline/alerting.py`, `gateway/pipeline/ml_scoring.py`, and `gateway/registry/satellite_state.py` (all established in earlier 8b sub-phases specifically to avoid circular imports with `recv_verify.py`), plus rewriting `docker_start.py` and the Dockerfile's entrypoint. That's a legitimate end-state, but it conflates "move the entrypoint" with "eliminate `recv_verify.py` as a module," which is a bigger, separate decision better made once all of `gateway/`'s state genuinely lives under `gateway/` (a future phase, not 8b3).
- **Option 3's `rv.NAME = value` mechanism is exactly equivalent to `global NAME; NAME = value` run from inside `recv_verify.py`** — Python module attribute assignment from outside a module and a `global` statement executed inside it both mutate the same `__dict__` entry. This meant the entire body of the old `main()` could be translated near-mechanically (prefix bare names with `rv.`) with no behavioral change, rather than needing a redesign of how state is threaded through the pipeline.
- `gateway/main.py` is the one place in the whole `gateway/` tree that imports `recv_verify` at module level instead of lazily — safe specifically because `recv_verify.py` does not import `gateway.main` at its own module top level (only lazily, inside the one-line `main()` shim), so there is no circular-import cycle for this particular pair, unlike every other `gateway/*` module that gets imported *by* `recv_verify.py` at its top level and must therefore import it back only lazily.
- Keeping `recv_verify.py`'s `main()` as a forwarding shim (rather than deleting it) means `python recv_verify.py ...` — existing muscle memory, any external docs, and `mic_tools/docker_start.py`'s `import recv_verify; recv_verify.main()` — all keep working unchanged. No caller needed to be updated.

## Consequences

**Positive:**

- `gateway/main.py` is a real, complete entrypoint — argparse, port guard, and every startup-wiring decision genuinely live under `gateway/` now, satisfying Phase 8's structural exit test without a follow-up phase.
- Zero call-site churn: every existing lazy `import recv_verify as _rv` across `gateway/api/`, `gateway/pipeline/`, `gateway/registry/` needed no changes, and neither did `mic_tools/docker_start.py`.
- `docker_start.py`/Dockerfile/docker-compose.yml needed only a `gateway/` package COPY and stale-path fixes (see the Dockerfile's own comments), not an entrypoint change — `import recv_verify; recv_verify.main()` still resolves correctly since it now just forwards.
- The translation from old `main()` to `gateway/main.py` is mechanical and auditable (`global X` → `rv.X`), minimizing the chance of a subtle behavioral regression during the move — confirmed by a clean 124-passed test run and a manual startup smoke test (`docker_start.py` run end-to-end from a proxy image layout: SQLite storage, mDNS, dashboard, and TCP receiver all wired up correctly).

**Negative / trade-offs:**

- `recv_verify.py` is not "just legacy" and not "fully migrated" — it's a module that still owns real state and shared pipeline functions but no longer owns its own entrypoint logic. This split (state + shared functions in `recv_verify.py`, entrypoint in `gateway/main.py`) is a genuine hybrid, not a clean single-direction migration, and needs this ADR to be legible to a future reader who might otherwise ask "why isn't this all just in `gateway/`?"
- `gateway/main.py`'s `rv.NAME = value` pattern is easy to misread as "these are gateway/main.py's own settings" rather than "these are recv_verify.py's globals, mutated from outside" — mitigated by the module docstring calling out the equivalence explicitly.
- A second, separate future phase is now implied (though not scheduled) to actually retire `recv_verify.py` as a module once its remaining state/functions are ready to move into `gateway/` proper — this ADR does not commit to when or whether that happens.

**Revisit this ADR if:** a future phase decides to fully migrate `recv_verify.py`'s remaining state and shared functions (`_process_satellite_frame`, `_sat_register`, `_log_security_event`, `run_plot`, `get_local_ip`, all module globals) into `gateway/` proper — at that point `recv_verify.py` can be retired for real (Option 2, revisited), and every lazy-import call site updated in one coordinated pass rather than piecemeal.

## Validation

Design decision only — no hardware dependency. Verified against actual code: grepped the whole repo for `recv_verify` (50 files) and confirmed the only real call sites needing changes were inside `recv_verify.py` itself (its own `main()`); every other reference was either an existing lazy-import pattern already tolerant of this change, or a historical doc out of scope. Smoke-tested both `import recv_verify` and `import gateway.main` from the repo root, and separately from a proxy directory replicating the exact flat `/app` layout the Docker image now produces (`gateway/`, `recv_verify.py`, `docker_start.py` as siblings) — `docker_start.py` run end-to-end from that layout for 8 seconds confirmed the full startup sequence (SQLite storage, mDNS advertisement, dashboard, TCP accept loop) completes with no errors before the process is killed by the test's own timeout. Full test suite: 124 passed, matching the pre-change baseline exactly.
