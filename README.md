# AIRelays

`AIRelays` is a local OpenAI-compatible HTTP server with provider-scoped runtimes.

- The default runtime uses an AIRelays-owned ChatGPT subscription login.
- An optional experimental Claude runtime uses the local `claude` CLI and its existing subscription auth state.
- AIRelays protects the relay with its own bearer token by default.
- Every transit is logged to hourly JSONL files.

## Independence And Intended Use

- AIRelays is an independent third-party project. It is not affiliated with, endorsed by, or sponsored by any provider.
- Provider and product names are used only to describe compatibility targets and upstream behavior.
- AIRelays is designed for a single user running a local relay for personal convenience.
- AIRelays is not presented as a shared, pooled, multi-user, or resale service.
- The Claude runtime is experimental, local-only, and not presented as a sanctioned provider integration path.

See [DISCLAIMER.md](DISCLAIMER.md).

## Install

From a source checkout:

```bash
python -m pip install .
```

From PyPI:

```bash
python -m pip install airelays
```

## macOS Menu Bar App

A native macOS status-bar app now lives under [macos/AIRelaysMenuBar](macos/AIRelaysMenuBar/README.md).

Build it:

```bash
swift build --package-path macos/AIRelaysMenuBar
```

Run it:

```bash
swift run --package-path macos/AIRelaysMenuBar AIRelaysMenuBar
```

## Quick Start

OpenAI runtime:

```bash
airelays init
airelays login
airelays doctor
airelays serve --port 8080
```

OpenAI runtime in open local relay mode:

```bash
airelays init --no-auth
airelays login
airelays serve --no-auth --port 8080
```

This disables only the AIRelays client-token gate. It does not bypass the upstream ChatGPT login.

Claude experimental runtime:

```bash
airelays init
claude auth login --claudeai
airelays serve --port 8080
```

Claude experimental runtime in headless environments:

```bash
airelays init
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN='YOUR_CLAUDE_TOKEN'
airelays serve --port 8080
```

When Claude experimental mode is enabled, AIRelays keeps the same auth behavior as the rest of the relay. The default protected mode requires the AIRelays bearer token; `--no-auth` starts an open local relay. Claude remains restricted to loopback binding.

## Basic Verification

Run setup and upstream probes before starting the server:

```bash
airelays doctor
```

Use `airelays doctor --skip-response` to skip the tiny `/responses` smoke request.

Public health:

```bash
curl http://127.0.0.1:8080/healthz
```

Protected relay status:

```bash
curl http://127.0.0.1:8080/v1/relay/status \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

OpenAI model listing:

```bash
curl http://127.0.0.1:8080/v1/models \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN'
```

OpenAI text request:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  -H 'content-type: application/json' \
  -d '{
    "model": "gpt-5.5",
    "messages": [{"role": "user", "content": "Reply with exactly: OPENAI AIRelays OK"}]
  }'
```

Claude experimental text request:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  -H 'content-type: application/json' \
  -d '{
    "model": "claude:sonnet",
    "messages": [{"role": "user", "content": "Reply with exactly: CLAUDE AIRelays OK"}]
  }'
```

## Relay Token

Show the current token:

```bash
airelays token show
```

Rotate the current token:

```bash
airelays token rotate
```

Use the relay token as the client credential when you point an OpenAI-compatible SDK at AIRelays.

## Provider Routing

- models starting with `claude:` use the Claude experimental runtime when it is enabled
- other model ids use the OpenAI runtime when it is enabled
- AIRelays rejects requests when the selected runtime is disabled or the route is outside that runtime's published subset

## What AIRelays Exposes

- `GET /v1/models`
- `GET /v1/subscription/status`
- `GET /v1/account/rate_limits`
- `GET /v1/relay/status`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- `POST /v1/completions`
- `POST /v1/files`
- `GET /v1/files`
- `GET /v1/files/{file_id}`
- `GET /v1/files/{file_id}/content`
- `DELETE /v1/files/{file_id}`
- `POST /v1/conversations`
- `GET /v1/conversations/{conversation_id}`
- `POST /v1/conversations/{conversation_id}`
- `DELETE /v1/conversations/{conversation_id}`
- `/no-tools/v1/models`
- `/no-tools/v1/responses`
- `/no-tools/v1/chat/completions`
- `/no-tools/v1/completions`

## Compatibility Boundary

OpenAI runtime:

- first-class routes: `/v1/responses`, `/v1/chat/completions`, `/v1/completions`
- local files and local conversations are supported
- non-stream responses are reconstructed from streamed upstream events
- `store=true` is rejected
- output-token limit fields are rejected explicitly on the OpenAI-shaped text-generation routes

Claude experimental runtime:

- explicit `claude:*` model ids only
- supported routes: text `/v1/chat/completions` and text `/v1/completions`
- stateless only
- no `/v1/responses`
- no files, images, audio, tools, or structured outputs
- no AIRelays local conversation reuse

## Security Defaults

- default listener: `127.0.0.1:8080`
- protected routes: `/v1/*` and `/no-tools/v1/*`
- public routes: `/` and `GET /healthz`
- protected diagnostics: `GET /v1/relay/status`
- default rate limit: `120` requests/minute with burst `40`
- default concurrent request cap: `8` per IP
- repeated bad tokens trigger a temporary IP block
- Claude experimental mode is loopback-only and follows the relay's protected or open local auth mode

## Configuration

AIRelays reads configuration in this order:

1. CLI flags
2. `AIRELAYS_*` environment variables
3. legacy `OPENAI_ENDPOINT_*` migration variables where supported
4. `~/.config/airelays/config.toml`
5. built-in defaults

Important toggles:

- `AIRELAYS_REQUIRE_BEARER_AUTH`
- `AIRELAYS_BEARER_TOKEN`
- `AIRELAYS_BEARER_TOKEN_FILE`
- `AIRELAYS_ENABLE_OPENAI`
- `AIRELAYS_OPENAI_MODELS_CACHE_TTL_SECONDS`
- `AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL`
- `AIRELAYS_CLAUDE_BIN`
- `AIRELAYS_CLAUDE_MODELS`

## Paths

- config: `~/.config/airelays/config.toml`
- data dir: `~/.airelays`
- logs: `~/.airelays/logs`
- relay token: `~/.airelays/relay-token`
- earlier singular AIRelay paths remain compatible for local upgrades

## More Docs

- [docs/getting-started.md](docs/getting-started.md)
- [docs/configuration.md](docs/configuration.md)
- [docs/security.md](docs/security.md)
- [docs/api.md](docs/api.md)
- [docs/architecture.md](docs/architecture.md)
- [docs/subscription-status.md](docs/subscription-status.md)
- [docs/faq.md](docs/faq.md)
- [docs/troubleshooting.md](docs/troubleshooting.md)
- [docs/disclaimer.md](docs/disclaimer.md)
