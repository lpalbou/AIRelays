# Backlog Overview

## Summary

AIRelays is a local OpenAI-shaped relay with provider-scoped runtimes. The provider-runtime expansion is complete and released: the ChatGPT subscription path is the primary runtime (with balanced multi-account routing) and the Claude runtime is a first-class local adapter as of 0.4.0. Open work is the cross-platform runtime track (ADR 0008) plus proposed hardening follow-ups.

## Counts

- Planned: 3
- Proposed: 4
- Completed: 11
- Deprecated: 1
- Recurrent: 0

## Priority

Planned track [cross-platform runtime](planned/platform/README.md) (ADR 0008): 0022 relay lifecycle and single-instance tray (P1), then 0023 headless service install and 0024 signed installers (P2). Proposed items 0014 (wall-clock bench expiry) and 0015 (Claude token shape validation) are low-urgency hardening. Proposed item 0018 (metered embeddings capability slice) is a deliberately uncertain product-boundary idea gated on a new ADR amending ADR 0002 — do not implement without promotion.

## Planned Tracks

- [Cross-platform runtime](planned/platform/README.md)

## Planned Items

- [0022_relay_lifecycle_and_single_instance_tray.md](planned/platform/0022_relay_lifecycle_and_single_instance_tray.md)
- [0023_headless_service_install.md](planned/platform/0023_headless_service_install.md)
- [0024_signed_installers.md](planned/platform/0024_signed_installers.md)

## Proposed Work

- [Multi-account pool](proposed/accounts/README.md)
  - [0014_wall_clock_bench_expiry_across_system_sleep.md](proposed/accounts/0014_wall_clock_bench_expiry_across_system_sleep.md)
  - [0021_keyring_sign_in_import.md](proposed/accounts/0021_keyring_sign_in_import.md) — optional import of keyring-stored OpenAI sign-ins; deferred by the owner (ADR 0008).
- [Provider runtimes](proposed/providers/README.md)
  - [0015_claude_token_shape_validation_on_set_token.md](proposed/providers/0015_claude_token_shape_validation_on_set_token.md)
  - [0018_metered_embeddings_capability_slice.md](proposed/providers/0018_metered_embeddings_capability_slice.md) — single opt-in API key for `/v1/embeddings` only; both subscriptions verifiably cannot produce embeddings. Value uncertain (possibly reduces to key-management centralization, a double-edged trade); promotion requires a new ADR amending ADR 0002, a demonstrated recurring need, and explicit rejection of the local/direct/sidecar alternatives.

## Completed Work

- [Cross-platform runtime](completed/platform/README.md)
  - [0025_uniform_cross_platform_runtime_first_slice.md](completed/platform/0025_uniform_cross_platform_runtime_first_slice.md)
- [Provider runtimes](completed/providers/README.md)
  - [0001_provider_runtime_registry_for_subscription_relays.md](completed/providers/0001_provider_runtime_registry_for_subscription_relays.md)
  - [0010_experimental_claude_subscription_cli_adapter.md](completed/providers/0010_experimental_claude_subscription_cli_adapter.md)
  - [0011_claude_runtime_mainline_graduation.md](completed/providers/0011_claude_runtime_mainline_graduation.md)
  - [0026_chat_completions_usage_contract.md](completed/providers/0026_chat_completions_usage_contract.md)
- [Multi-account pool](completed/accounts/README.md)
  - [0012_balanced_multi_account_routing_and_pool_hardening.md](completed/accounts/0012_balanced_multi_account_routing_and_pool_hardening.md)
  - [0013_openai_usage_probe_caching_and_single_flight.md](completed/accounts/0013_openai_usage_probe_caching_and_single_flight.md)
  - [0016_capacity_aware_balanced_routing.md](completed/accounts/0016_capacity_aware_balanced_routing.md)
  - [0019_longest_window_balancing_and_weekly_tally.md](completed/accounts/0019_longest_window_balancing_and_weekly_tally.md)
  - [0020_relay_owned_claude_accounts.md](completed/accounts/0020_relay_owned_claude_accounts.md)

