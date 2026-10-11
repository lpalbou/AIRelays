# Subscription Status

AIRelays exposes:

- `GET /v1/subscription/status`
- `GET /v1/account/rate_limits` (alias)

Both providers report usage in one normalized shape, so a client can render
OpenAI and Claude quota with the same code: per-window `used_percent`,
`window_label` ("5h", "weekly", derived from each window's duration), and
reset times (`reset_after_seconds`, `reset_at_iso`).

## OpenAI

Reads the OpenAI subscription usage surface at
`chatgpt.com/backend-api/wham/usage`.

Which windows appear is plan-dependent upstream policy: some plans report
a 5h window plus a weekly window, others only a weekly window. AIRelays
passes through exactly the windows the upstream reports — nothing is
synthesized — and identifies each by its duration, not its position in
the payload. Multi-account balancing and the desktop's per-account token
breakdown both key on the longest reported window (the weekly budget).

`credits`, `spend_control`, `rate_limit_reset_credits`, and `model_usage`
preserve the upstream's credit and model-availability facts. Available
limit-reset credits and credits applicable right now are distinct counts.
Quota percentages are not token counts or monetary balances. The desktop's
token breakdown counts only responses observed through this relay in the
account's reported window, not activity in other apps or before recording.

```bash
curl 'http://127.0.0.1:8080/v1/subscription/status' \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

With multiple enrolled accounts:

- `?account=<email-or-prefix>` selects one account
- `?all_accounts=true` returns the list shape with one entry per account,
  regardless of how many are enrolled; an account whose usage probe fails
  carries an `error` string instead of a `status`, so one broken sign-in
  never hides the others (releases before 0.12.5 folded a lone account to
  the bare single-account shape)
- `?raw=true` includes the raw upstream payload

## Claude

```bash
curl 'http://127.0.0.1:8080/v1/subscription/status?provider=claude' \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

Returns the 5-hour and weekly windows, plus named scoped caps such as
Fable, Sonnet, or Opus when the subscription reports them. The modern
`limits` array supplies explicit scope names and percentages; legacy
buckets remain supported. A Fable cap at 100% does not mean the whole
Claude allowance is exhausted. The desktop names the exhausted scope
instead of presenting all Claude models as unavailable.

Add `&all_accounts=true` for a per-account list
(`{"object": "subscription_status_list", "provider": "claude", "accounts": [...]}`),
or `&account=<email-or-id>` for one account. Each list entry contains `slug`,
`email`, and one of `status`, `error`, or `usage_unsupported`. The setup-token
account (`default`) always carries `usage_unsupported`: Claude reports no
usage or email for setup tokens, so `&account=default` returns 422
`unsupported_for_provider` and the desktop explains this instead of showing
bars. Each browser-enrolled account has its own set of bars.

`spend` and `extra_usage` preserve usage-credit status, disabled reasons,
and reported amounts. Hover over or activate **?** beside an account name
in the OpenAI or Anthropic section to open details; press **Escape** to close.
These details format money only with
its declared currency and decimal scale (`amount_minor` / `exponent` in
`spend`, or `decimal_places` in `extra_usage`). A missing balance or limit
is not inferred from zero spend. Notes:

- requires the Claude runtime to be enabled and an AIRelays-owned account;
  the personal Claude Code sign-in is never read or reported
- usage comes from Claude Code itself: the relay sends each profile's CLI the
  Claude Code SDK `get_usage` control request — no prompt, no inference, no
  quota. AIRelays never reads Claude credentials (no keychain or
  credential-file reads) and never calls the usage endpoint directly
- readings refresh at startup and every five minutes. Claude Code renews a
  profile's OAuth token while answering, so this also keeps idle accounts
  signed in
- requires a Claude Code version that supports `get_usage` (verified with
  2.1.292+); Claude Code marks it experimental. Older versions report the
  meter as unsupported without affecting requests
- failed refreshes back off exponentially (5, 10, 20, 40, then 60 minutes),
  persisted across restarts; refreshing the desktop does not bypass this
- usage state and backoff are separate for each profile; an unavailable usage
  meter does not imply unavailable inference

## Freshness

`captured_at` records the successful fetch time, not each cache read.
The Overview refreshes usage every five minutes while open; its reset
countdowns use absolute reset timestamps. Missing percentages, or windows
whose reset has passed without fresh evidence, display **awaiting fresh
data**, not zero usage. Neither catalog membership nor a quota snapshot
guarantees the next request will succeed.

A Claude reading is fresh only when Claude Code returns live data. Otherwise
the dashboard and API show the last good reading, including its scoped caps,
marked `stale` with its age and a `stale_reason`:

- `usage_unavailable`: Claude did not return live data, for example because
  the upstream rate-limited its usage endpoint
- `nonessential_traffic_disabled`: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
  is set in the relay's environment, which stops Claude Code fetching usage
- `cli_unsupported`: the installed Claude Code lacks `get_usage`; update
  Claude Code (requests are unaffected)
- `cli_failed`: the CLI is missing, timed out, or failed

Without any good reading yet, the API returns an error message instead of
numbers. Stale readings never unblock or rank an account, and capacity
observations older than fifteen minutes do not rank accounts; a known
exhausted window still blocks routing until its reset or a newer fresh
reading shows capacity.

## Auth

Default protected mode requires the relay bearer token, as shown above.
Open local relay mode (`--no-auth`) accepts the same requests without the
`Authorization` header.
