# Completed: Relay-owned Claude accounts and CLI-sourced usage

## Metadata

- Created: 2026-10-10
- Status: Completed
- Completed: 2026-10-10

## Context

ADR 0006 kept the personal Claude Code sign-in as an implicit `default`
account beside the browser-enrolled profiles. An owner with two Claude
subscriptions had one of them reachable only through that personal sign-in.
When the personal Claude Code login switched to the other subscription, which
was already enrolled as a profile, the relay deduplicated `default` away and
the first subscription disappeared from routing and from the dashboard.

At the same time, the enrolled profile's usage meter had been frozen for more
than three days: it still showed 19% weekly usage when the real value was 62%,
and a per-model weekly cap at 16% when it was really exhausted (100%). The
relay read OAuth access tokens directly from Claude Code's credential stores
and never refreshed them; a profile that served no requests kept its last
numbers after its access token expired. Only `default` stayed fresh, because
the personal session kept rotating its token. Signing out `default` from
AIRelays also signed out the personal Claude Code.

Refresh tokens rotate, so the personal sign-in cannot be shared with or copied
into a relay profile. The decision is recorded in
[ADR 0007](../../../adr/0007-relay-owned-claude-accounts.md).

## What we did

- Accounts are only those AIRelays owns: browser-enrolled profiles under
  `data_dir/claude/accounts/<id>/` and an optional setup token stored with
  `airelays claude set-token`. The personal Claude Code sign-in, an inherited
  `CLAUDE_CONFIG_DIR`, and an inherited `CLAUDE_CODE_OAUTH_TOKEN` are never
  used; `airelays doctor` reports an ignored `CLAUDE_CODE_OAUTH_TOKEN`.
- The setup token keeps the `default` slug for API compatibility, runs in its
  own relay-owned `data_dir/claude/token/config`, and is inference-only: no
  meter (`usage_unsupported`, 422 `unsupported_for_provider` for
  `account=default`) and no deduplication.
- Sign-out runs `claude auth logout` only inside a relay-owned profile;
  `logout default` only deletes the stored token. With no account connected,
  `claude:*` requests return 503 `provider_auth_error` and `/v1/models` lists no
  Claude models.
- Usage comes from the Claude Code SDK `get_usage` control request inside each
  profile, at startup and every five minutes. AIRelays no longer reads Claude
  credentials or calls the usage endpoint. The CLI renews the profile's token
  while answering, so the refresh also keeps idle profiles signed in.
- A reading is fresh only with live data; otherwise the last good reading is
  served stale with its age and `stale_reason` (`usage_unavailable`,
  `nonessential_traffic_disabled`, `cli_unsupported`, `cli_failed`). Failures
  back off 5 → 10 → 20 → 40 → 60 minutes, persisted across restarts. Stale
  readings, including scoped caps, never unblock or rank an account.
- Relay status: `oauth_token_source` is `profile` or `file`, `managed_profile`
  is `false` for the setup-token account, `served_by` is removed, and
  `duplicate_of` remains.
- Every Claude Code process runs with an explicit environment, its profile's
  `CLAUDE_CONFIG_DIR`, and a relay-owned working directory, and is ended
  gracefully (stdin closed or SIGTERM before SIGKILL).
- User-facing docs, `llms.txt`, `llms-full.txt`, and the changelog describe the
  account boundary, the usage source and stale reasons, and the one-time
  migration for installs that relied on the personal sign-in.

## Validation

- ADR 0007 accepted; ADR 0006 marked as amended.
- `pytest`: 417 passed. New coverage: ambient sign-in, config dir and env
  token never become accounts; zero accounts answer `claude:*` with 503;
  live vs. seeded `get_usage` readings; exponential backoff persisted across
  restarts; unsupported CLI and disabled nonessential traffic; usage refresh
  surviving caller cancellation; a real child process answering a control
  request and exiting cleanly on stdin close (no kill); setup-token sign-out
  deleting only the token file without running any CLI command.
- Desktop: 14 browser tests (setup-token row and sign-out copy, signed-out
  profile renewal), 3 node usage tests, `cargo check`.
- Live check against Claude Code 2.1.296 with a real enrolled profile: only
  the browser profile is discovered (the personal sign-in is ignored), every
  process uses the profile's config dir, and `get_usage` returns a live
  reading in under two seconds without inference, including a per-model cap
  at 100% that the frozen meter had shown at 16%.
- Documentation checked for real identifiers before publication (public
  repository).

## Residual risks / follow-ups

- Verify on a profile idle for more than eight hours that `get_usage` renews
  its token: the profile's keychain item modification time changes, and an
  inference request succeeds afterwards.
- Consider folding the 30-second `--version` and `auth status` probes into one
  CLI session to reduce process launches (backlog only).
- `get_usage` is experimental in Claude Code; a removal or shape change would
  turn every meter stale (`cli_unsupported`) without affecting requests.
- Claude Code still derives a few files from the home directory (for example
  device keys) regardless of `CLAUDE_CONFIG_DIR`. On macOS the relay must run
  in the user's login session to use the keychain.
