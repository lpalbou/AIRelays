# Capabilities by Account Type

AIRelays serves GPT models (your ChatGPT subscription) and Claude models (your
Claude subscription) through one OpenAI-compatible endpoint,
`http://127.0.0.1:8317/v1`, with the same API key. The model name picks the
account type: `gpt-…` models use the ChatGPT runtime, `claude:…` and
`claude-…` models use the Claude runtime. `/v1/models` lists both, with each
model's capabilities under `airelays.capabilities`.

This page is the reference for what works on each account type. Details for
each route are in [API Notes](api.md).

Legend: ✅ supported · ⚠️ accepted but not applied, and listed in the
`x-airelays-ignored-parameters` response header · ❌ refused with a clear
error (`422`, or `501` for whole endpoints).

## Routes and responses

| Capability | GPT models | Claude models |
|---|---|---|
| `POST /v1/chat/completions` | ✅ | ✅ |
| `POST /v1/completions` | ✅ | ✅ |
| `POST /v1/responses` | ✅ | ✅ |
| Streaming (`stream: true`) on all three routes | ✅ | ✅ |
| Standard `usage` (`prompt_tokens`, `completion_tokens`, cached and reasoning details) | ✅ | ✅ |
| `stream_options.include_usage` | ✅ | ✅ |
| `GET /v1/models`, `GET /v1/models/{model}` | ✅ | ✅ |

## Generation controls

| Capability | GPT models | Claude models |
|---|---|---|
| Output-token limit (`max_completion_tokens`, `max_tokens`, `max_output_tokens`) | ✅ enforced upstream; at least 16 | ✅ the run stops at the limit |
| `reasoning_effort` | ✅ `none`, `low`, `medium`, `high`, `xhigh` (per model catalog) | ✅ `low`, `medium`, `high`, `xhigh`, `max` (per model) |
| `temperature`, `top_p`, `presence_penalty`, `frequency_penalty` | ⚠️ | ⚠️ |
| `seed`, `user`, `safety_identifier` | ⚠️ | ⚠️ |
| `stop`, `logprobs`, `top_logprobs`, `logit_bias` | ❌ | ❌ |
| `n` greater than 1 | ❌ | ❌ |

A response cut by the output-token limit reports `finish_reason: "length"`
(or `status: "incomplete"` on `/v1/responses`), as on OpenAI.

## Structured output

| Capability | GPT models | Claude models |
|---|---|---|
| JSON mode (`response_format: {"type": "json_object"}`) | ✅ native; the messages must mention JSON | ✅ the answer is checked to be one JSON object |
| `json_schema` | ✅ strict-mode rewrite when `strict: true` | ✅ enforced natively by the CLI |

## Tool calls

| Capability | GPT models | Claude models |
|---|---|---|
| Function tools; calls returned as `tool_calls` for your application to run | ✅ | ✅ AIRelays never runs a tool ([how](api.md#tool-calls-on-claude-models)) |
| `tool_choice` `auto` and `none` | ✅ | ✅ |
| Forced call (`required` or a named tool) | ✅ | ❌ |
| `allowed_tools` | ✅ | ✅ in `auto` mode |
| `parallel_tool_calls: false` | ✅ | ❌ |
| `strict: true` function tools | ✅ | ⚠️ run non-strict |
| `custom` (grammar) tools | ✅ | ❌ |
| Deprecated `functions` / `function_call` | ✅ | ❌ |
| Server-side web search (`web_search` tool on `/v1/responses`) | ✅ | ❌ |
| `web_search_options` on chat completions | ❌ | ❌ |

## Inputs, files and conversations

| Capability | GPT models | Claude models |
|---|---|---|
| Images: PNG, JPEG, GIF, WebP as data URL, http(s) URL or local `file_id` | ✅ | ✅ |
| PDFs and UTF-8 text files (`file` parts: `file_data` or `file_id`) | ✅ | ✅ |
| `/v1/files` (upload, list with pagination, retrieve, delete) | ✅ | ✅ |
| `/v1/conversations`; `conversation` on `/v1/chat/completions` and `/v1/responses` | ✅ | ✅ |
| `conversation` on `/v1/completions` | ✅ | ❌ |
| One conversation continued across GPT and Claude | ✅ | ✅ (GPT `custom` tool calls in the history are not supported on Claude) |
| `previous_response_id` | ❌ use `conversation` | ❌ use `conversation` |
| Audio input, `prediction`, `moderation` | ❌ | ❌ |

An image or document a model cannot read returns `422` on both account types,
before any upstream call.

## Endpoints outside text generation

| Capability | GPT models | Claude models |
|---|---|---|
| Embeddings (`/v1/embeddings`) | ❌ `501` | ❌ `501` |
| Image generation, audio, realtime | ❌ `501` | ❌ `501` |

Neither subscription offers embeddings: the ChatGPT subscription refuses
embedding models, and Anthropic has no embedding model.

## Accounts

| Capability | GPT models | Claude models |
|---|---|---|
| Several accounts, balancing and failover | ✅ | ✅ |
| Usage and quota reporting (`/v1/subscription/status`) | ✅ | ✅ for browser sign-ins; not for the setup-token account |

## Streaming notes

Both account types stream answers as they are generated. Three parts arrive
whole instead:

- each tool call's arguments arrive in one chunk;
- on Claude models, a JSON-mode answer arrives in one chunk, because it is
  checked before it is sent;
- on Claude models, the first token takes a few seconds while the local CLI
  starts.
