# API Notes

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
