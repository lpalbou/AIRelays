# Proposed: Metered embeddings capability slice (single API key, embeddings only)

## Metadata

- Created: 2026-07-13
- Status: Proposed
- Completed: N/A

## ADR status

- Governing ADRs: ADR 0002 (subscription backend compatibility boundary), ADR 0003 (default-deny relay security), ADR 0004 (provider runtime boundary)
- ADR impact: Needs new ADR before promotion. ADR 0002 defines the product as subscription-backed, explicitly "not on a user-supplied OpenAI platform API key", and 0.2.3 deliberately removed the last API-key path. Implementing this item requires an ADR that amends or supersedes that scope with a narrow capability-complement rule (see Proposed direction). Do not implement from this backlog item alone.

## Context

Vector embeddings are physically impossible through both enrolled subscription
runtimes (verified live 2026-07-13):

- The ChatGPT Codex backend has no embeddings route (an unauthenticated probe of
  `/backend-api/codex/embeddings` behaves exactly like a nonexistent path, unlike
  `/responses` → 401 and `/models` → 405), its model catalog carries no embedding
  models, and forcing an embedding model id onto `/responses` is rejected upstream:
  "The 'text-embedding-3-small' model is not supported when using Codex with a
  ChatGPT account."
- Anthropic has no first-party embedding models at all — their docs state
  "Anthropic does not offer its own embedding model" and recommend Voyage AI;
  `api.anthropic.com/v1/embeddings` is a genuine 404. The `claude` CLI surface is
  text generation only.

So today no client of AIRelays can obtain embeddings, and the relay's
`/v1/embeddings` route answers an honest 501. An adversarial design review
(2026-07-13, Fable 5) of a broader proposal — general API-key providers for
OpenAI/Anthropic/OpenRouter/OVH — rejected the broad version (identity conflict
with ADR 0002, security model sized for quota not dollars, "passthrough" collapsing
into a translation layer by the third provider) but explicitly kept alive this
minimal slice: one metered credential, one capability, namespaced and opt-in.

## Current code reality

Inspected 2026-07-13:

- `src/airelay/app.py`: `/v1/embeddings` is registered in the `unsupported_route`
  group and returns 501 `unsupported_error` before any upstream contact.
- `src/airelay/providers.py`: provider-scoped runtimes with namespaced model ids
  (`claude:*`) and per-model route capability metadata published via `/v1/models` —
  the structural pattern this slice would follow.
- `src/airelay/auth.py`: `AuthStorage` (keyring-backed) is the correct home for a
  metered key. The desktop rewrites `config.toml` (`accounts.py` docstring), so the
  key must never live there.
- `src/airelay/config.py`: `validate_provider_guardrails()` is the precedent for a
  hard startup interlock (Claude refuses non-loopback listeners); the metered key
  needs an equivalent (refuse to start with a key configured in `--no-auth` mode).
- `src/airelay/security.py`: one shared relay bearer token, request-denominated
  rate limits — no per-client scoping or spend controls exist.
- `src/airelay/traffic.py`: logs full JSON request/response bodies; raw embedding
  vectors (thousands of floats per response) would bloat hourly logs and the
  desktop traffic reader, so vector payloads must be summarized, never logged raw.
- `src/airelay/accounts.py`: the pool retries on 429/5xx — free on subscriptions, a
  spend multiplier on metered calls; the metered path must not inherit retries.

## Problem or opportunity

Two candidate benefits, both uncertain:

1. Capability: RAG-style workloads next to chat need embeddings; the relay cannot
   serve them and forces a second endpoint into every client config.
2. Key custody: apps would hold only the relay token while the real billing key
   lives in one place (keyring), with one rotation point and unified traffic
   logging across subscription and metered usage.

