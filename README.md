# AIRelays

`AIRelays` is a local OpenAI-compatible HTTP server.

- The runtime uses an AIRelays-owned ChatGPT subscription login.
- AIRelays protects the relay with its own bearer token by default.
- Every transit is logged to hourly JSONL files.

## Independence And Intended Use

- AIRelays is an independent third-party project. It is not affiliated with, endorsed by, or sponsored by any provider.
- Provider and product names are used only to describe compatibility targets and upstream behavior.
- AIRelays is designed for a single user running a local relay for personal convenience.
- AIRelays is not presented as a shared, pooled, multi-user, or resale service.

See [DISCLAIMER.md](DISCLAIMER.md).

## Install

AIRelays ships two ways; both drive the same relay and share the same
config (`~/.config/airelays`) and data (`~/.airelays`).

### CLI / server install (PyPI)

For headless machines, servers, or terminal-first workflows:

```bash
python -m pip install airelays
```

Or from a source checkout:

```bash
python -m pip install .
```

### Desktop app (GUI + system tray)

A cross-platform tray app (macOS, Windows, Linux) lives under
[desktop/](desktop/README.md): a dashboard with relay start/stop, auth and
network modes, OpenAI sign-in/sign-out, per-account usage bars,
a model list with copy-ready ids, live traffic, and diagnostics. The tray
icon shows connection state and blinks on request activity; the app can
start at login, starts the relay when it opens, and restarts a crashed
relay automatically. Installers (DMG, NSIS, AppImage, deb) build from
`.github/workflows/desktop.yml`; locally:

```bash
cd desktop
./scripts/bundle_runtime.sh
npm install && npm run build
```

An earlier native macOS status-bar app remains available under
[macos/AIRelaysMenuBar](macos/AIRelaysMenuBar/README.md):

```bash
swift build --package-path macos/AIRelaysMenuBar
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

Headless / server install (SSH, no browser):

```bash
airelays init
airelays login --device
airelays doctor
airelays serve --port 8080
```

Device-code login prints a short code you approve from a browser on any
other device (laptop, phone). On SSH sessions and displayless Linux,
`airelays login` selects it automatically. Do not paste the browser-flow
URL into a browser on another computer: its sign-in redirect only works on
the machine running the relay (see
[docs/troubleshooting.md](docs/troubleshooting.md) for the SSH-tunnel
alternative).

OpenAI runtime in open local relay mode:

```bash
airelays init --no-auth
airelays login
airelays serve --no-auth --port 8080
```

This disables only the AIRelays client-token gate. It does not bypass the upstream ChatGPT login.

## Basic Verification

Run setup and upstream probes before starting the server:

```bash
airelays doctor
```

List the model ids the running relay accepts:

```bash
airelays models
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

## Multiple OpenAI Accounts

One person can enroll several of their own OpenAI subscriptions and let the
relay balance across them. Signing in again with a different account adds it
(the previous sign-in is kept, never overwritten):

```bash
airelays login            # first account
airelays login            # second account — added alongside the first
airelays accounts         # list accounts and the commands to manage them
airelays logout perso@gmail.com          # sign one account out
airelays accounts order work@company.com perso@gmail.com   # change priority
```

`airelays accounts` is the hub: it lists your accounts in balancing order and
prints the exact commands to add, sign out, or reorder them. In the desktop
app, each account row has a sign-out button and the "Add account" button
offers both browser and code (headless) sign-in.

By default AIRelays uses the first account until it reaches its usage
limit, then continues with the next, and returns when the limit resets.
Set `[providers.openai] balance = "round_robin"` to spread requests evenly
across healthy accounts instead. Failed-over requests are logged with the
serving account, and `/v1/subscription/status?all_accounts=true` reports
usage per account.

Multiple accounts exist so one user can use their own subscriptions from
one relay; it is not a mechanism for sharing or pooling access between
people (see [DISCLAIMER.md](DISCLAIMER.md)).

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

- all model ids route to the OpenAI runtime
- AIRelays rejects requests when the runtime is disabled or the route is outside its published subset

## What AIRelays Exposes

- `GET /v1/models`
- `GET /v1/subscription/status`
- `GET /v1/account/rate_limits`
- `GET /v1/relay/status`
- `POST /v1/relay/accounts/refresh`
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

## What The Relay Changes (Compatibility Layer)

The ChatGPT subscription backend is not the public OpenAI platform API, so
AIRelays adapts some requests instead of letting them fail. Every
adaptation is visible: it is logged as a `compatibility_adaptation` record
in the traffic logs and reported in the `x-airelays-ignored-parameters`
response header.

**Sampling parameters are removed.** The upstream rejects `temperature`,
`top_p`, `presence_penalty`, and `frequency_penalty` outright
(`"Unsupported parameter: temperature"`). AIRelays strips them so standard
SDK calls keep working; generation then runs with the upstream's own
sampling defaults, which cannot be overridden.

**Reasoning effort is forwarded, not invented.** `reasoning_effort` (chat
completions) and `reasoning: {"effort": ...}` (responses) pass through to
the upstream unchanged. When a request does not set one, the upstream runs
reasoning models at effort `none` — noticeably below what the official
apps use (`medium`). For quality comparable to the ChatGPT apps, set it
explicitly:

```bash
curl http://127.0.0.1:8317/v1/chat/completions \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  -H 'content-type: application/json' \
  -d '{
    "model": "gpt-5.5",
    "reasoning_effort": "medium",
    "messages": [{"role": "user", "content": "..."}]
  }'
```

**Conversations stick to one account.** With multiple OpenAI accounts, a
conversation keeps using the account that served its first turn (preserving
upstream prompt caching); it only fails over to another account at a turn
boundary when the pinned account is at its limit.

## Compatibility Boundary

- first-class routes: `/v1/responses`, `/v1/chat/completions`, `/v1/completions`
- local files and local conversations are supported
- non-stream responses are reconstructed from streamed upstream events
- `store=true` is rejected
- output-token limit fields are rejected explicitly on the OpenAI-shaped text-generation routes

## Security Defaults

- default listener: `127.0.0.1:8080`
- protected routes: `/v1/*` and `/no-tools/v1/*`
- public routes: `/` and `GET /healthz`
- protected diagnostics: `GET /v1/relay/status`
- default rate limit: `120` requests/minute with burst `40`
- default concurrent request cap: `8` per IP
- repeated bad tokens trigger a temporary IP block

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