| ID | Item | Original Path | Final Path | Completed | Outcome | Notes | Validation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0001 | Provider runtime registry for subscription relays | `docs/backlog/planned/providers/0001_provider_runtime_registry_for_subscription_relays.md` | `docs/backlog/completed/providers/0001_provider_runtime_registry_for_subscription_relays.md` | 2026-06-21 | Completed | Added provider runtime registry, model metadata, and provider-aware readiness. | `pytest -q`; `python -m compileall src tests`; `mkdocs build -q`; live `/v1/models` and `/v1/relay/status` checks |
| 0010 | Experimental Claude subscription CLI adapter | `docs/backlog/planned/providers/0010_experimental_claude_subscription_cli_adapter.md` | `docs/backlog/completed/providers/0010_experimental_claude_subscription_cli_adapter.md` | 2026-06-21 | Completed | Added local-only experimental Claude text runtime with explicit guardrails and docs. | `pytest -q`; `python -m compileall src tests`; `mkdocs build -q`; live Claude and OpenAI request smoke checks |
| 0011 | Claude runtime mainline graduation | (worked directly; recorded post-completion) | `docs/backlog/completed/providers/0011_claude_runtime_mainline_graduation.md` | 2026-07-10 | Completed | Sampling adaptation, merge to main (PR #1), label removal, personal-use disclaimer, 0.4.0 release with desktop installers. | `pytest -q`; live per-model verification 13/13; release workflow green (PyPI, GitHub Release, docs) |
| 0012 | Balanced multi-account routing and pool hardening | (worked directly; recorded post-completion) | `docs/backlog/completed/accounts/0012_balanced_multi_account_routing_and_pool_hardening.md` | 2026-07-11 | Completed | Round-robin default, evidence-gated benching, failover classification, launch-time warm-up; desktop exposes the balance setting. | `pytest -q` (165); live balanced-selection, failover, refresh, and warm-start checks against real accounts |
| 0013 | OpenAI usage-probe caching and single-flight | `docs/backlog/proposed/accounts/0013_openai_usage_probe_caching_and_single_flight.md` | `docs/backlog/completed/accounts/0013_openai_usage_probe_caching_and_single_flight.md` | 2026-07-11 | Completed | 60s TTL cache, single-flight lock, force-bypass for manual refresh, 300s background refresher feeding proactive benching. | `pytest -q` (168) incl. cache/coalesce tests |
| 0016 | Capacity-aware balanced routing | (worked directly; recorded post-completion) | `docs/backlog/completed/accounts/0016_capacity_aware_balanced_routing.md` | 2026-07-11 | Completed | New "balanced" default routes by remaining short-window quota so plans of different sizes deplete proportionally; round_robin/ordered stay as opt-ins. | `pytest -q` (168); log-verified 199/199 request parity motivating the change; live convergence check after install |
| 0019 | Longest-window balancing and weekly token tally | (worked directly; recorded post-completion) | `docs/backlog/completed/accounts/0019_longest_window_balancing_and_weekly_tally.md` | 2026-07-28 | Completed | Windows identified by duration after the upstream payload became plan-dependent; balancing and the "more" token tally key on each account's longest (weekly) window; fabricated idle 5h row removed. | Two-agent adversarial pass; `pytest -q` (208, 15 new); Playwright mock render; live check after install |
| 0020 | Relay-owned Claude accounts and CLI-sourced usage | (worked directly; recorded post-completion) | `docs/backlog/completed/accounts/0020_relay_owned_claude_accounts.md` | 2026-10-10 | Completed | Claude routes only through AIRelays-owned profiles and an optional inference-only setup token; the personal Claude Code sign-in is never used or signed out; usage comes from Claude Code's `get_usage` with stale readings marked and excluded from routing (ADR 0007). | `pytest` 417 passed; desktop browser, node and `cargo check` green; live `get_usage` check against a real profile |
| 0025 | Uniform cross-platform runtime, first slice | (worked directly; recorded post-completion) | `docs/backlog/completed/platform/0025_uniform_cross_platform_runtime_first_slice.md` | 2026-10-11 | Completed | File-only private credentials with a cross-process refresh lock, one `claude` launcher, isolated bundled interpreter, valid macOS signing, tests on three OSes (ADR 0008). | Three per-OS adversarial reviews; `pytest` 444 passed on 3.11–3.13; `cargo test`; desktop browser tests; MkDocs/build/twine green |
| 0026 | Chat Completions usage contract and OpenAI conformance | (worked directly; recorded post-completion) | `docs/backlog/completed/providers/0026_chat_completions_usage_contract.md` | 2026-10-11 | Completed | Chat and completions report OpenAI's CompletionUsage on both runtimes (Claude counts cached prompt tokens); spec-shaped streamed usage; `/v1/responses` unchanged. | Three adversarial reviews; 85 contract/conformance tests; `pytest` 529 on 3.11–3.13; live schema validation (0 failures) and openai-python 3.29.0 checks |
| 0027 | Images, files, conversations, `/v1/responses` and tool calls on Claude models | `docs/backlog/planned/providers/0027_images_and_claude_tools.md` | `docs/backlog/completed/providers/0027_images_and_claude_tools.md` | 2026-10-15 | Completed | Claude models gain the GPT feature set on the same endpoint; tool calls are returned to the client and never run. | Two adversarial audits; `pytest` 605 on 3.11–3.13; live SDK 28/28 and openai-openapi validation (0 failures) |

## Deprecated Work

- [0017_account_capacity_estimation.md](deprecated/accounts/0017_account_capacity_estimation.md): absolute token-capacity estimation rejected after adversarial investigation — the upstream quota is credit-denominated and mix-dependent, large plans are quantization-bound, and ADR 0001 forbids invented budgets; percent equalization already routes optimally without it.

## Process

- Add new planned work under `docs/backlog/planned/` with the next global numeric prefix.
- Keep uncertain follow-ups under `docs/backlog/proposed/`; promote to planned only with evidence.
- Move finished work to `docs/backlog/completed/` with its completion report and validation.
- Move superseded or rejected work to `docs/backlog/deprecated/` with the reason.
- Re-check related ADRs before implementing boundary or policy changes.
