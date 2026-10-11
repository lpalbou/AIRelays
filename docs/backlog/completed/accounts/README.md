# Multi-account pool backlog track

## Status

Completed (initial hardening slice)

## Purpose

These items record how the OpenAI multi-account pool became a balanced,
evidence-driven router: charge spreads across every account with capacity
by default, exhausted accounts are benched and released on fresh usage
evidence, and account-scoped failures fail over instead of failing the
request. Item 0020 records which accounts the Claude pool may
use and where its usage readings come from.

## Completed Items

- [0012_balanced_multi_account_routing_and_pool_hardening.md](0012_balanced_multi_account_routing_and_pool_hardening.md): round-robin default, evidence-gated benching, failover classification, launch-time warm-up.
- [0013_openai_usage_probe_caching_and_single_flight.md](0013_openai_usage_probe_caching_and_single_flight.md): TTL cache, single-flight, and a background refresher for the usage probes.
- [0016_capacity_aware_balanced_routing.md](0016_capacity_aware_balanced_routing.md): the "balanced" default — route by remaining short-window quota so plans of different sizes deplete proportionally.
- [0019_longest_window_balancing_and_weekly_tally.md](0019_longest_window_balancing_and_weekly_tally.md): windows identified by duration, balancing and the token tally keyed to each account's longest (weekly) window after the upstream payload became plan-dependent.
- [0020_relay_owned_claude_accounts.md](0020_relay_owned_claude_accounts.md): Claude routes only through AIRelays-owned profiles and an optional setup token, never the personal Claude Code sign-in; usage comes from Claude Code's `get_usage`, with stale readings marked and never used for routing.

## Related Material

- [Architecture](../../../architecture.md) (account pool lifecycle diagram)
- [Configuration](../../../configuration.md) (`[providers.openai] balance`)
- [FAQ](../../../faq.md) (account balancing entry)
- [ADR 0007](../../../adr/0007-relay-owned-claude-accounts.md) (relay-owned Claude accounts)
