# Troubleshooting

## `401 Missing or invalid AIRelays bearer token`

The client is not sending the relay token AIRelays expects.

Checks:

- run `airelays status`
- confirm the Relay section shows `Relay token: present`
- confirm the client credential matches the current AIRelays token
- if needed, run `airelays token show` to reveal the current token, or `airelays token rotate` to issue a new one
- confirm the client is calling `http://HOST:PORT/v1/...`, not just `http://HOST:PORT/...`
- use `airelays status --json` if you want field-based checks in automation
- if you intended an open local relay, restart with `airelays serve --no-auth` or `AIRELAYS_REQUIRE_BEARER_AUTH=false`

## `503 No ChatGPT login found`

AIRelays could not find reusable upstream auth.

Checks:

- run `airelays status`
- confirm the Upstream Session section shows `Ready: yes`
- if not, run `airelays login`
- if you upgraded from earlier AIRelay local state, AIRelays can reuse singular-path config or data directories and older `AIRelay Auth` keychain entries automatically
- if `airelays login` cannot bind `localhost:1455`, retry later or use `airelays login --device`

## `429 Too many invalid authentication attempts from this IP`

AIRelays temporarily blocked the client after repeated bad tokens.

Checks:

- wait for the `Retry-After` window to pass
- update the client to the correct token
- consider rotating the relay token if the wrong value leaked into automation
- remember that repeated `401` responses from the same IP cause the temporary `429` block

## `429 Request rate limit exceeded for this IP`

The local per-IP request budget was exceeded.

Options:

- reduce client concurrency
- spread traffic over time
- raise the configured limits in `config.toml` if this is expected local usage

## `Token refresh failed`

The stored upstream ChatGPT login could not be refreshed.

Checks:

- run `airelays login` again
- confirm `airelays status` reports an authenticated upstream login

## `501 unsupported_error`

The route is not yet verified against the subscription backend.

Current unverified categories include:

- embeddings
- image generation
- audio
- realtime sessions

## `422` on uploaded files

AIRelays only inlines supported text-like files up to 1 MB and supported images.

Checks:

- confirm the file type is text-like or an image
- confirm the file is not too large for text inlining
- use `/v1/files/{file_id}` and `/v1/files/{file_id}/content` to inspect what was stored locally

## `413` on uploaded files

AIRelays rejected the upload before or during local storage.

Checks:

- confirm the file is below the configured per-file upload ceiling
- confirm the relay has not reached its configured total stored-upload quota
- review `docs/configuration.md` if you need to raise `max_upload_bytes` or `max_total_upload_bytes`
