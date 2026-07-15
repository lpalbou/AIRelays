# API Notes

## Compatibility Adaptations (read this first)

The verified upstream is the ChatGPT subscription backend, not the public
platform API. AIRelays adapts requests on the three text-generation routes
(`/v1/responses`, `/v1/chat/completions`, `/v1/completions`) rather than
letting them fail, and always discloses what it changed:

- **Removed unsupported parameters:** `temperature`, `top_p`,
  `presence_penalty`, and `frequency_penalty` are stripped before the
  upstream call because the upstream rejects them
  (`"Unsupported parameter: temperature"`). Output-token limit fields
  (`max_tokens`, `max_completion_tokens`, `max_output_tokens`) are also
  stripped because the verified subscription backend does not honor them.
  The names of removed parameters are returned in the
  `x-airelays-ignored-parameters` response header and logged as a
  `compatibility_adaptation` traffic record with the reason. Generation
  runs with the upstream's own defaults. The same adaptation applies on
  the Claude routes: the local `claude` CLI exposes no sampling or token
  limit controls, so these parameters are stripped and disclosed there
  too instead of failing the request.
- **Reasoning effort:** `reasoning_effort` (chat completions) and
  `reasoning.effort` (responses) are forwarded verbatim to OpenAI models;
  on `claude:*` models `reasoning_effort` maps to the local CLI's
  `--effort` flag. Each model's supported modes and default are published
  in `/v1/models` under `airelays.reasoning`. Unsupported Claude values are
  rejected with the supported list (the CLI would silently ignore them);
  unsupported OpenAI values surface the upstream's own error. Requests
  that omit the parameter run OpenAI models at upstream effort `none` —
  lower than the `medium` the official ChatGPT apps use — and Claude
  models at their adaptive default.
- **Rejected loudly instead of adapted:** `store=true`, `n>1`, and
  `best_of`/`echo`/`logprobs`/`suffix` on `/v1/completions`. These change
  semantics in ways silent stripping would hide, so they return a clear
  error.
- **Account affinity:** with multiple OpenAI accounts, a conversation is
  pinned to the account that served its first turn (protects upstream
  prompt caching); failover to another account happens only at turn
  boundaries, logged as an `account_failover` traffic record.

## `GET /v1/models`

Returns an OpenAI-style models list built from the enabled provider runtimes.

- OpenAI models come from the verified ChatGPT subscription backend when that runtime is ready.
- Claude models are explicit `claude:*` ids.
- models starting with `claude:` route to the Claude runtime when it is enabled
- other model ids route to the OpenAI runtime when it is enabled
- Each model record includes an `airelays` extension block with provider identity, route capabilities, and a `reasoning` block (`parameter`, supported `modes`, `default`).
- Successful OpenAI upstream model-list responses are cached in memory for
  `models_cache_ttl_seconds` seconds. The default is 300 seconds; `0`
  disables the cache.
- Cached OpenAI model lists are scoped to the current local OpenAI auth account
  and ignored after logout or account changes.

## `GET /v1/subscription/status`

Returns a normalized subscription-usage snapshot with per-window usage
percentages, window labels ("5h", "weekly"), and reset times.

- default provider is OpenAI (source: `chatgpt.com/backend-api/wham/usage`)
- `?provider=claude` returns Claude subscription usage in the same
  normalized shape (see [Subscription Status](subscription-status.md))
- `?account=<email-or-prefix>` selects one enrolled OpenAI account
- `?all_accounts=true` returns one entry per enrolled OpenAI account
  (folds to the single-account shape when only one exists)
- `?raw=true` includes the raw upstream payload (OpenAI only)

`GET /v1/account/rate_limits` is an alias.

## `POST /v1/relay/accounts/refresh`

Re-checks every enrolled OpenAI account's capacity immediately and returns
the refreshed account list. Releases are evidence-gated: an account's
usage-limit hold is lifted only when its fresh usage report shows capacity,
so live traffic can never slip onto a still-exhausted account during the
re-check. Use it when you know an account has recovered and don't want to
wait for the scheduled reset. CLI equivalent: `airelays accounts refresh`.

## `GET /v1/relay/status`

Returns relay diagnostics, provider readiness, provider cache status, and
`requests_total` — the count of real (non-monitoring) requests served by
this process, usable as a lightweight activity signal. OpenAI model-list
cache diagnostics live under `providers.openai.models_cache`.

## CLI Diagnostics

`airelays status` reports local config, relay-token, and provider readiness
state. `airelays doctor` runs the same local checks and also probes the OpenAI
upstream `/models` route plus a tiny `/responses` smoke request when the OpenAI
runtime is enabled and logged in. Use `airelays doctor --skip-response` to skip
the response smoke request. `airelays models` lists every model id the running
relay accepts, grouped by provider (`--json` supported).

## `POST /v1/responses`

OpenAI runtime:

- general OpenAI Responses envelope
- `stream=true|false`
- local conversations
- local files and verified `input_file` forms

Current OpenAI limits:

- `store=true` rejected
- output-token limit fields rejected explicitly

Claude runtime:

- rejected explicitly on this route

## `POST /v1/chat/completions`

OpenAI runtime:

- current AIRelays OpenAI compatibility path

Claude runtime:

- explicit `claude:*` models only
- text-only `system`, `developer`, `user`, and `assistant` messages
- `stream=true|false`
- no tools
- no files, images, audio, or structured outputs
- no AIRelays local conversation reuse
- `reasoning_effort` supported (`low`, `medium`, `high`, `xhigh`, `max`),
  mapped to the CLI's `--effort` flag; omitted means the model's adaptive
  default
- sampling parameters stripped and disclosed via
  `x-airelays-ignored-parameters` (the `claude` CLI has no sampling
  controls); other unsupported generation controls rejected locally

## `POST /v1/completions`

OpenAI runtime:

- current AIRelays OpenAI compatibility path

Claude runtime:

- explicit `claude:*` models only
- text-only prompt-in, text-out
- `stream=true|false`
- no files, images, audio, tools, or structured outputs
- sampling parameters stripped and disclosed via
  `x-airelays-ignored-parameters`; other unsupported generation controls
  rejected locally

## `POST /v1/files`

Local AIRelays file storage for the OpenAI runtime compatibility path.

## `POST /v1/conversations`

Local AIRelays conversation storage for the OpenAI runtime compatibility path.

The Claude runtime is stateless and does not use local conversations.

## Unsupported Routes

These currently return `501 unsupported_error`:

- embeddings
- image generation
- audio
- realtime sessions

Claude models are also rejected on any route that is not part of their published subset.
