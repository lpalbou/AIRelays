---
name: airelays
description: Run and use AIRelays, a local OpenAI-compatible HTTP relay backed by the user's own ChatGPT and Claude subscriptions. Use when you need to install, configure, start, check, or call an AIRelays relay; point an OpenAI SDK (Python, JS/TS, Rust, curl) at it; pick model ids (GPT and `claude:*`); read subscription usage; share the relay with other devices or the internet (`network_scope`); or diagnose its 401/403/422/429/503 errors. Covers the `airelays` CLI (`init`, `login`, `serve`, `status`, `doctor`, `models`, `usage`, `accounts`, `claude`, `token`, `balance`, `logs`) and the `/v1/*` HTTP API.
license: MIT
compatibility: Python 3.11+ on macOS, Linux or Windows. Needs the user's own ChatGPT and/or Claude subscription sign-in (interactive, done by the user), and the `claude` CLI for Claude models.
metadata:
  package-version: "0.22.2"
---

# AIRelays

## Purpose and limits

AIRelays is a single-user local relay: it serves an OpenAI-compatible API
(`/v1/chat/completions`, `/v1/responses`, `/v1/completions`, `/v1/models`,
`/v1/images/*`, files, conversations) and answers it from the user's own
ChatGPT subscription and AIRelays-owned Claude Code profiles. Reach for it when
a tool speaks the OpenAI API and the user wants their subscription, not an API
key, behind it.

It is not the OpenAI platform API. Sampling parameters (`temperature`,
`top_p`, penalties) are stripped; `stop`, `logprobs`, `logit_bias`,
`web_search_options`, `moderation`, `store=true` and `n>1` on text routes are
rejected with 422. Claude models have no audio, no image generation, no
server-side tools, and AIRelays never runs a tool: function calls come back to
the caller. It is not a shared or multi-user service. For workloads that need
those features, use the providers' own APIs.

## Install and verify

```bash
python -m pip install airelays            # or: uv tool install airelays
airelays status | head -1                 # "AIRelays X.Y.Z Status"
airelays serve --help | grep -q -- --network-scope \
  && echo "network_scope supported" || echo "older AIRelays: no network_scope"
```

On macOS and Linux the one-line installer also works:
`curl -fsSL https://raw.githubusercontent.com/lpalbou/AIRelays/v0.22.2/scripts/install-headless.sh | bash`.

Compare the printed version with `metadata.package-version`; when they differ,
the docs of the installed version win. A running relay reports its version
without a token:

```bash
curl -fsS http://127.0.0.1:8080/healthz \
  || echo "no relay answering on 127.0.0.1:8080 (start it with: airelays serve)"
# {"ok":true,"app_name":"AIRelays","version":"X.Y.Z"}
```

The relay token is a credential. Never print it into the conversation; read it
into an environment variable:

```bash
export AIRELAYS_TOKEN="$(tr -d '\n' < ~/.airelays/relay-token)"
```

## Mental model

- **Two separate credentials.** The upstream sign-in (`airelays login` for
  ChatGPT, `airelays claude login` / `set-token` for Claude) lets the relay
  reach the providers. The relay token (`~/.airelays/relay-token`, created by
  `airelays init`) is what clients send as `Authorization: Bearer` / the SDK
  `api_key`. An OpenAI platform key never works here.
- **Model id picks the runtime.** Ids starting with `claude:` or `claude-` go
  to the Claude runtime; every other id goes to the OpenAI runtime. Take ids
  from `GET /v1/models` or `airelays models`, never from memory.
- **Accounts and quota.** Several of the user's own accounts per provider can
  be enrolled; the relay balances across them (`balance = "balanced"` by
  default). Every request spends subscription quota. `airelays.pricing` in
  `/v1/models` is the provider's API list price, only a relative cost hint:
  subscriptions are not billed per token.
- **Listener and network scope.** CLI default `127.0.0.1:8080` (this machine
  only); desktop app default `0.0.0.0:8317`. With a non-loopback listener,
  `[security] network_scope` admits `private` networks (default) or `any`
  address (API key required). Out-of-scope clients get 403 before auth.
- **Silent time costs.** Failed upstream OpenAI calls are retried 3 times
  (5 s, 20 s, 60 s) before the error returns, so a failing request can take
  over a minute: set client timeouts above that. `airelays doctor` sends a
  tiny `/responses` request (quota); `--skip-response` avoids it.
