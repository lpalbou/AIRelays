# Subscription Status

AIRelays exposes:

- `GET /v1/subscription/status`
- `GET /v1/account/rate_limits`

These routes read the verified upstream usage surface at `chatgpt.com/backend-api/wham/usage` and normalize the result into one OpenAI-shaped summary object.

## Authentication

These routes use the same auth mode as the rest of `/v1/*`.

- default mode: protected by the AIRelays bearer token
- open local relay mode: accessible without `Authorization`

Example:

```bash
curl \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  http://127.0.0.1:8080/v1/subscription/status
```

Open local relay mode example:

```bash
curl http://127.0.0.1:8080/v1/subscription/status
```

## Returned Shape

The normalized response includes:

- account identity fields when the upstream exposes them
- the default rate-limit window set
- optional additional named rate-limit windows
- credits summary
- spend-control summary
- optional raw upstream payload via `?raw=true`

Typical fields:

- `object`
- `account`
- `rate_limits.default.primary_window`
- `rate_limits.default.secondary_window`
- `rate_limits.additional`
- `credits`
- `spend_control`

AIRelays also labels known windows with human-friendly names such as `5h` and `weekly` when the upstream durations match those intervals.
