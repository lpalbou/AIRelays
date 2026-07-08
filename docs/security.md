# Security

AIRelays separates two authority domains:

- upstream provider auth
- local client access to AIRelays itself

The upstream subscription login is not the relay access credential.

## Protected Routes

Protected by default:

- `/v1/*`
- `/no-tools/v1/*`

Public:

- `/`
- `GET /healthz`

Detailed diagnostics live behind `GET /v1/relay/status` unless you intentionally launch an open local relay.

## Relay Token

The normal path is:

```bash
airelays init
```

Show the current token:

```bash
airelays token show
```

Rotate it:

```bash
airelays token rotate
```

## Open Local Relay Mode

Open local relay mode disables the AIRelays client-token gate.

```bash
airelays init --no-auth
airelays serve --no-auth --port 8080
```

Equivalent environment override:

```bash
AIRELAYS_REQUIRE_BEARER_AUTH=false airelays serve --port 8080
```

## Rate Limits

Default single-process protections:

- `120` requests/minute per IP
- burst `40`
- `8` concurrent requests per IP
- temporary IP block after repeated bad tokens

## Upload Ceilings

- `32` MiB per file
- `256` MiB total stored file bytes

## Logging

AIRelays logs:

- inbound requests
- provider resolution
- endpoint rejects
- provider and upstream requests
- provider and upstream responses
- provider stream lines
- usage summaries

Request and response contents, including prompts and model outputs, can be written to the local AIRelays log files. Raw bearer tokens are redacted.
