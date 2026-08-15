---
id: ADR-041
title: Provisioning AP default reverted to open (no password), WPA2-PSK kept as an opt-in build flag
status: accepted
date: 2026-08-15
deciders: Abhinav Krishna N
---

## Context

`docs/decisions/ADR-031-provisioning-ap-random-per-device-password.md`
decided the provisioning AP (`EPM-SAT-<node_id>`) should default to
WPA2-PSK with a true-random, NVS-persisted per-device password, rejecting
the reference firmware's open-AP design on the grounds that this project's
deployment context might not stay small enough for an open AP's brief
join-window risk to be acceptable.

That reasoning hasn't changed and isn't being relitigated here. What's
changed is a judgment call about the actual deployment context this
project ships into: a small fleet, physically supervised bring-up
(contest/demo use), where the operator provisioning a unit is standing
next to it. In that context, a WPA2 password prompt is pure onboarding
friction with no realistic attacker it's stopping — nobody else is in RF
range of a unit mid-provisioning at a demo table. The project owner
(Abhinav) decided this friction isn't worth it and the AP should be open
by default, no password prompt, phone joins and the captive portal opens
immediately, out of the box, with no build flag required to get there.

This ADR exists to make that reversal explicit and dated, rather than
letting it happen as a silent, undocumented flip of ADR-031's default —
see that ADR's own addendum, added alongside this one, pointing here.

## Options considered

### Option A: keep ADR-031's WPA2-by-default, no toggle at all
Leaves the AP exactly as ADR-031 shipped it: WPA2-PSK, random per-device
password, always.

Rejected: doesn't solve the actual problem. The friction ADR-031's default
now imposes on every onboarding — a password the operator has to go read
off the serial console before a phone can join — is exactly what's being
asked to go away. Keeping Option A with no escape hatch either accepts
that friction forever or forces a future undocumented workaround.

### Option B: compile-time flag, defaulting to WPA2-on, open as opt-in
A toggle exists, but its default value still matches ADR-031 — WPA2-PSK
unless a build explicitly passes `-DEPM_PROVISIONING_AP_OPEN=1`.

Rejected as the *default*: this still ships every ordinary build with the
password prompt, since nobody rebuilds with a non-default flag just to get
the behavior they actually want out of the box. It would technically make
open-AP mode reachable, but defeats the actual goal — frictionless
onboarding by default — by making the wanted behavior the one every build
has to remember to opt into. (This option remains available, functionally
identical, as this ADR's flag set to its *off* position — see Decision.)

### Option C: compile-time flag, defaulting to open, WPA2 as opt-in (chosen)
The provisioning AP is open (`WIFI_AUTH_OPEN`, no password) unless a build
explicitly sets `EPM_PROVISIONING_AP_OPEN=0`, which restores ADR-031's
exact original WPA2-PSK / random-password behavior. `ap_credentials_get_or_create()`
is not called at all in the open path — no reason to generate or touch the
NVS-persisted password if it's never used.

Chosen because it's the only option that actually delivers frictionless
onboarding out of the box, while keeping ADR-031's protection one rebuild
away rather than deleting it outright — a future deployment context
(unsupervised, larger fleet, hostile RF environment) can restore it with a
single build flag, no code changes.

## Decision

**Option C.** The flag lives driver-private in
`components/epm_drivers/provisioning.c`, not in `src/epm_config.h` — see
that file's own header comment on `EPM_PROVISIONING_AP_OPEN`. It follows
the same `#ifndef`-guarded, `platformio.ini` `build_flags`-overridable
style `EPM_MQTT_BROKER_HOST`/`PORT` already established in `link_mqtt.c`,
for the same reason: `epm_drivers` must not depend back on the main
component's config header (`src` already depends on `epm_drivers`; the
reverse would be a component dependency cycle).

`EPM_PROVISIONING_AP_OPEN` defaults to `1` (open). `hal_provisioning_start()`
sets `ap_cfg.ap.authmode = WIFI_AUTH_OPEN` and skips
`ap_credentials_get_or_create()` and the password buffer entirely when the
flag is `1`; it falls back to ADR-031's original WPA2-PSK path, unchanged,
when the flag is explicitly set to `0`. The open-AP bring-up line logs at
`ESP_LOGW`, not `ESP_LOGI`, specifically so it reads as a visually distinct
"reduced-security build" signal in the serial log rather than blending into
the normal startup noise.

## Consequences

**Positive:**
- Onboarding friction is gone by default: a phone joins the AP with no
  password prompt and the captive portal opens immediately, matching the
  reference firmware's own original open-AP design for the same reasoning
  ADR-031 quoted from it ("a deliberate, deployment-scale simplification
  for a transient, physically-supervised onboarding step").
- ADR-031's protection is not deleted, only demoted to opt-in: a single
  rebuild with `EPM_PROVISIONING_AP_OPEN=0` restores it exactly, with no
  code changes, for any future deployment context where the tradeoff below
  stops being acceptable.
- No wasted work in the open path: the NVS-persisted password is never
  generated or read when it'll never be used, so first boot into
  provisioning mode with this flag doesn't perform a needless
  `esp_fill_random()` + NVS write.

**Negative / trade-offs (restated, not softened, from ADR-031's originally
*rejected* Option A — now the accepted default):**
- Any phone in RF range of a unit that is currently in provisioning mode
  can join its AP and reach the onboarding page, with no password barrier,
  on every unit, unless someone deliberately rebuilds with
  `EPM_PROVISIONING_AP_OPEN=0`. This is exactly the risk ADR-031 rejected
  Option A over. This ADR does not reduce or mitigate that risk in any
  way — it accepts it as the default tradeoff for this project's actual
  deployment context (small fleet, physically supervised bring-up,
  contest/demo use), on the project owner's explicit call that the
  friction of a password prompt isn't worth it there.
- An unrelated device in range during the provisioning window could still
  join, occupy the AP's connection slot, or submit garbage credentials
  through the portal — the exact scenario ADR-031's Context section
  described as the reason Option A was rejected there. Nothing about that
  scenario's mechanics has changed; only the judgment about whether it
  matters for this project's context has.
- This tradeoff does not travel with the firmware to a different
  deployment context automatically — if this project (or a fork of it)
  is ever used unsupervised, at larger fleet scale, or in an RF
  environment with untrusted nearby devices, `EPM_PROVISIONING_AP_OPEN`
  must be deliberately set back to `0` at build time. Nothing in the
  running firmware detects or warns about that context change; it's a
  build-time human judgment call, same as it was under ADR-031.

## Validation

Hardware-tested both build configurations on the real XIAO ESP32-S3 board:

- **Default (`EPM_PROVISIONING_AP_OPEN=1`, no flag needed):** AP showed as
  open (no lock icon) in a phone's WiFi picker, phone joined with no
  password prompt, and the captive-portal onboarding page auto-opened
  immediately. Serial log showed the new `ESP_LOGW` open-AP line, not the
  old password line. Confirmed by inspection that
  `ap_credentials_get_or_create()` is not called in this path (compiled out
  under `#if !EPM_PROVISIONING_AP_OPEN`) — no NVS write for the AP password
  occurs when this flag is on.
- **Opt-out (`-DEPM_PROVISIONING_AP_OPEN=0`):** restored ADR-031's exact
  original behavior — AP came up as WPA2-PSK with the existing
  `ESP_LOGI` password line, and a phone needed that password to join.
- Host test suite (`tests/host/`) and `pytest` suite pass unmodified —
  neither covers this ESP-IDF/Wi-Fi-stack-dependent code path.
