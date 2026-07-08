# Completed: Provider runtime registry for subscription relays

## Metadata
- Created: 2026-06-21
- Status: Completed
- Completed: 2026-06-21

## ADR status
- Governing ADRs: 0002, 0003

## Context

AIRelays initially served one northbound OpenAI-shaped API over one upstream runtime. The product goal expanded to multiple subscription-backed local runtimes without collapsing them into one false parity surface.

## Current code reality

- `src/airelay/providers.py` now owns provider runtime registration, model metadata, model resolution, and provider readiness.
- `src/airelay/app.py` uses provider-aware model listing, provider-aware readiness reporting, and provider-specific route rejection.
- `src/airelay/config.py` now includes provider toggles.

## Problem

Without a provider boundary, a second runtime would have scattered branches across the app and unclear ownership of model routing and capability reporting.

## What we did

Added a provider registry and provider-scoped capability metadata while keeping the OpenAI runtime stable and first-class.

## Why it mattered

This made multi-provider routing explicit and gave AIRelays one place to declare provider ownership, readiness, and route limits.

## Scope completed

- Provider registry
- Provider-aware model listing
- Provider-aware relay readiness
- Deterministic model routing
- OpenAI-only local route gating when the OpenAI runtime is disabled

## Non-goals kept

- No universal provider abstraction for every route and content type
- No cross-provider session continuity
- No silent fallback between providers

## Expected outcomes achieved

- AIRelays can expose provider-scoped runtimes from one process.
- `/v1/models` now identifies provider ownership and route capabilities.
- `/v1/relay/status` now reports provider-scoped readiness and overall provider availability.

## Validation

- `pytest -q`
- `python -m compileall src tests`
- `mkdocs build -q`
- live `curl` checks for `/v1/models`, `/v1/relay/status`, `/v1/responses`, and `/v1/chat/completions`

## Progress checklist
- [x] Add the provider registry and runtime contracts.
- [x] Keep current OpenAI behavior stable behind the registry.
- [x] Surface provider metadata in models and relay status.
- [x] Add tests for multi-provider routing and unsupported combinations.

## Completion report

### Date

2026-06-21

### Summary

AIRelays now has an explicit provider-runtime seam. OpenAI remains the default runtime, while provider routing and readiness are now first-class concepts in code, status output, and docs.

### Files and symbols touched

- `src/airelay/providers.py`
- `src/airelay/app.py`
- `src/airelay/config.py`
- `tests/test_backend_and_app.py`
- `tests/test_providers.py`

### Docs updated

- `README.md`
- `docs/api.md`
- `docs/architecture.md`
- `docs/getting-started.md`
- `docs/security.md`
- `llms.txt`
- `llms-full.txt`

### Residual risks

- The provider seam is still partial: OpenAI route shaping remains shared rather than fully encapsulated in the runtime adapter.
- OpenAI model routing is still permissive for arbitrary model ids when the OpenAI runtime is enabled.

### Follow-ups

- No immediate follow-up item was promoted. The remaining work is architectural refinement, not a current-release blocker.
