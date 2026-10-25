# Proposed: disclose content-part fields the relay drops (e.g. `cache_control`), and document prompt caching per runtime

## Metadata

- Created: 2026-10-21
- Status: Proposed
- Reported by: RAGnarok (a client of the relay), from an ingest profiling session

## Summary

`docs/api.md` promises that a parameter the relay does not apply is "dropped and listed in
`x-airelays-ignored-parameters` … so nothing is ignored silently". That holds for top-level
request parameters, but fields inside message content parts are dropped without any disclosure.
The case that matters to clients is Anthropic-style `cache_control` on a text part: on Claude
models the relay keeps the part's `text` and silently discards the marker. Disclose dropped
content-part fields the same way, and document what prompt caching does on each runtime.

## Reason

A client that sends `cache_control` (RAGnarok does, for long system prompts on Claude models,
`src/utils/llm_client.py`) cannot tell whether caching was requested upstream, ignored, or
impossible. Silent drops contradict the relay's stated contract and make cache behaviour
impossible to diagnose from the client side.

## What the specs say (for reference)

- **OpenAI** (Chat Completions / Responses): prompt caching is automatic for prompts of at least
  1,024 tokens with an identical prefix; `prompt_cache_key` is an optional routing hint; cached
  input is reported in `usage.prompt_tokens_details.cached_tokens` (Responses:
  `usage.input_tokens_details.cached_tokens`). Reference: the OpenAI prompt caching guide
  (platform.openai.com/docs/guides/prompt-caching). The relay already forwards `prompt_cache_key`
  on GPT models and passes `cached_tokens` through faithfully.
- **Anthropic** (Messages API): caching is opt-in per content block with
  `"cache_control": {"type": "ephemeral"}`; reads and writes are reported as
  `usage.cache_read_input_tokens` and `usage.cache_creation_input_tokens`. Reference: the
  Anthropic prompt caching guide (docs.anthropic.com, "Prompt caching"). The relay maps
  `cache_read_input_tokens` to `cached_tokens` in its usage output.

## Observed (2026-10-21)

- **GPT path (subscription backend)**: compliant. Forwarded request bodies were byte-identical,
  `prompt_cache_key` reached `chatgpt.com/backend-api/codex/responses`, and usage was passed
  through unchanged. Across ~200 hourly traffic logs, calls under 2,048 input tokens never got a
  cache hit (0 of 4,927), while 4-8k-token calls that sent a key hit 592 of 782; the smallest
  cached amount seen was 2,432 tokens. So the subscription backend appears to cache only above
  roughly 2k tokens, not the 1,024 documented for the metered API. This is backend behaviour, not
  a relay defect, but it is worth stating in the docs.
- **Claude path**: `claude_content._parts` (src/airelay/claude_content.py) keeps `text` from
  text parts and ignores every other key, so `cache_control` disappears with no entry in
  `x-airelays-ignored-parameters`. (The OpenAI-path helper `transforms._content_to_text` flattens
  parts the same way.) The Claude runtime runs through the Claude CLI, so per-block markers may
  not be forwardable at all; the defect is the silent drop, not the absence of forwarding.

## Expected

1. Content-part fields the relay does not apply (starting with `cache_control`) are listed in
   `x-airelays-ignored-parameters` with their path (for example
   `messages[0].content[0].cache_control`), and in the traffic log, consistent with the top-level
   rule.
2. `docs/api.md` states, per runtime, how prompt caching works through the relay: GPT models get
   the backend's automatic caching (with the observed ~2k-token minimum on the subscription
   backend, marked as observed rather than documented upstream) and `prompt_cache_key`
   forwarding; Claude models get whatever caching the CLI applies itself, per-block
   `cache_control` is not forwarded, and `cached_tokens` reports what the CLI's usage reports.
3. When the upstream reports no cache information, `cached_tokens` stays `0` (as today), never
   omitted, so clients can rely on the field.

## Current code reality

- `src/airelay/claude_content.py` `_parts`: converts content parts for the Claude runtime; keeps
  text, refusal, image and file parts; other keys on a part are dropped silently.
- `src/airelay/transforms.py` `_content_to_text` (~543) and its callers (~951, ~969, ~993):
  same flattening on the GPT path.
- `src/airelay/transforms.py` ~1726-1762: usage pass-through (`cached_tokens`,
  `cache_write_tokens`, Anthropic `cache_read_input_tokens` → `cached_tokens`).
- `docs/api.md` ~70-83: the "nothing is ignored silently" rule and prompt-caching notes.

## Scope

- collect dropped content-part keys during conversion (both runtimes) and merge them into the
  existing ignored-parameters header and traffic log
- docs: per-runtime prompt-caching section
- no change to what is forwarded

## Validation

- unit tests: a chat request with `cache_control` on a system text part lists the path in the
  header on Claude and GPT models; a request without extra keys lists nothing new
- docs build passes
