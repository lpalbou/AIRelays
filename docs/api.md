# API Notes

## Traffic log retention API

`GET /v1/relay/balance` returns the account-routing strategy of each provider pool
(`openai`, `claude`) and the accepted names; `PUT /v1/relay/balance` with
`{"openai": "ping_pong"}` and/or `{"claude": ...}` switches the running relay's
strategy for the next request, no restart (runtime only: the config file and
`AIRELAYS_*_BALANCE` still decide the next start; 422 on an unknown name).
`airelays balance [strategy] [--provider claude]` is the CLI front for both.

`GET /v1/relay/logging` returns current policy and disk usage without deleting
logs. `PUT /v1/relay/logging` validates, persists and applies a partial policy
update immediately; omitted fields retain their saved values. These routes use
the same bearer-token gate and rate limits as the other `/v1/*` routes.

Example PUT body (all values must be integers; sizes are MiB):

```json
{"retention_days": 30, "max_total_mb": 1024, "max_file_mb": 50}
```

Both responses contain `policy`, `policy_source` (`saved` or `config`, including
built-in defaults), `policy_path`, `logs_dir`, `usage_bytes`, `file_count`,
`over_budget`, `cleanup_interval_seconds`, `last_cleanup_at`, `deleted_files`,
`deleted_bytes`, `oversized_records`, `dropped_records`, and `last_error`. Cleanup counters and
errors are local to the process answering the request; disk usage and saved
policy are shared. `last_cleanup_at` is the most recent completed cleanup pass,
which may have encountered deletion errors. It is `null` before a pass completes.

Invalid values, unknown fields, zero size limits, or a file limit larger than
the total budget return 422 without altering the saved policy. Storage failures
preventing policy reads or persistence return 503. A successfully saved policy
returns 200 even if cleanup fails; inspect `last_error` and `over_budget` before
reporting successful reclamation. A policy update permanently deletes eligible
old files. GET requests do not trigger reclamation.

