# Completed: Chat Completions usage contract and OpenAI conformance

## Metadata

- Created: 2026-10-11
- Status: Completed
- Completed: 2026-10-11

## Context

A client reading OpenAI's standard `usage` names recorded every OpenAI-runtime
call through AIRelays as 0 prompt / 0 completion tokens. The chat and
completions routes translated the Responses API output but passed its usage
through verbatim (`input_tokens`, `output_tokens`, a nested `attribution`
block). The Claude runtime used the standard names but counted only the
uncached prompt tokens the Claude CLI reports, missing cache reads and
writes (2 reported for a 3,542-token prompt), and invented zeros when usage
was missing. Streamed usage chunks kept a non-empty `choices` list, and
`/v1/completions` streams ignored `include_usage`.

## What we did

- One `CompletionUsage` section in `airelay.transforms`: ints-only counts,
  two explicit source adapters (Responses usage, whose `input_tokens` already
  include cached tokens; Anthropic usage, whose `input_tokens` exclude cache
  reads and writes), details only when reported, usage omitted when
  unreadable, unknown upstream keys dropped (kept in traffic logs).
- Streaming follows `stream_options.include_usage`: `usage: null` on every
  chunk, one final `choices: []` chunk after the finish_reason chunk and
  before `[DONE]`, on chat and completions for both runtimes.
- Claude usage comes only from the CLI's `result` event; streamed Claude
  requests now log `outbound_usage`.
- `stream_options` without streaming is dropped and disclosed in
  `x-airelays-ignored-parameters`; truncated non-streaming responses report
  `finish_reason: "length"`. `/v1/responses` is unchanged.

- Conformance pass beyond usage (schema validation against openai-openapi
  plus an adversarial audit): OpenAI error objects everywhere (with a
  `detail` copy), 404 `model_not_found`, `Retry-After`, Claude sign-in
  failures as non-retryable 503, Claude streams primed before the 200,
  `content_filter` and refusals mapped, required `logprobs`/`refusal`
  fields, integer `created`, and `stop`/`logprobs`/`top_logprobs`/
  `logit_bias` rejected instead of silently ignored (`seed` disclosed).
  Unsupported features keep 422 per ADR 0001 (the spec defines no error
  status codes).
- Third conformance round (adversarial audit with the openai-python SDK):
  conversations keep their whole history (`ConversationTurn` in
  `airelay.conversations`), assistant `content: null` with tool calls,
  `files.list` pagination and form-field `purpose`, `GET /v1/models/{model}`,
  `file` content parts, `allowed_tools`, `verbosity`/`prompt_cache_key`, the
  deprecated `functions` round trip, unknown parameters disclosed through
  per-route parameter sets (both runtimes), strict-only schema
  normalization, `Retry-After` from upstream `resets_in_seconds`,
  non-retryable 501s, and `x-request-id`.
- Fourth and fifth rounds (re-audits of those fixes): header-safe
  disclosure (percent-encoded, capped), mixed chat shapes rejected again,
  file tombstones so list-and-delete loops keep their cursor, conversations
  storing `file_id` references and expanding them after the history is
  attached, replay of every client-answered call and encrypted reasoning,
  `strict: false` sent explicitly for schemas and chat function tools (live
  evidence: the upstream assumes strict mode otherwise), and model
  retrieval limited to listed models.

## Validation

- Adversarial reviews (official spec from openai-python types and
  Anthropic prompt-caching docs; design and call-site audit; three
  conformance audits with the openai-python SDK), iterated until no
  must-fix or should-fix findings remained.
- `tests/test_usage_contract.py` (48) and `tests/test_openai_compliance.py`
  (62); full suite 554 passed on Python 3.11, 3.12 and 3.13.
- Live validation of every chat/completions response, stream chunk and
  error against the official openai-openapi JSON schemas: 0 failures (the
  completions-stream null `finish_reason`/`usage` quirk aside).
- Live check with the official openai-python SDK 3.29.0 against the local
  relay: chat and completions, streamed and not, OpenAI and Claude runtimes,
  all parse with standard usage.
