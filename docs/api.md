# API Notes

## Compatibility Adaptations (read this first)

The verified upstream is the ChatGPT subscription backend, not the public
platform API. AIRelays adapts requests on the three text-generation routes
(`/v1/responses`, `/v1/chat/completions`, `/v1/completions`) rather than
letting them fail, and always discloses what it changed:

- **Removed sampling parameters:** `temperature`, `top_p`,
  `presence_penalty`, and `frequency_penalty` are stripped before the
  upstream call because the upstream rejects them
  (`"Unsupported parameter: temperature"`). The names of removed
  parameters are returned in the `x-airelays-ignored-parameters` response
  header and logged as a `compatibility_adaptation` traffic record with the
  reason. Generation runs with the upstream's own sampling defaults.
- **Reasoning effort:** `reasoning_effort` (chat completions) and
  `reasoning.effort` (responses) are forwarded verbatim. Requests that omit
  it run at upstream effort `none`, which is lower than the `medium` the
  official ChatGPT apps use for the same models — set it explicitly when
  comparing model quality.
- **Rejected loudly instead of adapted:** `store=true`, output-token limit
  fields (`max_tokens`, `max_completion_tokens`, `max_output_tokens`),
  `n>1`, and `best_of`/`echo`/`logprobs`/`suffix` on `/v1/completions`.
  These change semantics in ways silent stripping would hide, so they
  return a clear error.
- **Account affinity:** with multiple OpenAI accounts, a conversation is
  pinned to the account that served its first turn (protects upstream
  prompt caching); failover to another account happens only at turn
  boundaries, logged as an `account_failover` traffic record.

## `GET /v1/models`

Returns an OpenAI-style models list built from the enabled provider runtimes.

- OpenAI models come from the verified ChatGPT subscription backend when that runtime is ready.
- Claude experimental models are explicit `claude:*` ids.
- models starting with `claude:` route to the Claude experimental runtime when it is enabled
- other model ids route to the OpenAI runtime when it is enabled
- Each model record includes an `airelays` extension block with provider identity and route capabilities.
- Successful OpenAI upstream model-list responses are cached in memory for
  `models_cache_ttl_seconds` seconds. The default is 300 seconds; `0`
  disables the cache.
- Cached OpenAI model lists are scoped to the current local OpenAI auth account
  and ignored after logout or account changes.

## `GET /v1/subscription/status`

Returns the current OpenAI subscription snapshot from `chatgpt.com/backend-api/wham/usage`.

- verified for the OpenAI runtime only
- `?raw=true` includes the raw upstream payload

`GET /v1/account/rate_limits` is an alias.

## `GET /v1/relay/status`

Returns relay diagnostics, provider readiness, and provider cache status.
OpenAI model-list cache diagnostics live under `providers.openai.models_cache`.

## CLI Diagnostics

`airelays status` reports local config, relay-token, and provider readiness
state. `airelays doctor` runs the same local checks and also probes the OpenAI
upstream `/models` route plus a tiny `/responses` smoke request when the OpenAI
runtime is enabled and logged in. Use `airelays doctor --skip-response` to skip
the response smoke request.

## `POST /v1/responses`

OpenAI runtime:

- general OpenAI Responses envelope
- `stream=true|false`
- local conversations
- local files and verified `input_file` forms

Current OpenAI limits:

- `store=true` rejected
- output-token limit fields rejected explicitly

Claude experimental runtime:

- rejected explicitly on this route

## `POST /v1/chat/completions`

OpenAI runtime:

- current AIRelays OpenAI compatibility path

Claude experimental runtime:

- explicit `claude:*` models only
- text-only `system`, `developer`, `user`, and `assistant` messages
- `stream=true|false`
- no tools
- no files, images, audio, or structured outputs
- no AIRelays local conversation reuse
- unsupported generation controls rejected locally

## `POST /v1/completions`

OpenAI runtime:

- current AIRelays OpenAI compatibility path

Claude experimental runtime:

- explicit `claude:*` models only
- text-only prompt-in, text-out
- `stream=true|false`
- no files, images, audio, tools, or structured outputs
- unsupported generation controls rejected locally

## `POST /v1/files`

Local AIRelays file storage for the OpenAI runtime compatibility path.

## `POST /v1/conversations`

Local AIRelays conversation storage for the OpenAI runtime compatibility path.

The Claude experimental runtime is stateless and does not use local conversations.

## Unsupported Routes

These currently return `501 unsupported_error`:

- embeddings
- image generation
- audio
- realtime sessions

Claude experimental models are also rejected on any route that is not part of their published subset.