Defaults: 7 days, 1024 MiB total, 50 MiB per file. `retention_days: 0` removes the
age limit but leaves size enforcement enabled. See
[configuration](configuration.md#traffic-log-retention) for bounds, precedence,
rotation behavior, migration, and CLI/tray controls.

For a side-by-side summary of what works on GPT models and on Claude models,
see [Capabilities](capabilities.md).

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
  (`"Unsupported parameter: temperature"`). Caller-supplied end-user
  identifiers (`user`, `safety_identifier`) are also stripped because the
  verified subscription backend does not honor them, as is `seed`
  (best-effort determinism in OpenAI's own API; neither upstream supports
  it). The names of removed parameters are returned in the
  `x-airelays-ignored-parameters` response header and logged as a
  `compatibility_adaptation` traffic record with the reason. Generation
  runs with the upstream's own defaults. The same adaptation applies on
  the Claude routes: the local `claude` CLI exposes no sampling controls,
  so these parameters are stripped and disclosed there too instead of
  failing the request.
- **Output-token limits:** `max_completion_tokens` (preferred) and
  `max_tokens` on chat and completions, and `max_output_tokens` on
  `/v1/responses`, are honoured on both runtimes. GPT models receive them as
  the upstream's `max_output_tokens`, which enforces them (values below 16
  get the upstream's own 400). On Claude models each CLI model call is capped
  and the run stops when the first one reaches the limit, before the CLI
  would continue on its own. A cut answer reports `finish_reason: "length"`
  (`status: "incomplete"` on `/v1/responses`).
- **Unrecognized parameters:** on `/v1/chat/completions` and
  `/v1/completions` (both runtimes), a parameter AIRelays does not
  recognize is dropped and listed in `x-airelays-ignored-parameters` like
  the stripped ones, so nothing is ignored silently. This covers cache
  hints such as `prompt_cache_retention`; on Claude models it also covers
  `verbosity`, `prompt_cache_key`, `metadata` and `service_tier`. A
  parameter sent as `null` means "not set" and is not listed. Names are
  percent-encoded in the header (so any JSON key is a valid header value)
  and the list is capped at about 2 KB, ending in `,...` when cut; the
  traffic log keeps every name. `/v1/responses` forwards unrecognized
  parameters to the upstream as-is.
- **Verbosity and prompt caching:** on OpenAI models, chat
  `verbosity` is forwarded as the Responses `text.verbosity`, and
  `prompt_cache_key` is forwarded as-is.
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
  (translated) to OpenAI models. With `strict: true`, the schema is
  rewritten to OpenAI's strict-mode form: every property becomes required,
  optional ones accept `null`, and `additionalProperties` defaults to
  `false` (a schema that sets it to anything else is rejected). Without
  `strict: true` the schema is forwarded unchanged with `strict: false`,
  OpenAI's default, sent explicitly because the upstream otherwise assumes
  strict mode. The same applies to `text.format` on `/v1/responses`.
  `response_format.type=json_object` is forwarded as the upstream's native
  JSON mode (`text.format: {"type": "json_object"}`), with OpenAI's rule that
  the messages must mention JSON enforced by the upstream. In JSON mode, chat
  system and developer messages are sent as developer input messages rather
  than `instructions`, so a system prompt that mentions JSON counts, as on
  OpenAI. On `claude:*` chat completions both
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
  preceding assistant tool call in the same request (or, with
  `conversation`, an earlier turn), are rejected loudly.
- **Rejected loudly instead of adapted:** `store=true`, `n>1`,
  `best_of`/`echo`/`logprobs`/`suffix` on `/v1/completions`, and on both
  chat and completions (both runtimes) a non-empty `stop`, a true
  `logprobs`, a nonzero `top_logprobs`, or a non-empty `logit_bias`.
  Neither upstream can honor them and dropping them would change the
  output, so they return a clear 422 error. Chat `web_search_options`,
  `moderation`, `audio`, `prediction` and non-text `modalities` are
  rejected the same way; for the first four any non-null value counts,
  since `web_search_options: {}` means "search with defaults". Default values (`null`, `false`, `0`, empty,
  `best_of: 1`, `modalities: ["text"]`) are accepted on both runtimes;
  on `/v1/completions`, an integer `logprobs` (including `0`) is a
  request.
- **Image generation:** `/v1/images/generations` and `/v1/images/edits`
  forward only what the subscription's image service applies (verified
  live): `prompt`, `model`, `background` and `moderation`; `quality` and
  `size` are forwarded too, but the service picks its own. The service
  does not validate the rest, so AIRelays checks every parameter against
  OpenAI's published values first (`422` with the offending `param`),
  produces `n` images with `n` upstream calls, answers `stream: true` with
  one completed event per image, and lists the parameters it dropped
  (`output_format`, `output_compression`, `partial_images`,
  `response_format`, `style`, `user`, `safety_identifier`,
  `input_fidelity`), plus `quality` and `size` whenever the rendered image
  differs from the request, in `x-airelays-ignored-parameters` with a
  `compatibility_adaptation` record. A `mask` is refused (`422`).
  See [`POST /v1/images/...`](#post-v1imagesgenerations-and-post-v1imagesedits).
- **OpenAI model admission:** when AIRelays can fetch the live ChatGPT
  Codex model catalog for the authenticated OpenAI account(s), OpenAI
  requests are forwarded only for model ids that appear in that catalog
  or in `[providers.openai].extra_models`. Other ids are rejected locally
  with 404 `model_not_found` and suggestions from `/v1/models`, instead of failing
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
- Claude models come from the installed CLI's initialization catalog, including its alias resolutions and per-model reasoning modes. Discovery sends no generation prompt. Both `claude:*` aliases and discovered concrete `claude-*` ids are accepted. No Claude models are listed until an AIRelays Claude account is connected.
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

`GET /v1/models/{model}` returns that model's entry from the list (ids match
case-insensitively, as for requests), or 404 `model_not_found`. While the
OpenAI catalog cannot be fetched, OpenAI ids return the catalog error instead.

## `GET /v1/subscription/status`

Returns a normalized subscription-usage snapshot with per-window usage
percentages, window labels ("5h", "weekly", derived from each window's
duration), and reset times. Which windows appear is plan-dependent
upstream policy; only reported windows are returned.

- default provider is OpenAI (source: `chatgpt.com/backend-api/wham/usage`)
- `?provider=claude` returns Claude subscription usage in the same
  normalized shape (see [Subscription Status](subscription-status.md))
- `?account=<email-or-prefix>` selects one enrolled OpenAI account
- `?provider=claude&account=<email-or-id>` selects one Claude account; the
  setup-token account (`default`) returns 422 `unsupported_for_provider`
  because Claude reports no usage for setup tokens
