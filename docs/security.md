# Security

AIRelays separates two authority domains:

- upstream provider auth: the ChatGPT subscription login stored in AIRelays-owned auth storage
- relay auth: the local bearer token required to call AIRelays itself

The upstream login is not the relay access credential.

## Protected Routes

AIRelays protects these route families by default:

- `/v1/*`
- `/no-tools/v1/*`

Public routes remain:

- `/`
- `/healthz`

`/healthz` is intentionally minimal. Use the protected `GET /v1/relay/status` route when you need detailed relay diagnostics.

## Relay Token

`airelays init` creates a strong bearer token and stores it at:

```text
~/.airelays/relay-token
```

Use that token as the client credential when you point OpenAI SDKs or HTTP clients at AIRelays.

Show the current token with:

```bash
airelays token show
```

If you want to launch the server with a specific token value instead of the default token file:

```bash
AIRELAYS_BEARER_TOKEN='YOUR_AIRELAYS_TOKEN' airelays serve --port 8080
```

If you want to launch the server with a specific token file:

```bash
airelays serve --bearer-token-file /path/to/relay-token --port 8080
```

## Open Local Relay Mode

If you want AIRelays to accept client requests without a relay bearer token:

```bash
airelays init --no-auth
airelays serve --no-auth --port 8080
```

Equivalent environment override:

```bash
AIRELAYS_REQUIRE_BEARER_AUTH=false airelays serve --port 8080
```

In this mode:

- `/v1/*` and `/no-tools/v1/*` are accessible without `Authorization`
- `GET /v1/relay/status` is also accessible without `Authorization`
- rate limits and concurrent-request caps still apply
- the default loopback listener is the safest way to run it
- if a client library insists on an API key field, any non-empty placeholder string is acceptable
- the upstream ChatGPT login from `airelays login` is still required for model requests

Rotate the token with:

```bash
airelays token rotate
```

## Rate Limits

Default single-process protections:

- `120` requests/minute per IP
- burst capacity `40`
- `8` concurrent requests per IP
- `8` failed auth attempts inside `300` seconds
- temporary block for `900` seconds after repeated failed auth

Repeated requests without the relay token, or with the wrong token, first return `401` and then trigger a temporary `429` block once the failed-auth threshold is exceeded.

These settings are configurable through `config.toml` or `AIRELAYS_*` overrides.

## Upload Ceilings

Local uploads are bounded by default:

- `32` MiB per file
- `256` MiB total stored file bytes

AIRelays rejects uploads that would exceed either limit with `413`.

## Logging

Security-relevant events are logged to the normal hourly JSONL traffic log:

- endpoint auth failures
- endpoint rejects
- token bootstrap at startup
- upstream and downstream request flow

Raw bearer tokens are never written to logs. Request headers are redacted before persistence.

## Operational Notes

- Keep the default loopback listener unless you intentionally need remote access.
- If you bind to a broader interface, keep bearer auth enabled.
- Do not enable `trust_x_forwarded_for` unless a trusted proxy is in front of AIRelays.
- Use `GET /v1/relay/status` to inspect relay auth, storage, and limiter state. Keep bearer auth enabled if you do not want that route exposed.
