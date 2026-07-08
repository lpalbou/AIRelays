# Backlog Overview

## Summary

AIRelays is a local OpenAI-shaped relay with a provider-scoped runtime registry. AIRelays serves the ChatGPT subscription path as its runtime.

## Counts

- Planned: 0
- Proposed: 0
- Completed: 1
- Deprecated: 0
- Recurrent: 0

## Priority

No active planned backlog items are open in the provider-runtime track.

## Planned Tracks

No active planned tracks.

## Planned Items

No active planned items.

## Completed Work

- [Provider runtimes](completed/providers/README.md)
  - [0001_provider_runtime_registry_for_subscription_relays.md](completed/providers/0001_provider_runtime_registry_for_subscription_relays.md)

| ID | Item | Original Path | Final Path | Completed | Outcome | Notes | Validation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0001 | Provider runtime registry for subscription relays | `docs/backlog/planned/providers/0001_provider_runtime_registry_for_subscription_relays.md` | `docs/backlog/completed/providers/0001_provider_runtime_registry_for_subscription_relays.md` | 2026-06-21 | Completed | Added provider runtime registry, model metadata, and provider-aware readiness. | `pytest -q`; `python -m compileall src tests`; `mkdocs build -q`; live `/v1/models` and `/v1/relay/status` checks |

## Deprecated Work

No deprecated backlog items yet.

## Process

- Add new planned work under `docs/backlog/planned/` with the next global numeric prefix.
- Move finished work to `docs/backlog/completed/` with its completion report and validation.
- Move superseded or rejected work to `docs/backlog/deprecated/` with the reason.
- Re-check related ADRs before implementing boundary or policy changes.
