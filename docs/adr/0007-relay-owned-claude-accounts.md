# 0007: Relay-Owned Claude Accounts

## Status

Accepted, 2026-10-10. Amends [ADR 0006](0006-isolated-claude-subscription-accounts.md).

## Context

ADR 0006 kept the user's own Claude Code sign-in as an implicit `default`
account. That made the relay depend on a personal tool it does not own:

- Switching the personal Claude Code login to another subscription silently
  changed which subscription the relay served and reported. When it switched
  to an account already enrolled as a profile, the relay deduplicated
  `default` away, and a subscription reachable only through it disappeared.
- Signing out `default` from AIRelays signed out the personal Claude Code.
- Usage meters read OAuth access tokens directly from Claude Code's
  credential stores and never refreshed them. A profile that served no
  requests showed its last numbers indefinitely after its access token
  expired; only `default` stayed fresh, because the personal session kept
  rotating its token.

Claude Code refresh tokens rotate, so one credential cannot be shared or
copied between the personal sign-in and a relay profile.

## Decision

- AIRelays routes only through accounts it owns: profiles enrolled with
  `airelays claude login` (each with its own `CLAUDE_CONFIG_DIR` under
  `data_dir/claude/accounts/<id>/`) and, optionally, one setup token stored
  with `airelays claude set-token`. The ambient Claude Code sign-in, an
  inherited `CLAUDE_CONFIG_DIR`, and an inherited `CLAUDE_CODE_OAUTH_TOKEN`
  are never used. Existing installs that relied on them must add an account.
- The setup-token account keeps the `default` slug for API compatibility. It
  runs in its own relay-owned configuration directory
  (`data_dir/claude/token/config`). It is inference-only: Claude reports no
  usage and no identity for setup tokens, so it has no meter and cannot be
  deduplicated. Signing it out only deletes the stored token.
- AIRelays never runs `claude auth logout` outside a relay-owned profile.
- AIRelays never reads Claude credentials. Usage comes from the Claude
  Code SDK control request `get_usage` (`skip_behaviors`), run inside each
  profile. The CLI refreshes the profile's OAuth token as needed, without an
  inference request. The background usage refresh therefore also keeps idle
  profiles signed in.
- `get_usage` is experimental in Claude Code. A reading counts as fresh only
  when it carries the `limits` list or a breakdown `as_of` under two minutes
  old; anything else (a snapshot the CLI seeded after an upstream failure,
  a null meter, an unsupported CLI) keeps the last good reading, marked stale
  with its age and reason. Stale data never unblocks or ranks an account.
  Failures back off exponentially, persisted across restarts. There is no
  fallback to reading tokens or calling the usage endpoint directly.
- Every Claude Code process runs with an explicit environment and a
  relay-owned working directory. Processes are ended gracefully (stdin
  closed or SIGTERM) before any SIGKILL, so the CLI can finish persisting a
  rotated token.

## Alternatives

Copying the personal sign-in into a profile would split one rotating
refresh token between two owners; the first refresh breaks the other.
Refreshing tokens in AIRelays would take ownership of Claude Code's OAuth
flow and its credential stores. A periodic inference request would keep
tokens alive but open usage windows and spend quota.

## Consequences

The personal Claude Code sign-in and AIRelays are independent in both
directions. Adding the first account requires one browser sign-in per
subscription (or a setup token). Usage reporting depends on Claude Code
supporting `get_usage`; older versions report the meter as unsupported
without affecting requests. Claude Code still derives a few files from the
home directory (for example device keys) regardless of `CLAUDE_CONFIG_DIR`.
On macOS the relay must run in the user's login session so Claude Code can
use the keychain.
