# Getting Started

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

In this mode AIRelays does not require `Authorization` on `/v1/*`. Open local relay mode applies to all enabled providers, including Claude experimental mode.

## Claude Experimental Runtime

Browser-based local Claude login:

```bash
claude auth login --claudeai
```

Headless Claude login:

```bash
airelays init
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN='YOUR_CLAUDE_TOKEN'
```

Start AIRelays with the Claude runtime enabled:

```bash
airelays serve --host 127.0.0.1 --port 8080
```

Verify:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  -H 'content-type: application/json' \
  -d '{
    "model": "claude:sonnet",
    "messages": [{"role": "user", "content": "Reply with exactly: CLAUDE AIRelays OK"}]
  }'
```

Current Claude limits:

- local-only
- loopback-only
- bearer-auth-required
- text-only
- stateless

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
upstream `/models`, a tiny `/responses` smoke request, and Claude readiness
when the experimental runtime is enabled. Use `airelays doctor --skip-response`
to skip the response smoke request.

Machine-readable output:

```bash
airelays status --json
airelays doctor --json
```

`airelays status` shows:

- relay config and token state
- OpenAI runtime readiness
- Claude runtime readiness when enabled
- next recommended commands

## Provider Routing

- models starting with `claude:` use the Claude experimental runtime when it is enabled
- other model ids use the OpenAI runtime when it is enabled
- AIRelays rejects requests when the selected runtime is disabled or the route is outside that runtime's published subset

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

OpenAI subscription status:

```bash
curl http://127.0.0.1:8080/v1/subscription/status \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

This route is verified for the OpenAI runtime only.
