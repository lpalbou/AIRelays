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
| Image generation and edits (`/v1/images/generations`, `/v1/images/edits`) | ✅ `gpt-image-2` ([details](#image-generation)) | ❌ `422` |
| Embeddings (`/v1/embeddings`) | ❌ `501` | ❌ `501` |
| Image variations, audio, realtime | ❌ `501` | ❌ `501` |

Neither subscription offers embeddings: the ChatGPT subscription refuses
embedding models, and Anthropic has no embedding model. Neither offers
text-to-speech or transcription.

## Image generation

The ChatGPT subscription serves the GPT image model on two OpenAI Images
routes; `/v1/models` lists it as `gpt-image-2` with these controls under
`airelays.capabilities.image_generation`. Images come back as PNG
`b64_json`, as OpenAI's GPT image models return them. The image quota is a
separate 24-hour window (see
[Subscription Status](subscription-status.md#image-generation-quota)).

| Capability | GPT models | Claude models |
|---|---|---|
| `POST /v1/images/generations`: `prompt`, `model` (`gpt-image-*`), `background`, `moderation` | ✅ checked locally, forwarded | ❌ `422` |
| `quality` (`auto`, `low`, `medium`, `high`) and `size` (`auto` or `WIDTHxHEIGHT`) | ⚠️ checked and forwarded, but the service picks its own; a difference is listed in `x-airelays-ignored-parameters` and the response reports what was rendered | ❌ |
| `n` up to 10 | ✅ one upstream image per count | ❌ |
| `stream: true` | ✅ one `image_generation.completed` (edits: `image_edit.completed`) event per image, no partial images | ❌ |
| `POST /v1/images/edits`: `image` files (multipart, up to 16 PNG/JPEG/WebP), or data URLs and local `file_id`s (JSON) | ✅ | ❌ `422` |
| `output_format`, `output_compression`, `partial_images`, `response_format: "url"`, `style`, `user`, `safety_identifier`, `input_fidelity` | ⚠️ the answer is always PNG `b64_json` | ❌ |
| `mask` | ❌ `422` | ❌ |
| Image model ids on the text routes | ❌ `404` | ❌ |

The upstream reports the resolution and quality it rendered in `size` and
`quality`. Observed live: four flat-shape requests asking for portrait,
square and landscape sizes at low, medium and high quality all came back
`1254x1254` at `low`, while photographic prompts came back `1536x1024` at
`medium`. Details for both routes are in
[API Notes](api.md#post-v1imagesgenerations-and-post-v1imagesedits).


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
