# Engineering conventions playbook

What made this project read as professional, debuggable, and easy for a
collaborator to pick up cold. Written to be reused on the next project
(EleTect / EleTect X), not specific to this one.

## Decision records (ADRs)

Every non-trivial design choice gets its own dated, numbered file:
Context → Options considered (with real pros/cons, not strawmen) → Decision
→ Consequences → Validation. Two rules that make this actually useful
instead of ceremony:

- **Append-only.** Never rewrite a past decision. If it turns out wrong,
  add a dated addendum explaining what changed and why — the original
  reasoning stays visible, which matters when you're debugging "why is it
  built this way" six weeks later.
- **A real "Validation" section, honestly filled in.** State what was
  actually confirmed on real hardware vs. what's still assumed. "Not
  confirmed: X" is more valuable than silence, and far more valuable than
  a false "done."

This single habit is the highest-leverage one on this list — it's the
difference between a codebase you can debug and one where every choice is
an archaeology project.

## Single source of truth + generated code

Anything two sides both need to agree on (a wire format, a shared schema)
gets defined **once**, in a schema file, with both consumers generated from
it — never hand-edited independently. Two components that drift out of sync
is one of the most common and hardest-to-spot classes of bug; codegen makes
drift a build-time impossibility instead of a runtime mystery.

## Self-describing data over hardcoded assumptions

Put lengths, counts, and versions **in the data itself** (a per-message
field), not baked into a fixed constant on the receiving side. It means one
side can change (more sensor channels, higher resolution) without a
coordinated two-sided deploy — the receiver just reads what's actually
there.

## Numbers over adjectives

Every performance or reliability claim is a measured number with the
measurement conditions stated ("~152s self-heal, measured over N real
reconnect cycles"), not "fast" or "should recover quickly." Vague claims
can't be debugged against; a number can be re-measured and checked.

## Flag gaps, don't quietly work around them

When something doesn't do what an earlier doc assumed (a config field that's
collected but never read, a sensor that doesn't behave as planned), write
that down plainly as a known gap, in the same place someone would look for
the feature — not fixed silently, not hidden, not glossed over. A
collaborator finding out from a doc is fine; finding out by debugging a
mystery for an hour is not.

## One canonical map per cross-cutting concern

Pin/GPIO assignments live in exactly one file, with the rationale for each
choice, and code comments point back to it rather than re-explaining
inline. Same principle for anything else that's referenced from many
places (config knobs, topic/API names) — one authority, everything else
cites it.

## Setup docs written for a stranger

A bring-up guide assumes zero context: staged (one module at a time, not
"wire everything then pray"), a checkpoint after each stage stating exactly
what you should see, a troubleshooting table keyed by symptom, and a
reference card at the end for once you already know it. Someone should be
able to follow it without ever asking you a question.

## Commit hygiene

One concern per commit, descriptive messages, no tooling/attribution noise.
A clean `git log` is itself documentation — it tells a reviewer the shape
of the work without opening a diff.

## Integrating into someone else's project

Match *their* conventions (naming, tone, structure), not your own — the
goal is a diff that reads like it was always part of their repo. Before any
merge/PR, explicitly verify the diff is scoped to only what you intended
(`git diff --stat` against their main) — easy to accidentally touch
something adjacent when copying a large chunk of work across repos.

## HAL abstraction + deliberate task/priority design

Yes — this is genuinely good, worth carrying over. Two related habits:

- **A thin hardware-abstraction interface between "what the driver is" and
  "what the code does with it."** `hal_display.h`/`hal_accel.h`-style
  contracts mean the business logic calls a stable interface, and the real
  driver underneath is swappable (real chip vs. a stub, one chip family vs.
  another) via a single config switch — same pattern as the fallback point
  above, and it's also what makes host-side unit testing possible without
  real hardware attached.
- **One task per real concern, with priority/core chosen for a reason and
  written down, not left at defaults.** Sampling, DSP/compute, networking,
  display, and background diagnostics each get their own FreeRTOS task
  rather than one big loop — and the priority numbers aren't arbitrary: the
  latency-sensitive DSP task sits above routine work, sensor sampling is
  pinned below the networking task specifically so it can never starve the
  radio stack (documented inline at the point of the priority `#define`,
  not just in a doc off to the side), and low-value background work
  (diagnostics logging) sits at the lowest priority so it can never delay
  anything that matters. That's the actual efficiency win — not "more
  threads," but bounded, predictable latency for the things that need it,
  achieved by deliberately ranking work instead of leaving the scheduler to
  sort it out.

The transferable rule: when you split work into tasks, write the *reason*
for each priority/core choice next to the number. A priority value with no
comment is a value nobody can safely change later.

## Fallbacks stay in the tree, cleanly separated

Where there's a "real" implementation and a fallback (different hardware,
degraded mode), keep both, gated by one clear config switch, mutually
exclusive at compile/build time — not runtime branching that has to be
mentally simulated to know which path actually runs.
