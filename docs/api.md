# API Notes

## Compatibility Adaptations (read this first)

The verified upstream is the ChatGPT subscription backend, not the public
platform API. AIRelays adapts requests on the three text-generation routes
(`/v1/responses`, `/v1/chat/completions`, `/v1/completions`) rather than
letting them fail. Parameter stripping is always disclosed; other
compatibility normalizations are documented here and may log dedicated
adaptation records when the request shape itself is repaired:

- **Removed unsupported parameters:** `temperature`, `top_p`,
  `presence_penalty`, and `frequency_penalty` are stripped before the
  upstream call because the upstream rejects them
  (`"Unsupported parameter: temperature"`). Output-token limit fields
  (`max_tokens`, `max_completion_tokens`, `max_output_tokens`) and
  caller-supplied end-user identifiers (`user`, `safety_identifier`) are
  also stripped because the verified subscription backend does not honor
  them. The names of removed parameters are returned in the
  `x-airelays-ignored-parameters` response header and logged as a
  `compatibility_adaptation` traffic record with the reason. Generation
  runs with the upstream's own defaults. The same adaptation applies on
  the Claude routes: the local `claude` CLI exposes no sampling or token
  limit controls, so these parameters are stripped and disclosed there
  too instead of failing the request.
- **Reasoning effort:** `reasoning_effort` (chat completions and
  completions) and `reasoning.effort` (responses) are forwarded verbatim
  to OpenAI models; on `claude:*` models `reasoning_effort` maps to the
  local CLI's `--effort` flag on both text routes. An explicit JSON `null`
  is treated as absent. Each model's supported modes and default are published
  in `/v1/models` under `airelays.reasoning`. Unsupported Claude values are
  rejected with the supported list (the CLI would silently ignore them);
  unsupported OpenAI values surface the upstream's own error. Modes and
  defaults come from the provider catalog when available and vary by model.
  Omitting effort leaves the choice to the provider; Claude can use an
  adaptive default. An empty Claude modes list means no effort parameter
  is advertised for that model.
- **Structured outputs:** `response_format.type=json_schema` is forwarded
  (translated) to OpenAI models; on `claude:*` chat completions both
  `json_schema` and `json_object` are honored via the claude CLI's native
  `--json-schema` enforcement. Supported types per model are published in
  `/v1/models` under `airelays.structured_output`.
- **Cursor chat-route compatibility:** AIRelays accepts two malformed
  request families currently observed from Cursor custom OpenAI endpoints
  on `/v1/chat/completions`: full Responses-style bodies (`input`,
  `instructions`, flat `tools`, etc.) and flat Responses-style `custom`
  tool definitions / tool choices / assistant tool calls (for example the
  `ApplyPatch` tool). AIRelays normalizes those requests locally, sends the
  canonical Responses shape upstream, and translates upstream
  `custom_tool_call` items back into chat-completions `tool_calls` on both
  streaming and non-streaming responses. Mixed-shape requests that contain
  both `messages` and `input`, or tool outputs that do not reference a
  preceding assistant tool call in the same request, are rejected loudly.
- **Rejected loudly instead of adapted:** `store=true`, `n>1`, and
  `best_of`/`echo`/`logprobs`/`suffix` on `/v1/completions`. These change
  semantics in ways silent stripping would hide, so they return a clear
  error.
- **OpenAI model admission:** when AIRelays can fetch the live ChatGPT
  Codex model catalog for the authenticated OpenAI account(s), OpenAI
  requests are forwarded only for model ids that appear in that catalog
  or in `[providers.openai].extra_models`. Other ids are rejected locally
  with a clear 422 and suggestions from `/v1/models`, instead of failing
  later upstream with an account-scoped unsupported-model error. In
  multi-account mode, the same cache is invalidated when the enrolled
  OpenAI account set changes, so admission follows the union of their
  catalogs. Routing and failover use only the accounts listing the model,
  even when all supporting accounts are cooling down. Explicit unlisted
  `extra_models` overrides remain eligible across the pool; if an override
  appears in an account catalog, its discovered subset takes precedence.
- **Account affinity:** with multiple OpenAI accounts, a conversation is
  pinned to the account that served its first turn (protects upstream
  prompt caching); failover to another account happens only at turn
  boundaries, logged as an `account_failover` traffic record.
- **Upstream failures and automatic retry:** failed OpenAI calls are
  retried automatically with exponential backoff (`retry_attempts`,
  default 3 retries waiting 5s/20s/60s; each retry re-runs account
  failover) while no response byte has reached the client —
  non-streaming requests and the pre-header phase of streaming ones. On
  final failure the client gets OpenAI-shaped error JSON
  (`{"error": {...}}`) with the real HTTP status: the upstream's own code
  (e.g. `server_is_overloaded`) and, for quota errors,
  `resets_in_seconds`. After a stream has started, failures surface as an
  in-band `data: {"error": ...}` event (chat/completions) or a verbatim
  `response.failed`/`error` event (responses passthrough); mid-stream
  failures are not retried. Retries are logged as `retry_backoff` traffic
  records (`retry_skipped` explains deliberate non-retries), and upstream
  failure events as `upstream_stream_error` records.

## `GET /v1/models`

Returns an OpenAI-style models list built from the enabled provider runtimes.

