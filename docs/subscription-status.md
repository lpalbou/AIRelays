# Subscription Status

AIRelays exposes:

- `GET /v1/subscription/status`
- `GET /v1/account/rate_limits`

These routes are verified for the OpenAI runtime only. They read the OpenAI subscription usage surface at `chatgpt.com/backend-api/wham/usage`.

## Auth

Default protected mode:

```bash
curl http://127.0.0.1:8080/v1/subscription/status \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

Open local relay mode:

```bash
curl http://127.0.0.1:8080/v1/subscription/status
```

## Notes

- `?raw=true` includes the raw upstream payload
- Claude experimental mode does not expose a normalized subscription-status route
