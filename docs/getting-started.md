# Getting Started

## Install

From a source checkout:

```bash
python -m pip install .
```

From a published package:

```bash
python -m pip install airelays
```

Open local relay mode uses the same package and login flow. The difference is whether you keep the default bearer-token protection or disable it for the running process.
It does not bypass the upstream ChatGPT login. Run `airelays login` before expecting model routes to succeed.

## Initialize AIRelays

```bash
airelays init
```

This creates the local control-plane files when they do not already exist:

- config: `~/.config/airelays/config.toml`
- relay token: `~/.airelays/relay-token`
- data dir: `~/.airelays`
- logs dir: `~/.airelays/logs`

The command prints a readable setup summary and, when it creates a new token, the token to use from normal OpenAI-compatible clients that you point at AIRelays.
If a token already exists, `airelays init` keeps it and does not reveal the value again by default.
Use `airelays init --json` when you need machine-readable output.

The relay token protects AIRelays itself. Clients calling `/v1/*` or `/no-tools/v1/*` must send:

```http
Authorization: Bearer YOUR_AIRELAYS_TOKEN
```

## Inspect Status

```bash
airelays status
```

The output contains:

- relay configuration summary
- whether a relay bearer token is present
- whether AIRelays-owned upstream ChatGPT auth is present and ready
- the next recommended command

Typical unauthenticated terminal output:

```text
AIRelays Status

Relay
  Config exists:     yes
  Relay token:       present

Upstream Session
  Ready:             no
  Authenticated:     no

Next Steps
  1. airelays login
```

Use `airelays status --json` for machine-readable output.

## Log In Upstream

Browser login:

```bash
airelays login
```

By default, AIRelays prints the login URL so you can open it in the browser profile you choose. Set `AIRELAYS_BROWSER_OPEN=true` if you want AIRelays to try opening the browser automatically.

If the browser flow cannot bind `localhost:1455`, use device-code login instead:

Device-code login:

```bash
airelays login --device
```

Restrict login to one workspace:

```bash
airelays login --workspace-id YOUR_WORKSPACE_ID
```

## Start The Server

```bash
airelays serve --host 127.0.0.1 --port 8080
```

By default, AIRelays protects `/v1/*` and `/no-tools/v1/*` with the relay bearer token.
If bearer auth is enabled and no relay token exists, `airelays serve` exits with a clear setup error instead of silently generating one.
On startup AIRelays prints the base URL, the token file path, and the required `Authorization` header shape.

To launch the server with an explicit token instead of the default token file:

```bash
AIRELAYS_BEARER_TOKEN='YOUR_AIRELAYS_TOKEN' airelays serve --host 127.0.0.1 --port 8080
```

To launch the server with a specific token file:

```bash
airelays serve \
  --bearer-token-file /path/to/relay-token \
  --host 127.0.0.1 \
  --port 8080
```

To start an open local relay with no client-side bearer auth:

```bash
airelays init --no-auth
airelays serve --no-auth --host 127.0.0.1 --port 8080
```

To do the same through environment or config:

```bash
AIRELAYS_REQUIRE_BEARER_AUTH=false airelays serve --host 127.0.0.1 --port 8080
```

When bearer auth is disabled, clients can call `/v1/*` and `/no-tools/v1/*` without `Authorization`. If a client library insists on an API key field, use any non-empty placeholder string.
If `airelays login` has not completed yet, model routes still fail because AIRelays has no upstream ChatGPT session to use.

## Verify The Server

Public liveness:

```bash
curl http://127.0.0.1:8080/healthz
```

Protected diagnostics:

```bash
curl \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  http://127.0.0.1:8080/v1/relay/status
```

`/healthz` is intentionally minimal. Use `/v1/relay/status` when you need protected config, auth, storage, and limiter details.
If you launched with `--no-auth`, the same `/v1/relay/status` request works without the `Authorization` header.

## Point Your Client

Base URL:

```text
http://127.0.0.1:8080/v1
```

Use the relay token from `airelays init` as the client credential. That works directly with standard OpenAI SDKs because they send `Authorization: Bearer <api-key>` to whichever `base_url` you configure.

Shell example:

```bash
export OPENAI_BASE_URL='http://127.0.0.1:8080/v1'
export OPENAI_API_KEY="$(tr -d '\n' < ~/.airelays/relay-token)"
```

Open local relay mode with a placeholder client key:

```bash
export OPENAI_BASE_URL='http://127.0.0.1:8080/v1'
export OPENAI_API_KEY='local-open-relay'
```

## Show The Relay Token

Show the current relay token:

```bash
airelays token show
```

Use `airelays token show --json` when you need the current token in machine-readable form.

## Rotate The Relay Token

```bash
airelays token rotate
```

After rotation, update your clients to use the new token. New requests start using the rotated token immediately.
Use `airelays token rotate --json` if you need the new token in machine-readable form.

## Inspect Subscription Windows

Once the server is running:

```bash
curl \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  http://127.0.0.1:8080/v1/subscription/status
```

For the normalized payload plus the raw upstream body:

```bash
curl \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  'http://127.0.0.1:8080/v1/subscription/status?raw=true'
```

If you launched with `--no-auth`, those same requests work without the `Authorization` header after `airelays login` has created an upstream session.

See [Subscription Status](subscription-status.md) for field details.

## Useful Commands

```bash
airelays init
airelays status
airelays login
airelays logout
airelays token show
airelays token rotate
```
