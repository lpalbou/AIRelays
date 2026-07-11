# Completed: Balanced multi-account routing and pool hardening

## Metadata

- Created: 2026-07-11
- Status: Completed
- Completed: 2026-07-11
- Version: unreleased at completion (targeting the next minor release)

## ADR status

- Governing ADRs: 0001 (no silent degradation), 0003 (local security defaults)
- ADR impact: none required — routing strategy is configuration, not durable policy

## Context

With two enrolled OpenAI accounts, production traffic logs showed whole hours in which one account received every request while the other idled, and a saturated account receiving live traffic again right after the desktop Refresh action. An adversarial three-track investigation of the code and the JSONL traffic logs established two root causes: the `ordered` default strategy (which the desktop-rendered config could not override — it never wrote a `balance` key), and the refresh path clearing every usage-limit bench before re-probing, opening a window in which the ordered picker routed to the known-exhausted account (observed twice, each earning an extra 429).

## What we did

- Default `balance = "round_robin"`, implemented as least-recently-selected (fair under membership churn, unlike a shared modulo counter over a changing list). `"ordered"` remains opt-in. Values are normalized and validated at startup.
- The desktop app writes the `balance` key into the rendered config, exposes it in Settings, and its Accounts card caption/badges reflect the actual mode.
- Evidence-gated bench lifecycle: the refresh action re-probes first and releases only accounts whose fresh usage shows capacity; a stale snapshot cannot erase a newer bench; benching reads every reached-limit signal in the payload and waits for the longest exhausted window.
- Failover classification covers account-scoped failures beyond HTTP errors: transport failures become structured 502s, dead credentials (AuthenticationError, persistent 401) bench and fail over, the last attempt is benched too, fallbacks try healthy accounts first, and usage-limit markers are only trusted structured or on a real 429.
- Robustness: benches survive pool reloads, retired HTTP clients are closed, sticky-map eviction replaces the all-at-once wipe, transient cooldowns cannot truncate longer benches, `account_selected` records carry an attempt index, and the refresh action writes an `accounts_refresh` record.
- Launch-time warm-up: a multi-account pool probes each account's usage and model catalog in the background at startup (`account_pool_warmed` record), so balancing and model-aware routing are correct from the first request.

## Validation

- `pytest -q` — 165 passed (19 new pool/config tests covering balancing fairness, evidence-gated releases, failover classes, marker scoping, warm-up)
- Live verification on a spare port against real accounts: balanced selection across both accounts, transparent failover on a real 429, evidence-gated refresh keeping the exhausted account benched, and a fresh process benching the at-limit account at launch with zero upstream errors

## Residual risks / follow-ups

- OpenAI usage probes have no cache/single-flight; see proposed item 0013.
- Benches use the monotonic clock and freeze across system sleep; see proposed item 0014.
- An in-stream upstream failure event (HTTP 200 + `response.failed`) does not trigger failover; latent, never observed in logs.