- **Error contract.** Every error is OpenAI's error object
  `{"error": {"message", "type", "param", "code"}, "detail"}` with the real
  HTTP status, so OpenAI SDKs raise their usual status exceptions. Ignored
  parameters are listed in the `x-airelays-ignored-parameters` response
  header; every response has `x-request-id`.
- **Reasoning fallback is opt-in.** `[providers.openai] reasoning_fallback = true`
  permits one same-account retry at the lowest advertised reasoning level only
  after upstream explicitly rejects `none` with `reasoning_effort_not_allowed`.
  It can use more reasoning tokens. Check `airelays.reasoning_fallback` in the
  response (final chat/completion stream chunk) and the traffic log for the
  requested and effective levels. A failed fallback is returned without another
  automatic retry. Leave the option false when exact reasoning settings matter.

## Capability map

| Task | Entry point |
| --- | --- |
| Create config and relay token | `airelays init` (`--host`, `--port`, `--network-scope`, `--no-auth`, `--force`) |
| Sign in (user does it) | `airelays login` (`--device` headless), `airelays claude login`, `airelays claude set-token` |
| Run the relay | `airelays serve` (`--host`, `--port`, `--network-scope`, `--balance`, `--no-auth`) |
| Check setup | `airelays status [--json]`, `airelays doctor [--skip-response] [--json]` |
| List models, prices | `airelays models [--json]`, `GET /v1/models` |
| Generate text | `POST /v1/chat/completions`, `/v1/responses`, `/v1/completions` |
| Generate or edit images (GPT only) | `POST /v1/images/generations`, `/v1/images/edits` (`gpt-image-2`) |
| Read usage | `airelays usage [--json]`, `GET /v1/subscription/status` (`?provider=claude`, `?all_accounts=true`) |
| Manage accounts | `airelays accounts`, `airelays logout <email>`, `airelays claude accounts` |
| Usage resets (OpenAI) | `airelays accounts resets`, `airelays accounts redeem <email>` |
| Switch balancing live | `airelays balance [strategy] [--provider claude]`, `PUT /v1/relay/balance` |
| Relay token | `airelays token show`, `airelays token rotate` |
| Traffic-log retention | `airelays logs`, `GET`/`PUT /v1/relay/logging` |

Not available: provider fallback between OpenAI and Claude (choose the model
yourself), tool execution, multi-user accounts; embeddings, audio, image
variations and realtime return `501 unsupported_error` (SDKs do not retry it);
image masks return 422. Side effects: config at `~/.config/airelays/config.toml`;
sign-ins, token, uploads, conversations and traffic logs under `~/.airelays`
(logs contain prompts and outputs; 7 days / 1 GiB by default). One relay per
data directory.

## Recipes

### End-to-end: first working request

```bash
airelays init                       # config + relay token (prints "Who can connect")
airelays login                      # USER step: browser sign-in; on SSH use --device
airelays doctor --skip-response     # setup checks without spending quota
airelays serve                      # foreground; run it in the background or another terminal
```

Then, with the relay running:

```bash
airelays models          # ids per provider, reasoning modes, list prices; exits 1 with the reason on failure
```

A 503 `upstream_auth_missing` means no ChatGPT sign-in (ask the user to run
`airelays login`); no `claude:*` ids are listed until a Claude account is
connected, and a `claude:*` request then returns 503 `provider_auth_error`.

### Call the relay from the OpenAI Python SDK

```python
# pip install openai
import os
import openai
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key=os.environ["AIRELAYS_TOKEN"],
                timeout=180)
try:
    response = client.chat.completions.create(
        model="<MODEL_ID>",  # from /v1/models, e.g. a GPT id or "claude:sonnet"
        messages=[{"role": "user", "content": "Say hello in one sentence."}],
    )
    print(response.choices[0].message.content)
except openai.APIConnectionError:
    print("relay not reachable: is `airelays serve` running on that address?")
except openai.APIStatusError as error:
    print(error.status_code, error.code, error.message)  # code: e.g. invalid_api_key
```

Success prints the answer. `finish_reason == "length"` means the request's
output-token limit was reached. Use the same `base_url` and key with any
OpenAI SDK; in open mode (`--no-auth`) any non-empty key works.

### Pick a model and reasoning effort

Each `/v1/models` entry has an `airelays` block: `provider`,
`reasoning.modes` and `reasoning.default` (values for `reasoning_effort`),
`structured_output.types` (`response_format` types on chat), `resolved_model`
(the concrete model behind a `claude:*` alias) and `pricing` (or `null`).
Send `reasoning_effort` only with a listed mode: Claude models reject other
values with 422, and OpenAI models forward them to the upstream unchanged.

