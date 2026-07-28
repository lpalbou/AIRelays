# Completed: Longest-window balancing and weekly token tally

## Metadata

- Created: 2026-07-28
- Status: Completed
- Completed: 2026-07-28

## Context

The upstream usage payload became plan-dependent (observed live 2026-07-28
in traffic logs): a Plus account reports its weekly window alone in
`rate_limit.primary_window` (the plan has no 5h window anymore), while an
Enterprise account keeps the classic 5h primary plus a weekly secondary.
Item 0016's balanced strategy read the capacity signal from the primary
slot, which now mixed a weekly percentage against a 5h percentage —
incomparable quantities that, early in a fresh week, would route sustained
traffic into the small plan's scarce weekly budget. The per-account token
tally was anchored to the primary window's `reset_at`; for accounts still
reporting a 5h primary, that wiped the breakdown every bucket roll, leaving
the desktop's "more" panel permanently empty (confirmed:
`openai-window-tokens.json` held `"models": {}` for both accounts despite
754 completed requests / 8.2M tokens served that day). The desktop also
synthesized an idle "5h window" row whenever only long windows were
present — a fabrication for plans that no longer have a 5h window.

Owner-reported symptom for the record: the Plus weekly bar at 91% with the
Enterprise at 4%. Traffic-log analysis showed the relay itself routed 97%
of that day's requests to the Enterprise account (23 vs 754), so the burn
was mostly external to the relay — but the signal mixing, the wiped tally,
and the fabricated idle row were all real defects.

## What we did

- New `airelay/usage_windows.py`: typed parsing of `rate_limit` windows
  (`UsageWindow`, `parse_rate_limit_windows`, `longest_window`) plus the
  shared `window_label`. Windows are identified by `limit_window_seconds`,
  never by slot position.
- Balanced routing signal = used_percent of each account's longest-horizon
  window (the weekly budget every current plan shape shares). Short-window
  exhaustion stays covered by proactive benching (any window >= 100% still
  benches until the longest exhausted window resets) and reactive 429
  failover. Integer bucketing, least-recently-selected ties, 15-minute
  signal max-age, sticky conversation affinity all unchanged. A payload
  without windows updates nothing (signal ages out; tally keeps its
  window).
- Token tally anchored to the longest window's `reset_at`, with
  `window_seconds`/`window_label` persisted and exposed on the
  `window_tokens` snapshot (`scope` is now
  `current_usage_window_via_this_relay`). STATE_VERSION stayed 1: the
  schema change is additive and old 5h-anchored entries self-correct on the
  first probe.
- Desktop: only reported windows render (idle-row synthesis removed); the
  "more" panel title names the tally window (e.g. "This weekly window, via
  this relay"); mock mode carries the two real plan shapes plus an
  exhausted at-limit account.
- `pytest` `testpaths` hygiene so a bare run no longer crawls into
  gitignored desktop runtime bundles.

## Validation

- Two-agent adversarial pass (implementer + independent adversarial
  reviewer): reviewer approved with no blocking/major findings after
  driving `_bench_from_usage`, the tally, and the parser with both live
  payload shapes and adversarial ones (missing durations, bool/string
  percents, equal durations, windowless payloads, upgrade paths).
- `pytest -q` — 208 passed (15 new tests: real-shape signal extraction,
  mixed-pool routing, rotation fallback, tally anchor semantics across 5h
  rolls and weekly rolls, snapshot label, parser unit tests,
  pre-upgrade state-file compatibility).
- Playwright render of mock mode: Plus card shows exactly one Weekly bar,
  Enterprise shows 5h + Weekly, at-limit badge renders, "more" panel
  populated under the weekly title, no horizontal overflow.
- Live verification after install on the production relay (real accounts).

## Residual risks / follow-ups

- A transient payload that drops the weekly window while keeping a 5h one
  would re-anchor the tally destructively (clear on the way out and back).
  No such shape observed live; hardening would require anchor changes from
  a shorter window to persist across two consecutive probes.
- Balancing compares weekly percentages across plans whose absolute weekly
  quotas differ; that is the intended definition of balanced charge
  (percentage of each plan's own capacity), not a defect.
- Item 0014 (wall-clock bench expiry across system sleep) remains proposed
  and now also affects multi-day weekly benches.
