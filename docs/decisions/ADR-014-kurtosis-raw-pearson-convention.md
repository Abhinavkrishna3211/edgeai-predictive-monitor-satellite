---
id: ADR-014
title: Kurtosis wire convention is RAW/Pearson, not excess/Fisher
status: superseded by ADR-018
date: 2026-08-04
deciders: Abhinav Krishna N
---

## Context

`docs/MASTER_PLAN.md` Part G, Phase 1 left an open discrepancy for this
phase to settle: `tests/host/test_scalar_stats.c`'s
`gaussian_kurtosis_raw_convention` test expects kurtosis of Gaussian noise
to read ~3.0 (RAW/Pearson convention: `Σx⁴/N ÷ (Σx²/N)²`), matching
`src/mic_task.c:129-138`'s actual on-device computation exactly. But
`docs/MASTER_PLAN.md` Part D documents the wire-protocol convention as
excess/Fisher (RAW − 3, Gaussian ≈ 0.0) instead — a mismatch between the
firmware's real behavior and the documented contract for the same field.

Part D itself flags this row as an unverified draft ("VERIFY against his
live source... a draft until Phase 4 confirms it"), written by a prior
session against a reference repo (`base-station/python/common/raw_features.py`)
that is not available as a sibling checkout in this environment — there is
no way to re-verify Part D's claim against its original source directly in
this phase.

Evidence gathered from the current codebase instead:

- `src/mic_task.c:129-138` computes RAW/Pearson kurtosis, with a fallback
  default of `3.0f` (`mic_task.c:82`, mirrored again in `dsp_task.c:118`'s
  `last_kurtosis` init) — a fallback value that only makes sense under the
  RAW convention (excess/Fisher's fallback would be `0.0f`).
- `src/threads/net_task.c:64`, the Phase 0.5 synthetic-frame publisher
  (written independently of `mic_task.c`, meant to emit plausible
  healthy-signal telemetry for base-station-side testing), hardcodes its
  kurtosis scalar entry to `3.00f` — not `0.0f`. This is corroborating
  evidence that RAW/Pearson is already the implicit, self-consistent
  convention across the firmware, not just one function's incidental
  choice.
- `tests/host/test_scalar_stats.c`'s `gaussian_kurtosis_raw_convention`
  already expects ~3.0 and already passes against `mic_task.c`'s real
  behavior.

Two firmware call sites and one independent test all agree with each other
and disagree with Part D. The weight of live evidence favors the firmware,
not the draft doc.

## Decision

**Option (a): keep the firmware's RAW/Pearson convention as-is.** No change
to `src/mic_task.c` — it already agrees with `net_task.c` and the existing
passing host test. `docs/MASTER_PLAN.md` Part D is intentionally **not**
hand-edited in this phase: Part D is explicitly out of this phase's file
scope, and Phase 4 is where Part D is formally reconciled into
`docs/BASE_STATION_CONTRACT.md`. This ADR is the input Phase 4 should
reconcile Part D against.

`tests/host/test_scalar_stats.c` needed no logic or expectation change — it
already agreed with `mic_task.c`. Its comment block (mirror-function header)
and the `gaussian_kurtosis_raw_convention` test's `snprintf` detail string
were updated to say the discrepancy is resolved by this ADR, instead of
calling it an "open discrepancy for Phase 2/4."

## Consequence

- The wire/firmware kurtosis convention going forward is RAW/Pearson
  (Gaussian ≈ 3.0), confirmed by this ADR.
- **Flagged for Phase 4**: this decision should be re-confirmed against
  `base-station/python/common/raw_features.py` (or whatever the current
  reference repo's live source is) once that repo is available again — the
  evidence here is strong (two independent firmware call sites agree) but
  was gathered without being able to re-check Part D's original source. If
  the live reference repo turns out to genuinely expect excess/Fisher on
  the wire, that would mean `mic_task.c` needs to change instead — a
  decision Phase 4 is better positioned to make with the live repo in hand.
- No behavior change in this phase: `mic_task.c` is untouched, and
  `test_scalar_stats.c`'s only change is the comment/detail-string update
  described above (already committed in the same commit as this ADR).
