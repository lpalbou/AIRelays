# 0004: Provider Runtime Boundary And Experimental Local Adapters

## Status

Accepted

Amended 2026-07-10: the Claude adapter graduated out of the "experimental"
label when it merged to mainline (user-facing labels, the `experimental`
wire fields, and the env-var name changed; `AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL`
remains honored as a legacy alias). The rules below still bind the Claude
adapter unchanged — explicit model-driven routing, visible capability
boundaries, local-only/loopback-only operation, stateless default, and
explicit rejection of unsupported combinations. Only the labeling and the
"reject open-relay mode" clause (superseded by the relay-wide auth-mode
behavior documented in README and docs/security.md) have evolved.

## Context

AIRelays now exposes more than one upstream runtime behind one OpenAI-compatible local edge. The OpenAI runtime remains the primary subscription-backed path, while Claude support is introduced as a smaller local adapter with a materially different auth surface and capability boundary.

Without an explicit rule, future provider work would drift into ambiguous model routing, overstated parity claims, or provider-specific behavior leaking into shared routes without a clear contract.

## Decision

AIRelays adopts these rules for multi-provider expansion:

1. Provider selection is explicit and model-driven.
2. `claude:*` model ids select the Claude experimental runtime when it is enabled.
3. Other model ids select the OpenAI runtime when it is enabled.
4. Provider capability boundaries must be visible in `/v1/models` and `/v1/relay/status`.
5. Unsupported provider, route, state, or parameter combinations must fail locally and explicitly.
6. Experimental local adapters must be:
   - opt-in
   - local-only
   - loopback-only
   - bearer-auth-required
   - stateless by default
7. AIRelays must not claim blanket parity across providers.
8. AIRelays may use provider-owned local login state or provider-approved local tokens, but must not silently store or invent a separate provider session model unless a later ADR allows it.

## Consequences

### Positive

- AIRelays can serve more than one subscription-backed runtime from one local server.
- Users can discover provider ownership and route limits from the model catalog itself.
- Experimental adapters can ship with narrow, explicit limits without weakening the richer OpenAI path.

### Negative

- The shared northbound API is no longer universal across all enabled providers.
- Docs, CLI output, and runtime diagnostics must stay explicit about provider-specific setup and route support.
- Adding future providers will require deliberate routing, capability, and security decisions instead of silent fallback.

## Enforcement

- `/v1/models` must include provider identity and route capability metadata.
- `/v1/relay/status` must expose provider-scoped readiness.
- Provider-specific route handlers must reject unsupported combinations explicitly.
- User-facing docs must state the current provider-routing rule and the experimental/local-only status of narrow adapters.
- Experimental local adapters must reject open-relay mode.

## Validation

- Tests for provider routing and provider-specific route rejection
- Tests for provider readiness reporting
- Tests for Claude experimental guardrails
- Live smoke checks for one OpenAI request and one Claude request through the same AIRelays process
