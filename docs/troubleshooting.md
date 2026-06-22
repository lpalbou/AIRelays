# Troubleshooting

## `401 Missing or invalid AIRelays bearer token`

- run `airelays status`
- confirm the relay token is present
- confirm the client is calling `http://HOST:PORT/v1/...`
- use `airelays token show` if needed

## `503 No ChatGPT login found`

- run `airelays status`
- if the OpenAI runtime is enabled, run `airelays login`
- if the browser flow cannot bind `localhost:1455`, use `airelays login --device`

## `422` on Claude experimental routes

The current Claude runtime supports only explicit `claude:*` models on text `chat.completions` and text `completions`.

Checks:

- confirm the model id is one of the configured `claude:*` ids
- remove tools, files, images, audio, structured outputs, and `conversation`
- remove unsupported generation controls

## Claude startup refusal

When Claude experimental mode is enabled:

- keep the listener on `127.0.0.1`, `localhost`, or `::1`
- keep relay bearer auth enabled
- keep `trust_x_forwarded_for=false`

## `429 Too many invalid authentication attempts from this IP`

- wait for the `Retry-After` window
- update the client to the correct relay token
- rotate the token if needed

## `413` on uploads

- confirm the file is below the per-file upload ceiling
- confirm the relay has not reached the total stored-upload quota
