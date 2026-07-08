# Getting Started

This guide covers the CLI/server install. If you prefer a GUI, the desktop
app (macOS, Windows, Linux) wraps the same relay with a system tray,
dashboard, and one-click sign-in — see [desktop/README.md](../desktop/README.md)
and the README's install section.

## Install

From a source checkout:

```bash
python -m pip install .
```

From PyPI:

```bash
python -m pip install airelays
```

## Initialize AIRelays

```bash
airelays init
```

This prepares:

- config: `~/.config/airelays/config.toml`
- data dir: `~/.airelays`
- logs dir: `~/.airelays/logs`
- relay token: `~/.airelays/relay-token`

Show the current relay token at any time:

```bash
airelays token show
```

## OpenAI Runtime

Log in:

```bash
airelays login
```

On a server or over SSH (no local browser), use device-code login — you
approve the sign-in from a browser on any other device:

```bash
airelays login --device
```

`airelays login` selects the device flow automatically on SSH sessions and
displayless Linux. The browser flow's URL only works in a browser on the
same machine as the relay (its redirect targets `localhost:1455` there).

You can enroll several of your own OpenAI accounts: running `airelays login`
again with a different account adds it alongside the first, and the relay
balances across them (see the README's "Multiple OpenAI Accounts" section).
Sign an account out with `airelays logout <email>`; manage order and
capacity holds with `airelays accounts`.

Start the server:

```bash
airelays doctor
airelays serve --host 127.0.0.1 --port 8080
```

Verify:

```bash
curl http://127.0.0.1:8080/v1/models \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'

curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  -H 'content-type: application/json' \
  -d '{
    "model": "gpt-5.5",
    "messages": [{"role": "user", "content": "Reply with exactly: OPENAI AIRelays OK"}]
  }'
```

## OpenAI Open Local Relay Mode

```bash
airelays init --no-auth
airelays login
airelays serve --no-auth --host 127.0.0.1 --port 8080
```

In this mode AIRelays does not require `Authorization` on `/v1/*`.

## Status

Inspect relay and provider readiness:

```bash
airelays status
```

Run local setup checks plus live upstream probes:

```bash
airelays doctor
```

`airelays doctor` checks config, relay-token state, OpenAI login readiness,
upstream `/models`, and a tiny `/responses` smoke request. Use
`airelays doctor --skip-response` to skip the response smoke request.

List every model id the running relay accepts:

```bash
airelays models
```

Machine-readable output:

```bash
airelays status --json
airelays doctor --json
airelays models --json
```

`airelays status` shows:

- relay config and token state
- OpenAI runtime readiness
- next recommended commands

## Provider Routing

- all model ids route to the OpenAI runtime
- AIRelays rejects requests when the runtime is disabled or the route is outside its published subset

## Client Configuration

Base URL:

```text
http://127.0.0.1:8080/v1
```

Standard OpenAI SDKs can use the AIRelays relay token through their normal `api_key` field when the `base_url` points at AIRelays.

Example shell setup:

```bash
export OPENAI_BASE_URL='http://127.0.0.1:8080/v1'
export AIRELAYS_TOKEN="$(tr -d '\n' < ~/.airelays/relay-token)"
```

## Subscription Status

OpenAI subscription usage:

```bash
curl http://127.0.0.1:8080/v1/subscription/status \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

See [Subscription Status](subscription-status.md) for multi-account
parameters and the payload shape.
