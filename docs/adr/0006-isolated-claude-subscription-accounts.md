# 0006: Isolated Claude Subscription Accounts

## Status

Accepted, 2026-09-26. Extends [ADR 0004](0004-provider-runtime-boundary-and-experimental-local-adapters.md).

## Context

One user may have several Claude subscriptions. Claude Code supports isolated
configuration directories, including directory-scoped macOS Keychain entries.
AIRelays needs to select accounts without sharing credentials across requests
or taking ownership of Anthropic's OAuth flow.

## Decision

- `airelays claude login` delegates to the unmodified CLI's subscription
  login with a unique, stable `CLAUDE_CONFIG_DIR`.
- Enrollment metadata lives at `data_dir/claude/accounts/<id>/account.json`.
  It contains identity and a revision, never OAuth tokens. The adjacent
  `config/` directory and its credential store remain owned by Claude Code.
- The existing CLI/token setup is the implicit `default` account. Global
  token overrides apply only to it. No existing credential is moved.
- Reauthentication suspends a managed profile until its original identity
  is verified. Renewing `default` creates an isolated profile without
  changing the default CLI sign-in or headless token.
- One Claude account pool selects account-specific CLI runtimes. Model
  catalogs, usage caches, persisted usage backoff, and concurrency limits
  are isolated per profile. A duplicate subscription is counted once.
- Selection honors model eligibility and fresh usage, with rotation when
  quota is unknown. Request failures can fail over only before output reaches
  the client; invalid request shapes do not trigger account rotation.
  Known exhausted windows stay blocked until reset even when the meter ages.
- Logout targets one profile through the CLI and removes enrollment only
  after successful logout. Multiple-account logout needs a target or `--all`.
  Profile directories are retained because they may contain CLI-owned state.

## Alternatives

A generic pool shared with OpenAI would require changing its HTTP/SSE backend
contract and auth storage model. Separate provider pools keep those boundaries
explicit while reusing the existing Claude request adapter.

One relay process per account would isolate credentials but require another
routing service and duplicate ports, supervisors, and usage reporting.

Direct subscription-token inference would remove subprocess overhead but
depends on an unsupported inference/authentication integration. It is outside
this decision. Account isolation does not establish provider permission for
every relay workload; the existing intended-use boundary still applies.

## Consequences

Existing single-account configuration remains usable. Adding profiles is
reversible through targeted logout, without moving the default CLI credentials.
Credential-store behavior and usage reporting remain dependent on the installed
Claude Code version; failed usage probes cannot be treated as zero consumption.
Claude remains local, stateless, subscription-backed, and without API-key auth.
