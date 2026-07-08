# Subscription Status

AIRelays exposes:

- `GET /v1/subscription/status`
- `GET /v1/account/rate_limits` (alias)

Usage is reported in one normalized shape: per-window `used_percent`,
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

## Auth

Default protected mode requires the relay bearer token, as shown above.
Open local relay mode (`--no-auth`) accepts the same requests without the
`Authorization` header.