- `?all_accounts=true` returns the list shape with one entry per enrolled
  OpenAI account (an entry carries an `error` instead of a `status` when
  that account's usage probe fails)
- `?raw=true` includes the raw upstream payload (OpenAI only)
- `rate_limits.image_generation` reports the image-generation quota, a
  separate 24-hour upstream bucket the usage endpoint does not include: the
  same window fields, plus `source` (`image_response` or `probe`),
  `observed_at`, and `relay` (the images this relay made in the window). See
  [Subscription Status](subscription-status.md#image-generation-quota)
- `?provider=claude&all_accounts=true` returns
  `{"object": "subscription_status_list", "provider": "claude", "accounts": [...]}`,
  keyed by stable account `slug`. Each entry has `slug`, `email`, and one of
  `status`, `error`, or `usage_unsupported` (the setup-token account); a
  failed reading affects only its entry
- Claude readings come from each profile's Claude Code (`get_usage`). A
  reading without live data keeps the last good values with `stale: true`, its
  age, and `stale_reason` (`usage_unavailable`,
  `nonessential_traffic_disabled`, `cli_unsupported`, `cli_failed`). Without
  any good reading, the account reports an error message instead of numbers

`GET /v1/account/rate_limits` is an alias.

## `POST /v1/relay/accounts/refresh`

Re-checks every enrolled OpenAI account's capacity immediately and returns
the refreshed account list. Releases are evidence-gated: an account's
usage-limit hold is lifted only when its fresh usage report shows capacity,
so live traffic can never slip onto a still-exhausted account during the
re-check. Use it when you know an account has recovered and don't want to
wait for the scheduled reset. CLI equivalent: `airelays accounts refresh`.

With `?provider=claude`, refreshes Claude account usage while respecting each
account's cached readings and persisted backoff.

## `GET /v1/relay/accounts/resets`

Lists each enrolled OpenAI account's usage-limit reset credits: the resets
Codex offers under **Redeem reset** (for example the "Full reset" OpenAI
grants to Codex users), read from the same subscription at
`chatgpt.com/backend-api/wham/rate-limit-reset-credits`. Nothing is
inferred: every fact comes from the upstream listing and the account's
usage report. CLI equivalent: `airelays accounts resets`.

- `?account=<email-or-prefix>` selects one account (slug also accepted)
- `?force=true` bypasses the one-minute cache shared with usage probes
- `?raw=true` includes the raw upstream listing per account

```json
{
  "object": "accounts.resets",
  "available_count": 1,
  "accounts": [
    {
      "slug": "default",
      "email": "you@example.com",
      "resets": {
        "object": "rate_limit_reset_credits",
        "captured_at": "2026-10-18T09:12:03Z",
        "available_count": 1,
        "applicable_available_count": 0,
        "redeemable_now": false,
        "total_earned_count": 0,
        "next_expiry_at": "2026-11-07T00:00:05Z",
        "credits": [
          {
            "id": "RateLimitResetCredit_…",
            "reset_type": "codex_rate_limits",
            "status": "available",
            "supported_by_plan": true,
            "title": "Full reset",
            "description": "…",
            "granted_by": "Codex Team",
            "granted_at": "2026-10-08T00:00:05Z",
            "expires_at": "2026-11-07T00:00:05Z",
            "expires_in_seconds": 1712282,
            "expired": false,
            "redeem_started_at": null,
            "redeemed_at": null
          }
        ]
      }
    }
  ]
}
```

`available_count` is how many credits the account holds;
`applicable_available_count` (from the usage report) is how many the
upstream would apply right now. A credit held while usage is below its
limit is available but not applicable: `redeemable_now` is `false`, and
redeeming it answers `nothing_to_reset` without spending it. An account
whose listing cannot be read carries an `error` string instead of `resets`.

## `POST /v1/relay/accounts/resets/redeem`

Redeems one reset credit now, through AIRelays, without opening an OpenAI
app: the call Codex makes behind **Yes, use reset**
(`chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume`). The
relay then re-reads that account's usage so a lifted limit returns it to
rotation immediately. Irreversible on OpenAI's side; the desktop and the
CLI (`airelays accounts redeem <email>`) ask for confirmation first.

Body (JSON, all fields optional with one enrolled account):

- `account`: email, unambiguous prefix, or slug; **required** when several
  accounts are enrolled (the relay never picks one for you)
- `credit_id`: redeem this specific credit; otherwise the upstream chooses
- `redeem_request_id`: idempotency key forwarded to the upstream; a retry
  with the same key cannot spend a second credit (generated when omitted)

```json
{
  "object": "accounts.reset_redeem",
  "slug": "default",
  "email": "you@example.com",
  "outcome": "reset",
  "known_outcome": true,
  "reset_applied": true,
  "windows_reset": 2,
  "credit_id": "RateLimitResetCredit_…",
  "redeem_request_id": "6d1f…",
  "status": { "object": "subscription_status", "…": "fresh usage after the redeem" }
}
```

`outcome` is the upstream's code verbatim: `reset` (applied;
`windows_reset` says how many windows restarted), `nothing_to_reset` (usage
did not need a reset; credit kept), `no_credit`, or `already_redeemed`.
`reset_applied` is true only for `reset`. `status` is `null` when the
follow-up usage read fails. Errors: 400 when several accounts are enrolled
and none is named, 404 unknown account, 409 ambiguous prefix, 422 malformed
field; upstream rejections pass through with their status.

## `GET /v1/relay/status`

Returns relay diagnostics, provider readiness, provider cache status, and
`requests_total` — the count of real (non-monitoring) requests served by
this process, usable as a lightweight activity signal. OpenAI model-list
cache diagnostics live under `providers.openai.models_cache`.

`providers.claude.accounts` lists AIRelays-owned Claude accounts with their
`slug`, email, readiness, `managed_profile`, `oauth_token_source`, request
cooldown, and `unavailable_models` (models the account cannot serve right now
although its sign-in works, with seconds until AIRelays tries them again). `managed_profile` is `false` only for the setup-token account;
`oauth_token_source` is `profile` or `file`. `providers.claude.balance` reports their selection
strategy. A second ready profile of the same subscription carries
`duplicate_of` and is counted once; an account whose identity differs from its
enrollment is not eligible.

`providers.openai.accounts[]` (or `providers.openai` itself with a single
account) carries `image_usage`: the image-generation quota as last read and
the relay's own image count for the window; `image_limited` and
`image_limited_for_seconds` appear while an account's image bucket is
benched (text requests are unaffected).

`GET /v1/relay/status?activity_only=true` returns only
`{"requests_total": N}`. It requires the same authentication as the full
status, but it does not scan storage or probe providers, is not itself
counted, and does not consume rate-limit quota or a concurrency slot, so
clients such as the desktop tray can poll it frequently.

## CLI Diagnostics

`airelays usage` prints every usage window the running relay reports, per
account and provider, text windows and the image-generation quota alike
(`--json` for the raw payloads).

`airelays status` reports local config, relay-token, and provider readiness
state. `airelays doctor` runs the same local checks and also probes the OpenAI
upstream `/models` route plus a tiny `/responses` smoke request when the OpenAI
runtime is enabled and logged in, and reports a `CLAUDE_CODE_OAUTH_TOKEN` set in
the relay's environment, which AIRelays ignores. Use
`airelays doctor --skip-response` to skip the response smoke request.
`airelays models` lists every model id the running relay accepts, grouped by
provider (`--json` supported). `airelays accounts resets` shows each OpenAI
account's usage resets and `airelays accounts redeem <email>` spends one
through the running relay (see the two `/v1/relay/accounts/resets` routes
above; `--yes` skips the confirmation, `--credit <id>` picks a credit).

## `POST /v1/responses`

OpenAI runtime:

- general OpenAI Responses envelope
- `stream=true|false`
- local conversations
- local files and verified `input_file` forms

Current OpenAI limits:

- `store=true` rejected
- output-token limits enforced upstream (see
  [Compatibility Adaptations](#compatibility-adaptations-read-this-first))

Claude runtime:

- the same request and response shapes, streamed (`response.created`,
  `response.output_text.delta`, `function_call` items,
  `response.completed`) or not, translated to the Claude runtime's chat
  path: `input` (text, `input_image`, `input_file`, `function_call` and
  `function_call_output` items), `instructions`, function `tools`,
  `tool_choice`, `text.format` (`json_schema`, `json_object`) and
  `reasoning.effort`
- `conversation` works as on GPT models; `previous_response_id`,
  `store: true`, `background: true` and stored prompts are refused (Claude
  models keep no responses: use `conversation`)
- log probabilities (`top_logprobs`, or `message.output_text.logprobs` in
  `include`) are refused; other `include` values and `text.verbosity` are
  dropped and listed in `x-airelays-ignored-parameters`
- a `function_call_output` is a string or text parts; image and file parts
  in tool outputs are refused (as in `role: "tool"` messages on chat
  completions)
- function tools run non-strict, since Claude has no strict tool mode
  (`strict: true` is listed in `x-airelays-ignored-parameters`). Server-side
  tools (web search, file search)
  are not available on Claude models
- other unsupported fields, such as `reasoning.summary`, are dropped and
  listed in `x-airelays-ignored-parameters`

## `POST /v1/chat/completions`

OpenAI runtime:

- current AIRelays OpenAI compatibility path
- standard chat-completions `messages` requests supported
- Responses-shaped request bodies also accepted on this route for Cursor
  compatibility and translated back to chat-completions responses
- tool support includes both OpenAI `function` tools and `custom` tools;
  AIRelays also accepts Cursor's flatter Responses-style `custom` tool
  shape on this route
- `tool_choice` accepts `auto`, `none`, `required`, a named function or
  custom tool, and `allowed_tools`
- function tools run non-strict unless `function.strict` is `true`, as in
  OpenAI's chat API; AIRelays sends `strict: false` explicitly because the
  upstream otherwise assumes strict. `strict` must be a boolean here and in
  `response_format.json_schema`
- assistant messages may carry `content: null` alongside `tool_calls` or a
  `refusal`, as OpenAI SDKs send them back
- the deprecated `functions`/`function_call` interface is supported end
  to end: replies carry `message.function_call` (streamed as
  `delta.function_call`) with `finish_reason: "function_call"`, one call
  per reply, and `role: "function"` messages answer the matching call by
  `name`. Send either `tools` or `functions`, not both, and use `tools`
  with `conversation`
- file content parts use OpenAI's shape, `{"type": "file", "file": {...}}`,
  with a local `file_id` from `POST /v1/files` or inline `file_data` plus
  `filename`

Claude runtime:

- discovered Claude aliases and concrete model ids, plus configured overrides
- `system`, `developer`, `user`, `assistant` and `tool` messages
- `stream=true|false`
- images (`image_url`: a PNG, JPEG, GIF or WebP data URL, an http(s) URL,
  or a local `file_id`) and documents (`file` parts: PDF or UTF-8 text, as
  `file_data` or a local `file_id`), in any user message; an image or
  document Claude cannot read returns 422, never a silently degraded answer.
  `detail` has no Claude equivalent
- function tools: Claude decides which of the client's tools to call, and
  the calls come back as OpenAI `tool_calls` with
  `finish_reason: "tool_calls"`; the client runs its tools and sends the
  results back as `tool` messages, as with GPT models. AIRelays never runs
  a tool: the CLI's built-in tools are off, the client's tools are only
  descriptions behind a helper that executes nothing, every call is denied
  before execution, and the run stops at the call. The CLI's startup report
  is checked on every request, and a request stops if any tool appears that
  AIRelays did not define (see [Tool calls on Claude models](#tool-calls-on-claude-models)).
  Refused with 422 because Claude cannot honour them: a forced call
  (`tool_choice` `required` or a named tool), `parallel_tool_calls: false`,
  non-function tools, and `parameters` that are not an object schema.
  `strict: true` tools run non-strict, listed in
  `x-airelays-ignored-parameters` (Claude has no strict tool mode). A
  request whose tools the CLI could not load fails with 502
  `cli_tools_unavailable` instead of answering without them; the
  `/no-tools/` routes refuse tools as on GPT models
- `conversation` works as on GPT models
- no audio
- `reasoning_effort` supported (`low`, `medium`, `high`, `xhigh`, `max`),
  mapped to the CLI's `--effort` flag; omitted means the model's adaptive
  default
- structured outputs supported: `response_format.type=json_schema` maps to
  the CLI's `--json-schema` flag (native schema enforcement). The response
  `content` is the enforced JSON only — a run that produces no
  schema-conforming output fails loudly instead of returning prose. On
  streaming requests the JSON text streams as the content deltas.
  Enforcement runs as an internal tool turn upstream, so schema-enforced
  calls bill some additional output tokens. Unsupported
  `response_format` shapes (including a missing `type`) are rejected with
  a 422; serialized schemas are capped at 200 KB (they travel on the local
  CLI's argv). Two documented leniencies versus OpenAI:
  `json_schema.name` is optional (metadata the CLI does not use), and
  `strict` is ignored because the CLI's native enforcement is always
  strict — clients can only get stricter behavior than asked, never
  weaker.
  `json_object` asks the model, in the system prompt, for a single JSON
  object and checks the answer before returning it (a surrounding code fence
  is removed); an answer that is not one JSON object returns 502
  `invalid_json_output` and does not pause any account. An answer cut short
  (`finish_reason: "length"`) or refused (`content_filter`) is returned as is.
  In JSON mode a streamed answer arrives as one content chunk, because it is
  checked first.
- every Claude request carries a system prompt: the client's, or a minimal
  one when the client sends none, so the Claude CLI's own built-in agent
  prompt never applies.
- sampling parameters stripped and disclosed via
  `x-airelays-ignored-parameters` (the `claude` CLI has no sampling
  controls); other unsupported generation controls rejected locally
- with no AIRelays Claude account connected, `claude:*` requests (here and
  on `/v1/completions`) return 503 `provider_auth_error`: "No Claude account
  is connected to AIRelays. Run `airelays claude login` or
  `airelays claude set-token`."

## `POST /v1/images/generations` and `POST /v1/images/edits`

OpenAI's Images API on the ChatGPT subscription runtime (GPT models only;
`claude:*` models return `422 unsupported_for_provider`). The subscription
serves the GPT image model; `/v1/models` lists it as `gpt-image-2` and the
routes accept any `gpt-image-*` id (the upstream does not report which model
answered). Other ids return `404 model_not_found`.

- `prompt` (required, up to 32,000 characters), `model` (default
  `gpt-image-2`), `background` (`transparent`, `opaque`, `auto`) and
  `moderation` (`low`, `auto`) are forwarded; `background` is applied
  (verified live, transparent images carry an alpha channel).
- `quality` (`auto`, `low`, `medium`, `high`) and `size` (`auto`, or
  `WIDTHxHEIGHT` with both sides multiples of 16, an aspect ratio between
  1:3 and 3:1, a longest edge up to 3840 and 655,360 to 8,294,400 pixels)
  are checked and forwarded, but the service decides on its own: observed
  live, flat-shape prompts came back `1254x1254` at `low` whatever was
  asked (portrait, square, landscape, low, medium or high), and
  photographic prompts `1536x1024` at `medium`. The response's `size` and
  `quality` report what was rendered, and whenever they differ from the
  request the parameter is listed in `x-airelays-ignored-parameters`.
  Values outside OpenAI's sets are refused with `422` naming the field in
  `param` (verified live: an unknown `quality` renders at `low`, any
  `model` is accepted, an impossible `size` is replaced).
- `n` (1 to 10) runs one upstream call per image, up to three at a time, and
  merges the images and `usage` into one response; one failed image fails
  the request, as on OpenAI.
- `stream: true` answers `text/event-stream` with one
  `image_generation.completed` (edits: `image_edit.completed`) event per
  image in OpenAI's event shape, once the images exist. There are no
  partial images, so `partial_images` is listed as ignored. Failures surface
  as real HTTP statuses, never as an empty stream.
- the answer is OpenAI's `ImagesResponse`: `created`, `data[].b64_json`,
  `background`, `output_format` (`png`), `quality`, `size`, `usage`
  (`input_tokens`, `output_tokens`, `total_tokens` with image/text details).
  `size` is the rendered resolution, which may differ from the request. Each
  image also carries the upstream's `generation_id`.
- `output_format`, `output_compression`, `partial_images`,
  `response_format: "url"`, `style`, `user` and `safety_identifier` are
  accepted, checked, dropped and listed in `x-airelays-ignored-parameters`:
  the upstream returns PNG `b64_json` whatever they say. Unknown parameters
  are dropped and listed the same way; `null` values are not listed.
- edits take OpenAI's multipart form (`image` or `image[]` files, PNG, JPEG
  or WebP, up to 16 and 20 MiB each, plus text fields) or a JSON body whose
  `image` is a data URL, a local `file_id` from `/v1/files`, or an array of
  them. `input_fidelity` is listed as ignored (the model always uses high
  fidelity). `mask` is refused with `422`: the upstream edits whole images
  and dropping the mask would change the output.
- an image call waits up to `[providers.openai] image_timeout_seconds`
  (default 300). Quota rejections (`429 usage_limit_reached`) carry
  `Retry-After`; with several accounts the request fails over to one whose
  image quota is open (the image bucket is benched separately from the text
  windows), and automatic retry applies as on the text routes.
- the traffic log keeps request and response shapes with image sizes, never
  the image bytes; `upstream_usage` records carry the image quota reading.

```bash
curl http://127.0.0.1:8080/v1/images/generations \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  -H 'content-type: application/json' \
  -d '{"prompt": "a watercolor fox", "size": "1024x1024", "quality": "low"}' \
  | python3 -c 'import base64,json,sys; open("fox.png","wb").write(base64.b64decode(json.load(sys.stdin)["data"][0]["b64_json"]))'

curl http://127.0.0.1:8080/v1/images/edits \
  -H 'authorization: Bearer YOUR_AIRELAYS_TOKEN' \
  -F image=@fox.png -F prompt='make the fox blue'
```

With the OpenAI SDK, `client.images.generate(model="gpt-image-2", prompt=...)`
and `client.images.edit(model="gpt-image-2", image=open("fox.png", "rb"), prompt=...)`
work as documented; `client.images.generate(..., stream=True)` yields
`image_generation.completed` events.

### Image examples

Both images were generated through AIRelays with the requests shown. The
service chose the rendered size and quality in each case, so each response
listed `quality,size` in `x-airelays-ignored-parameters` and reported the
rendered values in `size` and `quality`.

![A compact matte-black device with a thin green light line and the word AIRelays engraved on top, photographed on a slate pedestal](assets/images/airelays-product-shot.jpg)

```json
{
  "model": "gpt-image-2",
  "prompt": "Studio product photograph of a compact matte-black aluminum device with softly rounded edges resting on a slate pedestal, one thin illuminated green line across its front, the word AIRelays engraved in small clean sans-serif lettering on its top, seamless dark grey backdrop, soft key light and rim light, photorealistic.",
  "size": "1024x1024",
  "quality": "high"
}
```

Rendered at `1536x1024`, reported quality `low`, 343 image tokens, 16 seconds.

![Dozens of grey network cables in a server rack converging into one green cable that leads to a small black box](assets/images/airelays-one-relay.jpg)

```json
{
  "model": "gpt-image-2",
  "prompt": "Photorealistic photograph inside a dim server room: dozens of grey network cables converging into one single bright green cable that leads to a small sleek black box mounted on a rack, cinematic lighting with cool blue ambient and one warm highlight, shot on a full-frame camera, no text.",
  "size": "1024x1536",
  "quality": "high"
}
```

Rendered at `1536x1024`, reported quality `medium`, 601 image tokens, 20 seconds.

## `POST /v1/completions`

OpenAI runtime:

- current AIRelays OpenAI compatibility path

Claude runtime:

- discovered Claude aliases and concrete model ids, plus configured overrides
- text prompt in, text out (images, documents and tools are on
  `/v1/chat/completions` and `/v1/responses`)
- `stream=true|false`
- `reasoning_effort` supported (same modes and mapping as chat completions)
- `response_format` rejected loudly (not part of the completions API;
  ignoring it would silently hand unenforced text to a client that asked
  for JSON — use `/v1/chat/completions` for structured outputs)
- sampling parameters stripped and disclosed via
  `x-airelays-ignored-parameters`; other unsupported generation controls
  rejected locally

## Errors

Every error uses OpenAI's error object, with a `detail` copy of the message
for existing AIRelays clients:

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": null, "code": "model_not_found"}, "detail": "..."}
```

- `type` is `invalid_request_error` for client errors, `rate_limit_error`
  for 429 and `server_error` for 5xx (authentication problems with the
  upstream subscription use `authentication_error`)
- unknown models return 404 with code `model_not_found`; unsupported
  parameters and features return 422 (see ADR 0001)
- a Claude answer in `json_object` mode that is not one JSON object returns
  502 `invalid_json_output`; retrying usually helps, or use `json_schema`
- a quota or cooldown rejection returns 429 with a `Retry-After` header when
  the reset time is known (OpenAI SDKs then wait, or stop retrying when the
  reset is more than two minutes away); a Claude CLI that is not signed in
  returns 503, not 401, because the client's own credentials are fine
- every response carries an `x-request-id` header, the id AIRelays uses in
  its traffic log; OpenAI SDKs expose it as `request_id`
- Claude streams start only once the CLI produces output, so failures before
  the first token are real HTTP errors; failures after it arrive in-band as
  `data: {"error": {...}}` without `[DONE]`

## Finish reasons and refusals

`finish_reason` is `stop`, `length`, `tool_calls`, `content_filter`, or
`function_call` for requests using the deprecated `functions`. An
upstream response cut short by its token limit reports `length`, and one
stopped by the provider's content filter (OpenAI) or a refusal (Claude)
reports `content_filter`. OpenAI refusals are returned in `message.refusal`
(streamed as `delta.refusal`). Chat responses always include
`choices[].logprobs` and `message.refusal`, `null` when not applicable.

## Token usage on chat and completions

`/v1/chat/completions` and `/v1/completions` report `usage` in OpenAI's
Chat Completions shape on both runtimes:

```json
{
  "prompt_tokens": 18,
  "completion_tokens": 5,
  "total_tokens": 23,
  "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 2},
  "completion_tokens_details": {"reasoning_tokens": 1}
}
```

- `prompt_tokens` counts every prompt token, cached ones included;
  `prompt_tokens_details.cached_tokens` and `cache_write_tokens` break out
  the tokens read from and written to the prompt cache
  (`cache_write_tokens` is typed in recent openai-python releases; older
  SDKs keep it as an extra field).
  `total_tokens` is `prompt_tokens + completion_tokens`.
- the details objects and their members appear only when the provider
  reports them (the Claude runtime has no `completion_tokens_details`)
- when a provider reports no readable usage, `usage` is omitted rather than
  filled with zeros
- streaming: with `stream_options: {"include_usage": true}`, every chunk
  carries `"usage": null`, and one extra chunk with `"choices": []` and the
  usage follows the chunk with `finish_reason`, before `data: [DONE]`.
  Without it, chunks have no `usage` field. A stream that fails sends no
  usage chunk.
- `/v1/responses` returns the Responses API usage (`input_tokens`,
  `output_tokens`, ...) unchanged
- `stream_options` sent without `stream: true` has no effect; it is dropped
  and listed in `x-airelays-ignored-parameters`
- for the OpenAI runtime, traffic logs keep the upstream usage as reported
  (`upstream_usage` and `outbound_usage` records). For Claude, the raw CLI
  result is logged for non-streaming requests (`provider_response`) and
  stream lines only when stream-line logging is enabled; Claude
  `outbound_usage` records carry the translated usage

## `POST /v1/files`

Local AIRelays file storage for the OpenAI runtime compatibility path.

- `purpose` is a multipart form field, as OpenAI SDKs send it: one of
  `assistants` (the default when omitted), `batch`, `fine-tune`, `vision`,
  `user_data` or `evals`
- `GET /v1/files` supports OpenAI's `after`, `limit` (1 to 10000, default
  10000), `order` (`desc` by default) and `purpose` query parameters and
  returns `first_id`, `last_id` and `has_more`, so SDK auto-pagination
  works, including loops that delete each file as they go (a deleted file
  still anchors `after` for a day)
- `expires_after` is not supported and returns 422; delete files
  explicitly
- `GET /v1/files/{file_id}`, `GET /v1/files/{file_id}/content` and
  `DELETE /v1/files/{file_id}` complete the set

## `POST /v1/conversations`

Local AIRelays conversation storage for the OpenAI runtime compatibility path.

- create a conversation, optionally with initial `items` (Responses input
  items) and `metadata`; `GET`, `POST` (metadata update) and `DELETE`
  `/v1/conversations/{id}` manage it
- pass its id as `conversation` on `/v1/responses` (a string or
  `{"id": ...}`), `/v1/chat/completions` or `/v1/completions`. The
  conversation's items are prepended to each turn's input, and once a turn
  succeeds its input and output items are appended, as in OpenAI's
  conversations. A failed turn leaves the conversation unchanged
- stored output items are the replayable ones: assistant messages, every
  call the client answers (items with a `call_id`, such as function,
  custom and local shell calls), and reasoning items that carry
  `encrypted_content` (request it with
  `include: ["reasoning.encrypted_content"]`). Other reasoning items and
  server-side tool calls are not kept; their outcome is in the messages
  that follow
- local files are stored as `file_id` references and inlined on every
  turn. Initial `items` are checked like a turn's input (the file exists
  and fits its content part). Deleting a file that a conversation uses
  makes its later turns fail with 422; start a new conversation
- the conversation items API (`/v1/conversations/{id}/items`) is not
  available and returns 501
- on `/v1/chat/completions`, a `tool` message may answer a tool call made
  in an earlier turn of the conversation

Conversations work on Claude models too (chat completions and
`/v1/responses`), and one conversation can continue across models and
runtimes. Claude cannot replay other models' reasoning items, so they are
skipped on Claude turns, and a conversation holding GPT `custom` tool calls
cannot continue on a Claude model (422).

## Tool calls on Claude models

The `claude` CLI is an agent that would normally run tools itself. AIRelays
uses it so that Claude only decides which tool to call, and the client runs
it, exactly as with OpenAI:

| Safeguard | What it does |
|---|---|
| Built-in tools off | The CLI starts with no shell, file, web or agent tools. |
| Descriptions only | The client's tools reach the CLI through a helper inside AIRelays that lists them and answers every call with "not executed"; it can run nothing. |
| Startup check | Every request checks the CLI's own list of available tools; anything AIRelays did not define stops the request. |
| Pre-execution deny | The CLI asks a check before running any tool, and the answer is always "deny". |
| Stop at the call | The run is interrupted as soon as Claude has made its calls. |

The first two make running a tool impossible; the other three add defence
in depth. The CLI itself still starts its usual helper programs on every
Claude request (a read-only `git` check of its empty working folder, `ps`,
`ioreg` and, on macOS, the keychain reader for its sign-in).

## Unsupported Routes

These currently return `501 unsupported_error`. Every 501 AIRelays returns
carries `x-should-retry: false`, so OpenAI SDKs do not retry it:

- embeddings
- image variations (`/v1/images/variations`); generations and edits are
  served, see above
- audio
- realtime sessions

Claude models are also rejected on any route that is not part of their published subset.
