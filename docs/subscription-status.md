# Subscription Status

AIRelays exposes:

- `GET /v1/subscription/status`
- `GET /v1/account/rate_limits` (alias)

Both providers report usage in one normalized shape, so a client can render
OpenAI and Claude quota with the same code: per-window `used_percent`,
`window_label` ("5h", "weekly"), and reset times (`reset_after_seconds`,
`reset_at_iso`).

## OpenAI

Reads the OpenAI subscription usage surface at
`chatgpt.com/backend-api/wham/usage`.

```bash
curl 'http://127.0.0.1:8080/v1/subscription/status' \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

With multiple enrolled accounts:

- `?account=<email-or-prefix>` selects one account
- `?all_accounts=true` returns one entry per account (folds to the
  single-account shape when only one exists)
- `?raw=true` includes the raw upstream payload

## Claude

```bash
curl 'http://127.0.0.1:8080/v1/subscription/status?provider=claude' \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

Returns the 5-hour and weekly windows, plus per-model weekly caps
(Sonnet/Opus) when the subscription reports them. Notes:

- requires the Claude runtime to be enabled
- credentials resolve from the stored token file
  (`airelays claude set-token`) first, then the `claude` CLI's own
  credential store
- the upstream source is the same usage surface Claude Code's `/usage`
  command reads; it is not a publicly documented API, so AIRelays caches it
  briefly (30s) and degrades gracefully if it becomes unavailable

## Auth

Default protected mode requires the relay bearer token, as shown above.
Open local relay mode (`--no-auth`) accepts the same requests without the
`Authorization` header.
