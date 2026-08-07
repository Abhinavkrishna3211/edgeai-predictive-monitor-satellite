# CLAUDE.md

Index for AI tools/assistants working in this repo. Full detail lives in the linked
docs — this file is deliberately short and does not duplicate them.

## Commit hygiene

- **Zero AI/tool attribution, anywhere** — no `Co-Authored-By: the AI coding assistant`, no
  "Generated with," no mention in code, comments, commit messages, or docs. Verify
  before every push: `git log --format='%B' | grep -iE 'claude|generated with|co-authored'`
  must return nothing.
- One logical change per commit (`docs/MASTER_PLAN.md` Part H / `docs/CONVENTIONS.md`
  "Git / commit standards") — never mix a file move with a behavior change, or a bug
  fix with a rename.
- Git author identity comes from the local machine's `git config` — nothing to set up
  per-tool or per-session.
- Full naming/error-handling/testing conventions: [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md).

## `docs/MASTER_PLAN.md` — the planning tool-owned

This file's *content* belongs entirely to the planning tool. Never read it for context, edit it,
or reformat it. If `git status` shows it modified before a branch switch, reset,
rebase, or `filter-repo` operation, commit it mechanically, unread, exactly as-is:

```
git add docs/MASTER_PLAN.md && git commit -m "docs: checkpoint MASTER_PLAN.md (content owned by the planning tool, mechanical commit only)"
```

It is also the source of truth for cross-phase project status — read it (without
editing) when you need that context.

## Architecture Decision Records

`docs/decisions/ADR-NNN-*.md`, sequential, append-only. A reversed decision gets a
new, higher-numbered ADR that says so and references the one it supersedes — never
edit or delete a past ADR to make it retroactively "correct."

## Wire protocol

Source of truth: [`docs/BASE_STATION_CONTRACT.md`](docs/BASE_STATION_CONTRACT.md)
(MQTT to Mosquitto, section-list telemetry frames). `README.md` and `ARCHITECTURE.md`
carry a summary but have drifted stale relative to the actual wire format before
(they described a deleted raw-TCP + AES-128-GCM protocol as current as of
2026-08-07) — if they ever disagree with `BASE_STATION_CONTRACT.md` or the code
(`components/epm_codec/`, `gateway/common/`), trust the contract doc and code.

## Background reading for base-station interop work

[`docs/MASTER_PLAN.md`](docs/MASTER_PLAN.md) (cross-phase status, the planning tool-owned —
read only).
