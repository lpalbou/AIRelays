# Proposed: Wall-clock bench expiry across system sleep

## Metadata

- Created: 2026-07-11
- Status: Proposed

## Summary

Store the upstream-reported wall-clock reset time (`reset_at` epoch) alongside the monotonic bench deadline and expire a bench when either clock says the window has reset.

## Reason

Bench deadlines use `time.monotonic()`, which does not advance during system sleep on macOS. A multi-hour bench on a laptop that sleeps overnight persists past the real window reset; until a usage probe or the launch warm-up corrects it, traffic keeps avoiding an account that actually has capacity.

## Current code reality

`_PooledAccount.limited_until` and all bench arithmetic in src/airelay/accounts.py are monotonic-only. The upstream usage payload and 429 bodies carry absolute reset times (`reset_at`, `resets_in_seconds`) that are currently only converted to relative cooldowns. The launch warm-up (`warm_start`) already self-heals this after a restart, which reduces the severity.

## Scope

- add an optional wall-clock expiry to the bench state
- treat "either clock says reset" as recovered
- no persistence to disk (fresh probes remain the source of truth)

## Validation

- unit test simulating a monotonic/wall divergence
