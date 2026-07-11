# Completed: Capacity-aware balanced routing

## Metadata

- Created: 2026-07-11
- Status: Completed
- Completed: 2026-07-11

## Context

After the round-robin default shipped, live usage showed the intended limit: request counts were exactly equal (verified in traffic logs: 199/199 completed requests, ~1.0M tokens each side over two hours), yet the Plus account's 5h window climbed to 25% while the Enterprise account sat at 1% — equal request counts drain a small plan many times faster than a large one. The owner's requirement is balanced charge, and charge is quota consumption, not request count.

## What we did

- New default strategy `balance = "balanced"`: each request routes to the healthy, model-capable account with the lowest short-window `used_percent` (integer-bucketed to avoid ping-pong, ties rotate least-recently-selected), so consumption equalizes as a percentage of each plan's own capacity.
- The capacity signal comes from the existing usage probes: recorded on every probe by `_bench_from_usage`, trusted for routing for up to 15 minutes, with fair rotation as the fallback when the signal is missing or stale.
- Probe infrastructure from item 0013 (TTL cache, single-flight, background refresher) keeps the signal fresh at a bounded upstream cost.
- `round_robin` (strictly equal counts) and `ordered` (spillover) remain as explicit opt-ins; config validation, desktop settings select, Accounts-card captions, and CLI summaries describe all three.

## Validation

- `pytest -q` — 168 passed, including: balanced mode routes everything to the lower-used account, stale signals fall back to rotation, default-config behavior, and cache/single-flight semantics
- Live verification on the production relay after install: traffic shifts to the Enterprise account while its used_percent trails the Plus account's

## Residual risks / follow-ups

- `used_percent` reflects the upstream's own accounting granularity (integer percent); very small plans may still step in visible increments.
- Wall-clock bench expiry across system sleep remains proposed (item 0014).
