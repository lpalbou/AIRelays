# Multi-account pool backlog track

## Status

Completed (initial hardening slice)

## Purpose

These items record how the OpenAI multi-account pool became a balanced,
evidence-driven router: charge spreads across every account with capacity
by default, exhausted accounts are benched and released on fresh usage
evidence, and account-scoped failures fail over instead of failing the
request.

## Completed Items

- [0012_balanced_multi_account_routing_and_pool_hardening.md](0012_balanced_multi_account_routing_and_pool_hardening.md): round-robin default, evidence-gated benching, failover classification, launch-time warm-up.
- [0013_openai_usage_probe_caching_and_single_flight.md](0013_openai_usage_probe_caching_and_single_flight.md): TTL cache, single-flight, and a background refresher for the usage probes.
- [0016_capacity_aware_balanced_routing.md](0016_capacity_aware_balanced_routing.md): the "balanced" default — route by remaining short-window quota so plans of different sizes deplete proportionally.

## Related Material

- [Architecture](../../../architecture.md) (account pool lifecycle diagram)
- [Configuration](../../../configuration.md) (`[providers.openai] balance`)
- [FAQ](../../../faq.md) (account balancing entry)
