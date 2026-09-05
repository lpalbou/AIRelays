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

`spend` and `extra_usage` preserve usage-credit status, disabled reasons,
and reported amounts. The desktop **more** details format money only with
its declared currency and decimal scale (`amount_minor` / `exponent` in
`spend`, or `decimal_places` in `extra_usage`). A missing balance or limit
is not inferred from zero spend. Notes:

- requires the Claude runtime to be enabled
- credentials resolve from the stored token file
  (`airelays claude set-token`) first, then the `claude` CLI's own
  credential store
- the upstream source is the same usage surface Claude Code's `/usage`
  command reads; it is not a publicly documented API, so AIRelays caches it
  for five minutes and degrades gracefully if it becomes unavailable
- upstream rate-limit cooldowns and minimum probe spacing survive restarts;
  refreshing the desktop does not bypass these protections

## Freshness

`captured_at` records the successful fetch time, not each cache read.
The Overview refreshes usage every five minutes while open; its reset
countdowns use absolute reset timestamps. Missing percentages, or windows
whose reset has passed without fresh evidence, display **awaiting fresh
data**, not zero usage. Claude snapshots served after a failed refresh
carry `stale`, `stale_reason`, and their last-good time. Neither catalog
membership nor a quota snapshot guarantees the next request will succeed.

## Auth

Default protected mode requires the relay bearer token, as shown above.
Open local relay mode (`--no-auth`) accepts the same requests without the
`Authorization` header.
