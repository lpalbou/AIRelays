# Proposed: Claude token shape validation on set-token

## Metadata

- Created: 2026-07-11
- Status: Proposed

## Summary

Validate the pasted token's shape in `airelays claude set-token` (expected `sk-ant-oat01-` prefix, sane length floor) and warn on mismatch before storing.

## Reason

`claude setup-token` prints the token once and line-wraps in narrow terminals; a copied first line still looks like a token and only fails later with a confusing upstream 401 at the first request. Catching a truncated paste at store time turns a delayed, hard-to-diagnose failure into an immediate, actionable message. There is a reported upstream issue for exactly this failure mode.

## Current code reality

`airelays claude set-token` (src/airelay/cli.py) stores the pasted value as-is via `Settings.write_claude_oauth_token`. The usage path already fingerprints rejected tokens (src/airelay/providers.py) but only after an upstream 401.

## Scope

- prefix/length sanity check with a clear warning and a confirmation path for intentionally different tokens
- strip embedded whitespace/newlines from pastes
- no change to storage format or env fallback

## Validation

- unit tests for accepted, truncated, and whitespace-wrapped pastes
