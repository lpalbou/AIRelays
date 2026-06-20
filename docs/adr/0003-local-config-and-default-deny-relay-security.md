# 0003: Local Config And Default-Deny Relay Security

## Status

Accepted

## Context

AIRelays is a local OpenAI-compatible relay backed by a reused ChatGPT subscription login. That upstream login is necessary for provider access, but it is not a safe or coherent credential for client-to-relay access.

The project also needed a cleaner end-user setup story than raw environment variables alone.

## Decision

AIRelays adopts these rules:

1. Relay-client authorization is separate from upstream provider authorization.
2. Protected API routes default to bearer-token enforcement.
3. The relay bearer token is local state owned by AIRelays, not by the upstream provider auth store.
4. AIRelays exposes a small local control plane through:
   - `~/.config/airelays/config.toml`
   - CLI overrides
   - `AIRELAYS_*` environment variables
5. AIRelays applies single-process in-memory abuse controls at the edge:
   - per-IP request rate limits
   - per-IP concurrent-request caps
   - temporary IP blocks after repeated bad tokens
6. Listener default remains loopback-only.

## Consequences

### Positive

- End users get a repeatable first-run setup with `airelays init`.
- Relay access can be rotated independently from upstream login state.
- Abuse controls apply before request bodies are parsed or upstream calls are made.
- Future provider adapters can remain behind one stable client-facing relay boundary.

### Negative

- AIRelays now owns a small amount of local secret state.
- Rate limiting is single-process and not distributed across multiple instances.
- Setup and docs must explain two credential layers clearly.

## Non-Goals

- Multi-tenant access control
- Distributed rate limiting
- Shared remote operations control plane
