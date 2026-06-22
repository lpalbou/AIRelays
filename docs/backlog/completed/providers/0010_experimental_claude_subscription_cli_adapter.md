# Completed: Experimental Claude subscription CLI adapter

## Metadata
- Created: 2026-06-21
- Status: Completed
- Completed: 2026-06-21

## ADR status
- Governing ADRs: 0003, 0004
- ADR impact: Resolved by ADR 0004

## Context

AIRelays needed a second subscription-backed runtime that could reuse an existing local Claude subscription setup without pretending the Claude route surface matched the OpenAI runtime.

## Current code reality

- `src/airelay/providers.py` now includes `ClaudeCliRuntime`.
- Claude readiness comes from `claude auth status --json`.
- Claude requests run through isolated `claude -p` subprocesses.
- Claude support is opt-in and experimental.

## Problem

Claude subscription access was available locally through the CLI, but AIRelays had no runtime boundary, no guardrails, and no honest compatibility story for it.

## What we did

Added a local-only experimental Claude adapter for explicit `claude:*` model ids on text `chat.completions` and text `completions`.

## Why it mattered

Users can now reach both OpenAI and Claude subscription-backed text generation through one AIRelays server while keeping the contract explicit about where parity ends.

## Scope completed

- Claude model registration
- Claude runtime status and diagnostics
- Claude text route support
- Claude-specific startup guardrails
- Claude-specific docs and disclaimers

## Non-goals kept

- No AIRelays-owned Claude login flow
- No Claude `/v1/responses`
- No Claude files, PDFs, images, audio, tools, or structured outputs
- No shared or persistent Claude sessions
- No open or remote Claude runtime deployment

## Expected outcomes achieved

- AIRelays can serve Claude text requests through explicit `claude:*` model ids.
- Claude setup works through browser login or headless token setup with the local `claude` CLI.
- Claude runtime limits and local-only guardrails are visible in status output and docs.

## Validation

- `pytest -q`
- `python -m compileall src tests`
- `mkdocs build -q`
- live `claude auth status --json`
- live Claude `/v1/chat/completions` and `/v1/completions` through AIRelays
- live rejection checks for Claude on `/v1/responses`

## Progress checklist
- [x] Add Claude config and startup guardrails.
- [x] Implement Claude status, model catalog, and subprocess runtime.
- [x] Support Claude text route(s) with explicit rejections for unsupported routes and parameters.
- [x] Add tests and live smoke checks.
- [x] Update docs and disclaimer language.

## Completion report

### Date

2026-06-21

### Summary

AIRelays now exposes an experimental Claude path that is local-only, bearer-auth-required, loopback-only, and stateless by default. The runtime is intentionally narrow and explicit.

### Files and symbols touched

- `src/airelay/providers.py`
- `src/airelay/config.py`
- `src/airelay/app.py`
- `src/airelay/cli.py`
- `src/airelay/html.py`
- `tests/test_providers.py`
- `tests/test_auth_and_cli.py`
- `tests/test_backend_and_app.py`

### Docs updated

- `README.md`
- `docs/getting-started.md`
- `docs/configuration.md`
- `docs/security.md`
- `docs/api.md`
- `docs/faq.md`
- `docs/troubleshooting.md`
- `docs/disclaimer.md`
- `llms.txt`
- `llms-full.txt`

### Residual risks

- Claude remains an experimental local adapter, not a blanket provider parity path.
- AIRelays currently documents and uses provider-owned local auth/token paths for Claude rather than owning a Claude session lifecycle.

### Follow-ups

- No immediate follow-up item was promoted. The remaining questions are about long-term provider symmetry and logging/privacy policy, not the current runtime slice.