Headless and desktop servers share OpenAI discovery. Codex is optional:
`[upstream] client_version = "auto"` checks PATH and common installations
(including NVM and the Codex desktop client), then uses a bundled floor
when no client is installed or the installed version is older. Explicit
version pins can select an older catalog. Use `GET /v1/models?refresh=true`
to refresh every account's catalog. `airelays.account_availability` reports
`supported` and `total` counts; balancing and failover stay within the
requested model's supporting accounts. Never substitute a personal Codex
cached catalog for the relay's own account catalogs.

### Check remaining quota before a large job

```bash
airelays usage --json        # running relay: every window per account, image quota included
```

`used_percent` per window with reset times; percentages are not token counts.

### Serve other devices or the internet

```bash
airelays serve --host 0.0.0.0                          # private networks (default scope)
airelays serve --host 0.0.0.0 --network-scope any      # anyone; API key stays required
airelays status                                        # "Who can connect" shows the enforced scope
```

`airelays init --host 0.0.0.0 --network-scope any` writes these into a new
config only (add `--force` to rewrite an existing one). Internet use also
needs router port forwarding and a TLS reverse proxy or VPN; confirm with the
user before choosing `any`.

## Rules and sharp edges

- **Confirm with the user first:** any sign-in (interactive, theirs);
  `airelays accounts redeem` (spends a usage-reset credit; `--yes` skips its
  prompt); `network_scope = "any"` or `--no-auth` (exposes their quota);
  `airelays token rotate` (breaks every configured client);
  `airelays logs` / `PUT /v1/relay/logging` with smaller limits (deletes logs
  permanently); `PUT /v1/relay/balance` (changes routing for every client).
- Every generation spends the user's subscription quota. Answer "does it
  work" with `/healthz`, `airelays status`, `airelays doctor --skip-response`
  and `/v1/models` first.
- Do not send `temperature`, `stop`, `logprobs`, `n>1` or `store=true`
  expecting them to apply; check `x-airelays-ignored-parameters` when output
  ignores a parameter.
- `--no-auth` together with `network_scope = "any"` on a non-loopback
  listener is refused at startup. A loopback listener always narrows the
  scope to this machine.
- `airelays init` on an existing config does not rewrite it without `--force`.
- Only one relay per data directory: if the desktop app runs, it already
  runs the relay (default port 8317); a second `airelays serve` exits with
  status 75. Use the running one or a separate `--data-dir`.
- `trust_x_forwarded_for = true` is refused while the Claude runtime is
  enabled.

## When things fail

| Symptom | Cause | Fix |
| --- | --- | --- |
| 401 `invalid_api_key` | missing or wrong relay token | re-read `~/.airelays/relay-token`; check the base URL ends in `/v1` |
| 429 `ip_temporarily_blocked` | repeated bad tokens | fix the token, wait for `Retry-After` |
| 403 `client_not_on_private_network` / `client_not_on_this_machine` | client outside `network_scope` | connect from an allowed network, or widen the scope with the user's consent |
| 503 `upstream_auth_missing` / `provider_auth_error` | no ChatGPT login / no Claude account | ask the user to run `airelays login` / `airelays claude login` |
| 422 naming a parameter | the relay cannot honor it | remove it; see the capability limits above |
| 404 `model_not_found` | id not in the catalog | pick an id from `/v1/models` |

Everything else: <https://raw.githubusercontent.com/lpalbou/AIRelays/v0.22.2/docs/troubleshooting.md>.

## Go deeper

- Getting started: <https://raw.githubusercontent.com/lpalbou/AIRelays/v0.22.2/docs/getting-started.md>
- API notes (routes, fields, errors): <https://raw.githubusercontent.com/lpalbou/AIRelays/v0.22.2/docs/api.md>
- GPT vs Claude capability matrix: <https://raw.githubusercontent.com/lpalbou/AIRelays/v0.22.2/docs/capabilities.md>
- Network scope, auth, internet setup: <https://raw.githubusercontent.com/lpalbou/AIRelays/v0.22.2/docs/security.md>
- Configuration keys and environment variables: <https://raw.githubusercontent.com/lpalbou/AIRelays/v0.22.2/docs/configuration.md>
- Whole doc set in one file: <https://raw.githubusercontent.com/lpalbou/AIRelays/v0.22.2/llms-full.txt>
