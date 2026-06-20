# 0001: No Silent Degradation, Truncation, Or Budget Invention

- Status: Accepted
- Date: 2026-06-12

## Context

This project sits between OpenAI-compatible clients and a different upstream surface with real behavioral differences. A compatibility layer can easily become misleading if it silently drops parameters, truncates user content, invents token budgets, or fakes support for unsupported endpoints.

## Decision

The server must not silently:

- truncate file content
- invent token budgets or usage numbers
- coerce unsupported binary/audio payloads into text
- treat unsupported routes as successful no-ops
- hide upstream constraints that materially affect semantics

When a behavior cannot be reproduced honestly, the server must:

- reject the request with an explicit error, or
- document the exact adaptation being performed when the adaptation is minimal and required for basic interoperability

## Required Consequences

- text-like uploaded files larger than 1 MB are rejected instead of truncated
- unsupported endpoints return explicit `501 unsupported_error`
- unverified modalities return `422` or `501`, not silent best-effort guessing
- usage is logged only when the upstream provides it
- upstream `store=true` is rejected because the verified subscription backend requires `store=false`
- the minimal placeholder instructions value `"."` may be injected only when the caller omitted instructions entirely and only because the verified upstream rejects an empty instructions field

## Rationale

The project is more useful when it is slightly narrower but trustworthy than when it appears broader while silently changing behavior.
