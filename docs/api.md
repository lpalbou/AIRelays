# API Notes

## Supported Routes

### `GET /v1/models`

Returns an OpenAI-style models list built from the subscription backend catalog.

### `GET /v1/subscription/status`

Returns the current ChatGPT subscription snapshot from the verified upstream usage endpoint.

Supported behavior:

- fetches usage from `chatgpt.com/backend-api/wham/usage`
- normalized account summary
- normalized primary and secondary windows
- normalized additional named rate limits when the upstream exposes them
- normalized credits and spend-control information
- `?raw=true` to include the raw upstream payload alongside the normalized summary

### `GET /v1/account/rate_limits`

Alias of `/v1/subscription/status`.

### `GET /v1/relay/status`

Returns protected AIRelays diagnostics for operators.

Supported behavior:

- readiness flags for upstream auth and relay-token presence
- resolved relay config summary
- protected auth summary
- limiter diagnostics for the current client IP
- local storage counters for files and conversations

### `POST /v1/completions`

Supported behavior:

- legacy prompt-in, text-out shape
- `stream=true|false`
- `conversation`
- common generation controls such as `max_tokens`, `stop`, `metadata`, and `user`

Currently rejected:

- `n` other than `1`
- `best_of`
- `echo`
- `logprobs`
- `suffix`
- upstream `store=true`

### `POST /v1/responses`

Supported behavior:

- `input` as string, object, or array
- `stream=true|false`
- `conversation` for local stateful sessions
- `tools` when using the normal route
- local file expansion through `file_id` content items

Notes:

- upstream storage is forced to `false`
- missing instructions are adapted to the verified minimal placeholder `"."`
- non-stream requests are reconstructed from streamed upstream output
- unsupported upstream sampling parameters are omitted before the upstream call; when that happens AIRelays adds `x-airelays-ignored-parameters`

### `POST /no-tools/v1/responses`

Same as `/v1/responses`, but rejects tool-bearing requests.

### `POST /v1/chat/completions`

Supported behavior:

- system and developer messages are folded into upstream `instructions`
- user and assistant messages are mapped into upstream input items
- assistant function tool calls and tool outputs are mapped into upstream function call items
- `stream=true|false`
- `conversation`
- common generation parameters such as `max_completion_tokens`, `metadata`, `service_tier`, and `user`

Currently rejected:

- `n` other than `1`
- `audio`
- `modalities`
- `prediction`
- `response_format.type=json_object`
- upstream `store=true`

### `POST /no-tools/v1/chat/completions`

Same as `/v1/chat/completions`, but rejects tool-bearing requests.

### `POST /v1/files`

Stores a file locally and returns an OpenAI-style file record.

Upload limits:

- `32` MiB per file by default
- `256` MiB total stored file bytes by default
- `413` when either ceiling would be exceeded

### `GET /v1/files`

Lists locally stored file metadata.

### `GET /v1/files/{file_id}`

Returns file metadata.

### `GET /v1/files/{file_id}/content`

Returns the raw stored file content.

### `DELETE /v1/files/{file_id}`

Deletes the locally stored file.

### `POST /v1/conversations`

Creates a local conversation container with optional metadata and seed items.

### `GET /v1/conversations/{conversation_id}`

Returns the stored local conversation state.

### `POST /v1/conversations/{conversation_id}`

Merges new metadata into a stored local conversation.

### `DELETE /v1/conversations/{conversation_id}`

Deletes a stored local conversation.

## Explicitly Unsupported Routes

These currently return `501 unsupported_error`:

- `POST /v1/embeddings`
- `POST /v1/images/{operation}`
- `POST /v1/audio/{operation}`
- `POST /v1/realtime/sessions`

## File Expansion Rules

Text-like MIME types:

- `text/*`
- `application/json`
- `application/xml`
- `application/yaml`
- `application/x-yaml`
- `application/csv`

Rules:

- text-like files larger than 1 MB are rejected
- images are passed as `input_image`
- unsupported binary files are rejected

## Relay Auth

AIRelays protects `/v1/*` and `/no-tools/v1/*`.

Public routes:

- `GET /`
- `GET /healthz`

Clients should send:

```http
Authorization: Bearer YOUR_AIRELAYS_TOKEN
```

OpenAI-compatible SDKs will do this automatically when you set the AIRelays token as the client credential for the AIRelays base URL.
Requests that omit the token, or use the wrong token, return `401`. Repeating that mistake enough times from the same IP triggers a temporary `429` block.

If you start AIRelays with `airelays serve --no-auth` or `AIRELAYS_REQUIRE_BEARER_AUTH=false`, these route families become openly accessible for that server process. In that mode, the relay does not require `Authorization`, though some client SDKs may still need a non-empty placeholder `api_key` value on their side.