- OpenAI models come from the authenticated ChatGPT/Codex upstream catalog. The default client version setting, `auto`, follows the installed `codex --version`, with a tested version floor when the CLI is missing or older. Model names are not hard-coded into automatic discovery.
- Claude models come from the installed CLI's initialization catalog, including its alias resolutions and per-model reasoning modes. Discovery sends no generation prompt. Both `claude:*` aliases and discovered concrete `claude-*` ids are accepted.
- models starting with `claude:` or `claude-` route to the Claude runtime when it is enabled
- other model ids route to the OpenAI runtime when it is enabled
- Each model record includes an `airelays` extension block with provider identity, route capabilities, a `reasoning` block (`parameter`, supported `modes`, `default`), and a `structured_output` block (`parameter`, supported `types` for `response_format` on chat completions).
- Provider catalogs are cached in memory for
  `models_cache_ttl_seconds` seconds. The default is 300 seconds; `0`
  disables the cache.
- Cached OpenAI model lists are scoped to the current local OpenAI auth account
  and ignored after logout or account changes.
- `GET /v1/models?refresh=true` bypasses the provider catalog caches, including all OpenAI account catalogs. The desktop Refresh button uses this parameter, and the open Models tab also reloads every five minutes while the relay is reachable.
- `airelays.discovery_source` identifies `upstream_catalog`, `claude_cli`, or a `configured` override. `airelays.upstream_model` is the selector forwarded upstream. `airelays.resolved_model` reports the concrete Claude model when the CLI supplies it; `airelays.display_name` carries its provider label. Aliases keep following the CLI's selection; use a concrete id to pin a model version.
- Configured OpenAI `extra_models` and Claude `models` extend discovery. They are retained for compatibility and may not have catalog confirmation. A missing or incompatible Claude CLI retains configured ids and the last successful catalog; `providers.claude.models_discovery_error` in relay status reports discovery failures. Failed Claude probes are cached for the same interval to avoid repeatedly launching a failing CLI.
- With several OpenAI accounts, discovery returns the union of successful catalogs and preserves each model's metadata. `airelays.account_availability` reports `supported` and `total` account counts without disclosing account identities. A failed account catalog does not hide models returned by other accounts. Routing retains last-known membership during catalog outages rather than broadening a known subset.
- `airelays.catalog_visibility` and `airelays.description` preserve upstream metadata. Entries marked `hide` appear as **upstream-hidden** in the desktop; AIRelays does not infer their underlying identity or promote them as recommended models.
- Catalog discovery reports provider availability and capabilities; it does not generate a test response for every model. Account limits and upstream availability still apply when serving a request.

## `GET /v1/subscription/status`

Returns a normalized subscription-usage snapshot with per-window usage
percentages, window labels ("5h", "weekly", derived from each window's
duration), and reset times. Which windows appear is plan-dependent
upstream policy; only reported windows are returned.

- default provider is OpenAI (source: `chatgpt.com/backend-api/wham/usage`)
- `?provider=claude` returns Claude subscription usage in the same
  normalized shape (see [Subscription Status](subscription-status.md))
- `?account=<email-or-prefix>` selects one enrolled OpenAI account
- `?all_accounts=true` returns the list shape with one entry per enrolled
  OpenAI account (an entry carries an `error` instead of a `status` when
  that account's usage probe fails)
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
- standard chat-completions `messages` requests supported
- Responses-shaped request bodies also accepted on this route for Cursor
  compatibility and translated back to chat-completions responses
- tool support includes both OpenAI `function` tools and `custom` tools;
  AIRelays also accepts Cursor's flatter Responses-style `custom` tool
  shape on this route

Claude runtime:

- discovered Claude aliases and concrete model ids, plus configured overrides
- text-only `system`, `developer`, `user`, and `assistant` messages
- `stream=true|false`
- no tools
- no files, images, or audio
- no AIRelays local conversation reuse
- `reasoning_effort` supported (`low`, `medium`, `high`, `xhigh`, `max`),
  mapped to the CLI's `--effort` flag; omitted means the model's adaptive
  default
- structured outputs supported: `response_format.type=json_schema` and
  `json_object` map to the CLI's `--json-schema` flag (native schema
  enforcement; `json_object` enforces the permissive `{"type": "object"}`
  schema). The response `content` is the enforced JSON only — a run that
  produces no schema-conforming output fails loudly instead of returning
  prose. On streaming requests the JSON text streams as the content
  deltas. Enforcement runs as an internal tool turn upstream, so
  schema-enforced calls bill some additional output tokens. Unsupported
  `response_format` shapes (including a missing `type`) are rejected with
  a 422; serialized schemas are capped at 200 KB (they travel on the local
  CLI's argv). Two documented leniencies versus OpenAI:
  `json_schema.name` is optional (metadata the CLI does not use), and
  `strict` is ignored because the CLI's native enforcement is always
  strict — clients can only get stricter behavior than asked, never
  weaker.
- sampling parameters stripped and disclosed via
  `x-airelays-ignored-parameters` (the `claude` CLI has no sampling
  controls); other unsupported generation controls rejected locally

## `POST /v1/completions`

OpenAI runtime:

- current AIRelays OpenAI compatibility path

Claude runtime:

- discovered Claude aliases and concrete model ids, plus configured overrides
- text-only prompt-in, text-out
- `stream=true|false`
- no files, images, audio, or tools
- `reasoning_effort` supported (same modes and mapping as chat completions)
- `response_format` rejected loudly (not part of the completions API;
  ignoring it would silently hand unenforced text to a client that asked
  for JSON — use `/v1/chat/completions` for structured outputs)
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
