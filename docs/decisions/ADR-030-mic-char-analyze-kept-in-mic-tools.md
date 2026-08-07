---
id: ADR-030
title: mic_char_analyze.py kept as live tooling, stays in mic_tools/ (not archived, not relocated)
status: accepted
date: 2026-08-06
deciders: Abhinav Krishna N
---

## Context

Phase 8c closes out the gateway restructure. Two scripts outside `gateway/`
still speak the legacy TCP+AES wire protocol independently (each with its own
copy of the frame-format constants): `satellite_sim.py` (relocated to
`tools/` earlier in this phase, see the "Task 2" commit and ADR-028) and
`mic_tools/mic_char_analyze.py`, an INMP441 frequency-response
characterization tool built for "WP-08" (its own internal label, per its
docstring) — not to be confused with the *different*, also-open WP-08 in
`docs/performance/WEAK_POINTS_AUDIT.md` / `KNOWN_ISSUES.md` (fault_models.py's
uncalibrated resonance centre/sigma parameters). The two are unrelated items
that happen to share a work-package number.

Two questions needed answering, in order: (1) is this tool's underlying work
closed, such that it should be archived/documented as historical rather than
kept live; and (2) if kept, does it belong alongside `satellite_sim.py` in
`tools/`, or somewhere else.

**Is the underlying characterization work closed?** No — checked directly
rather than assumed:

- `mic_tools/char_logs/` (untracked) contains exactly two session CSVs, both
  `20260704_*`, both ambient-only (`32000hz_fft1024_ambient_*`). No tone-test
  session has ever been logged, despite the tool's own docstring documenting
  a tone-test workflow (`--tone`) as the primary use case.
- Four sample-rate-specific sdkconfig files exist and are untracked/unused —
  `sdkconfig.mic_char_16k`, `_22050`, `_32k`, `_48k` — clearly staged for a
  multi-rate characterization sweep that has not happened yet.
- `docs/MASTER_PLAN.md`'s interop tracker (the "Sensor parameters" row) lists
  reconciling our multi-rate mic sampling against the reference implementation's fixed 96 kHz/2048-pt
  choice as a real, not-yet-scoped decision, to be made "with an ADR recording
  the choice and why" — and that decision explicitly needs per-rate empirical
  data, which is exactly what this tool produces and nothing else in the repo
  does.

This is incomplete, still-needed work, not a closed work item — ruling out
"archive as historical."

## Decision

**Kept as live, reusable diagnostic tooling — not archived. Stays in
`mic_tools/`, not relocated to `tools/`.**

The "keep, don't archive" half mirrors ADR-028's satellite_sim.py reasoning:
working tooling that a real, still-open task depends on isn't deleted or
mothballed just because a restructuring phase is passing through.

The "stays in `mic_tools/`" half does **not** mirror `satellite_sim.py`'s
relocation, for reasons specific to what each tool actually is:

- `satellite_sim.py` is a generic test double for the whole gateway pipeline
  (IMU + mic, multi-satellite) — it has no inherent tie to microphone
  hardware and mirrors the reference repository's `tools/satellite_node_sim.py`
  layout, which is what motivated its move.
- `mic_char_analyze.py` is INMP441-specific hardware bring-up/characterization
  tooling — it belongs with the other artifacts of that same work (the
  `sdkconfig.mic_char_*` sweep configs, `char_logs/` output, and its own
  standalone receive loop that exists specifically to characterize this one
  piece of hardware).
- `mic_tools/` was never fully emptied by this restructure in the first
  place: `recv_verify.py` itself stays there (ADR-029), and so do several
  other dev/analysis scripts never touched by Phase 8 —`plot_mic.py`,
  `sim_sweep.py`, `test_simulator.py`, `ml_trainer.py`, `train_autoencoder.py`.
  Moving only `mic_char_analyze.py` out to `tools/` while leaving those in
  place would be an arbitrary, inconsistent line to draw; `satellite_sim.py`
  had a specific, named justification (ADR-028 + the reference-repo mirror) that
  this tool doesn't share.

No file move means no docstring/usage-text update is needed — its existing
`python mic_tools/mic_char_analyze.py --sample-rate ...` usage lines and
`--log-dir` default (`mic_tools/char_logs`) remain accurate as written.

## Consequences

**Positive:**

- No churn for a tool with active, incomplete characterization work still
  ahead of it (the 4-sample-rate sweep, tone tests).
- `mic_tools/`'s remaining contents stay coherent: it now holds the core
  gateway module still pending full retirement (`recv_verify.py`, ADR-029)
  plus mic/ML-specific dev tooling, while `tools/` holds generic
  system-level dev tooling (`satellite_sim.py`). The two directories now have
  a legible split by *kind* of tool, not just by restructuring happenstance.
- `char_logs/`'s existing two sessions and the `_index.csv` summary format
  stay valid for direct comparison against future sessions once the sweep
  resumes, since the CSV logging is neither moved nor renamed by this
  decision.

**Negative / trade-offs:**

- `mic_tools/` remains a mixed bag (core module + dev tooling + ML training
  scripts) rather than being fully retired to just gateway-adjacent code —
  already true before this decision (ADR-029), unaffected by it either way.
- This file stays untracked in git a while longer regardless of the
  decision here; being added to version control for the first time (as part
  of this phase's commits) is orthogonal to where it lives.

**Revisit this ADR if:** the multi-rate characterization sweep finishes and
the sensor-parameters ADR referenced in `docs/MASTER_PLAN.md` gets written —
at that point `mic_char_analyze.py`'s job is done and it becomes a genuine
candidate for archiving (closing the loop this ADR left open), the same way
ADR-028 names its own future trigger for retiring `tcp_legacy.py`.

## Validation

Design decision only — no hardware dependency. Verified against actual repo
state, not assumption: read `mic_char_analyze.py` in full; listed
`mic_tools/char_logs/` directly and confirmed both existing sessions are
ambient-only; grepped `docs/performance/KNOWN_ISSUES.md` and
`WEAK_POINTS_AUDIT.md` for "WP-08" and confirmed that item is unrelated
(fault_models.py resonance calibration, itself still DEFERRED/open, not
closed) rather than this tool's own internal WP-08 label; grepped
`docs/MASTER_PLAN.md` for the still-unscoped sensor-parameters interop
decision that depends on this tool's output; listed `mic_tools/`'s current
contents post-Task-2-move to confirm it still holds several other
never-relocated dev/analysis scripts, supporting the "not an arbitrary
inconsistency" argument above. `git log --follow -- mic_tools/mic_char_analyze.py`
returns nothing (file has never been committed) — noted honestly rather than
implying history was "preserved" for a file that has none yet.
