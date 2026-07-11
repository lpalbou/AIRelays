# Completed: OpenAI usage-probe caching and single-flight

## Metadata

- Created: 2026-07-11
- Status: Completed
- Completed: 2026-07-11

## Summary

Add a short per-account TTL cache and single-flight to the OpenAI subscription-usage path, mirroring the guardrails the Claude usage path already has (cache, fetch lock, attempt spacing, retry-after handling).

## Reason

`/v1/subscription/status` is public relay API and each call hits the upstream usage endpoint once per account with no protection. Current desktop cadence is trivial (fetches on mount/refresh only), but any client scripting the route at a poll cadence would generate thousands of upstream hits per hour per account, plausibly triggering upstream throttling on a personal account. Cheap probes would also make proactive benching affordable at higher frequency.

## Current code reality

`ChatGptCodexBackend.get_subscription_status` (src/airelay/backend.py) fetches unconditionally; `OpenAiAccountPool.subscription_statuses` (src/airelay/accounts.py) loops accounts per call. Compare `ClaudeCliRuntime` usage guardrails in src/airelay/providers.py.

## Scope

- 15–30s per-account TTL cache with single-flight in the pool
- bypass or short-circuit knob for the refresh action (which wants fresh evidence)
- keep `_bench_from_usage` semantics unchanged (probe-start gating already in place)

## Validation

- unit tests for cache hit/miss/single-flight and refresh bypass
- confirm the desktop Accounts card still shows fresh data after Refresh

## Completion report

Implemented alongside capacity-aware balanced routing (item 0016), which
needed exactly this: `_probe_usage` adds a 60s per-account TTL cache,
`subscription_statuses` is single-flighted behind an asyncio lock and
takes `force=True` for the manual refresh and launch warm-up paths, and a
background `usage_refresh_loop` (300s cadence, multi-account pools only)
keeps the capacity signal fresh — ~12 probes/hour/account. Every probe
also feeds `_bench_from_usage`, so proactive limit-benching now happens
continuously instead of only on manual status loads. Validated by
`test_usage_probes_are_cached_and_coalesced` plus the existing refresh
and warm-up tests (168 passing).