Honest counter-position (owner's own doubt, recorded deliberately): this may be
useless in practice. Any app can point its embeddings base URL directly at OpenAI
(same API shape, upstream-enforceable project budgets) or at a local model. The
marginal value may reduce to key-management centralization alone — and
centralizing a billing key behind one shared, unscoped bearer token is a richer
theft target as much as it is a convenience. That trade cuts both ways and is the
core reason this item is proposed, not planned.

## Proposed direction

A capability-complement rule, to be codified in the new ADR: metered credentials
may exist only for capabilities no enrolled subscription can physically provide —
today, exactly embeddings.

- Serve `/v1/embeddings` only for explicitly namespaced ids
  (e.g. `openai-api:text-embedding-3-small`); bare or subscription model ids keep
  the current rejection; every other 501 route stays 501.
- Opt-in, off by default; key stored via `AuthStorage` (keyring), never
  `config.toml`.
- Hard interlock: refuse to start when a metered key is configured together with
  `--no-auth` (open mode), mirroring `validate_provider_guardrails()`.
- No retries/failover on the metered path.
- Traffic logs record dims/bytes/hash summaries of vectors, never raw payloads.
- Desktop: one plain "Embeddings" line (request and token counts, link to the
  upstream billing dashboard, explicit "AIRelays does not enforce spend caps"
  note). No usage bars, no windows, no pretending.
- Setup docs require an endpoint-restricted, budget-capped OpenAI project key so a
  stolen key is nearly worthless.

## Why it might matter

- Closes the only verified capability hole in the OpenAI-compatible surface while
  keeping the subscription identity intact (one narrow, explicitly bounded
  exception instead of a general metered gateway).
- Preserves the "one endpoint, one client secret" story for mixed chat+RAG apps.
- The bounded shape is cheap to hold: embeddings is the most stable OpenAI route —
  no streaming, no tools, spend measured in cents per million tokens.

## Promotion criteria

Promote to `planned/` only when all of these hold:

1. A concrete, recurring need is demonstrated (a real workflow that wants chat and
   embeddings through the same relay endpoint — not a hypothetical).
2. The competing alternatives are reassessed and rejected for that need:
   local embeddings via an `ollama:*`-style adapter (no key, no billing, follows
   the `claude` CLI precedent), direct provider config in the client app, or a
   documented sidecar (e.g. LiteLLM) holding keys with AIRelays as one upstream.
3. The superseding/amending ADR is drafted and accepted (capability-complement
   rule, non-goals, and the open-mode interlock as policy).
4. The owner accepts the custody trade-off explicitly: billing key behind one
   shared bearer token, no per-client scopes, no relay-side spend enforcement.

## Validation ideas

- Unit: namespaced id routing, rejection of bare/subscription ids on
  `/v1/embeddings`, startup interlock with `--no-auth`, key resolution from
  keyring only.
- Traffic: assert vector payloads are summarized (dims/bytes/hash) in logs.
- Live: one real embeddings call through the relay against a budget-capped project
  key; confirm token counts surface in the desktop line.
- Docs: FAQ entry explaining why embeddings are impossible via subscriptions and
  when to prefer direct/local/sidecar alternatives instead of this slice.

## Non-goals

This proposal does not authorize:

- metered chat/completions/responses of any kind;
- OpenRouter, Anthropic API, or OVH providers (OpenRouter does not reliably close
  the embeddings gap and aggregation is better consumed directly);
- any subscription→metered fallback, silent or configured;
- images/audio/realtime unlocks;
- spend dashboards or relay-side budget enforcement (v1 explicitly displays "no
  spend caps" instead);
- per-client scoped relay tokens (a prerequisite for any broader centralization
  ambition, tracked separately if that product bet is ever made).

## Guidance for future agents

Re-verify the capability gap before acting: if either subscription upstream gains
an embeddings surface, or if a local-embeddings adapter lands first, this item
likely dies — check `deprecated/` fit before promoting. Read ADR 0002 and the
0.2.3 changelog entry first; the identity boundary is deliberate and this item is
an exception to it, not a precedent for general API-key support. If the real
underlying need turns out to be "centralize all my keys", stop and write that up
as its own decision (per-client scoped tokens and budgets as the entry fee) rather
than stretching this slice.
