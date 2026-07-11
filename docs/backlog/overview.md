# Backlog Overview

## Summary

AIRelays is a local OpenAI-shaped relay with provider-scoped runtimes. The provider-runtime expansion is complete and released: the ChatGPT subscription path is the primary runtime (with balanced multi-account routing) and the Claude runtime is a first-class local adapter as of 0.4.0. Open work is limited to proposed hardening follow-ups.

## Counts

- Planned: 0
- Proposed: 2
- Completed: 6
- Deprecated: 1
- Recurrent: 0

## Priority

No committed planned items. Remaining proposed items (0014 wall-clock bench expiry, 0015 Claude token shape validation) are low-urgency hardening.

## Planned Tracks

No active planned tracks.

## Planned Items

No active planned items.

## Proposed Work

- [Multi-account pool](proposed/accounts/README.md)
  - [0014_wall_clock_bench_expiry_across_system_sleep.md](proposed/accounts/0014_wall_clock_bench_expiry_across_system_sleep.md)
- [Provider runtimes](proposed/providers/README.md)
  - [0015_claude_token_shape_validation_on_set_token.md](proposed/providers/0015_claude_token_shape_validation_on_set_token.md)

## Completed Work

- [Provider runtimes](completed/providers/README.md)
  - [0001_provider_runtime_registry_for_subscription_relays.md](completed/providers/0001_provider_runtime_registry_for_subscription_relays.md)
  - [0010_experimental_claude_subscription_cli_adapter.md](completed/providers/0010_experimental_claude_subscription_cli_adapter.md)
  - [0011_claude_runtime_mainline_graduation.md](completed/providers/0011_claude_runtime_mainline_graduation.md)
- [Multi-account pool](completed/accounts/README.md)
  - [0012_balanced_multi_account_routing_and_pool_hardening.md](completed/accounts/0012_balanced_multi_account_routing_and_pool_hardening.md)
  - [0013_openai_usage_probe_caching_and_single_flight.md](completed/accounts/0013_openai_usage_probe_caching_and_single_flight.md)
  - [0016_capacity_aware_balanced_routing.md](completed/accounts/0016_capacity_aware_balanced_routing.md)

| ID | Item | Original Path | Final Path | Completed | Outcome | Notes | Validation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0001 | Provider runtime registry for subscription relays | `docs/backlog/planned/providers/0001_provider_runtime_registry_for_subscription_relays.md` | `docs/backlog/completed/providers/0001_provider_runtime_registry_for_subscription_relays.md` | 2026-06-21 | Completed | Added provider runtime registry, model metadata, and provider-aware readiness. | `pytest -q`; `python -m compileall src tests`; `mkdocs build -q`; live `/v1/models` and `/v1/relay/status` checks |
| 0010 | Experimental Claude subscription CLI adapter | `docs/backlog/planned/providers/0010_experimental_claude_subscription_cli_adapter.md` | `docs/backlog/completed/providers/0010_experimental_claude_subscription_cli_adapter.md` | 2026-06-21 | Completed | Added local-only experimental Claude text runtime with explicit guardrails and docs. | `pytest -q`; `python -m compileall src tests`; `mkdocs build -q`; live Claude and OpenAI request smoke checks |
| 0011 | Claude runtime mainline graduation | (worked directly; recorded post-completion) | `docs/backlog/completed/providers/0011_claude_runtime_mainline_graduation.md` | 2026-07-10 | Completed | Sampling adaptation, merge to main (PR #1), label removal, personal-use disclaimer, 0.4.0 release with desktop installers. | `pytest -q`; live per-model verification 13/13; release workflow green (PyPI, GitHub Release, docs) |
| 0012 | Balanced multi-account routing and pool hardening | (worked directly; recorded post-completion) | `docs/backlog/completed/accounts/0012_balanced_multi_account_routing_and_pool_hardening.md` | 2026-07-11 | Completed | Round-robin default, evidence-gated benching, failover classification, launch-time warm-up; desktop exposes the balance setting. | `pytest -q` (165); live balanced-selection, failover, refresh, and warm-start checks against real accounts |
| 0013 | OpenAI usage-probe caching and single-flight | `docs/backlog/proposed/accounts/0013_openai_usage_probe_caching_and_single_flight.md` | `docs/backlog/completed/accounts/0013_openai_usage_probe_caching_and_single_flight.md` | 2026-07-11 | Completed | 60s TTL cache, single-flight lock, force-bypass for manual refresh, 300s background refresher feeding proactive benching. | `pytest -q` (168) incl. cache/coalesce tests |
| 0016 | Capacity-aware balanced routing | (worked directly; recorded post-completion) | `docs/backlog/completed/accounts/0016_capacity_aware_balanced_routing.md` | 2026-07-11 | Completed | New "balanced" default routes by remaining short-window quota so plans of different sizes deplete proportionally; round_robin/ordered stay as opt-ins. | `pytest -q` (168); log-verified 199/199 request parity motivating the change; live convergence check after install |

## Deprecated Work

- [0017_account_capacity_estimation.md](deprecated/accounts/0017_account_capacity_estimation.md): absolute token-capacity estimation rejected after adversarial investigation — the upstream quota is credit-denominated and mix-dependent, large plans are quantization-bound, and ADR 0001 forbids invented budgets; percent equalization already routes optimally without it.

## Process

- Add new planned work under `docs/backlog/planned/` with the next global numeric prefix.
- Keep uncertain follow-ups under `docs/backlog/proposed/`; promote to planned only with evidence.
- Move finished work to `docs/backlog/completed/` with its completion report and validation.
- Move superseded or rejected work to `docs/backlog/deprecated/` with the reason.
- Re-check related ADRs before implementing boundary or policy changes.
