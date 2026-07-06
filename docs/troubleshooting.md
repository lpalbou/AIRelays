# Troubleshooting

## `401 Missing or invalid AIRelays bearer token`

- run `airelays status`
- run `airelays doctor --skip-response`
- confirm the relay token is present
- confirm the client is calling `http://HOST:PORT/v1/...`
- use `airelays token show` if needed

## `503 No ChatGPT login found`

- run `airelays status`
- run `airelays doctor --skip-response`
- if the OpenAI runtime is enabled, run `airelays login`
- on a server or over SSH, run `airelays login --device`
- if the browser flow cannot bind `localhost:1455`, use `airelays login --device`

## I opened the login URL on my laptop and got a connection error at `localhost:1455`

The browser flow's sign-in redirect goes to `localhost:1455` **on the machine
running the browser** — pasting the URL into a browser on another computer
sends the redirect to the wrong machine, so the login on the server never
completes and eventually times out.

Two fixes:

- **Device-code login (recommended):** `airelays login --device` prints a
  short code you approve from a browser on any device. This is the default
  on SSH sessions and displayless Linux.
- **SSH tunnel (if you specifically need the full browser flow, e.g. for a
  browser-profile picker):** run `ssh -L 1455:localhost:1455 user@server`
  first, then open the printed URL in your local browser. The tunnel can be
  opened even after `airelays login` has started waiting.

## Claude runtime is "not ready" under systemd/docker even though `claude setup-token` worked

A shell `export CLAUDE_CODE_OAUTH_TOKEN=...` never reaches a service
manager's environment, and it evaporates on reboot. Store the token instead:

```bash
airelays claude set-token   # paste the token from `claude setup-token`
```

It is written 0600 to `~/.airelays/claude-token` and injected into every
`claude` invocation automatically. `airelays status` shows the token source
(`file`, `env`, or `none`) under the Claude provider.

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

## Live upstream verification

Use `airelays doctor` when local state looks correct but client requests still
fail. It checks local setup, then verifies the OpenAI upstream `/models` route
and runs a tiny `/responses` smoke request when the OpenAI runtime is enabled
and logged in.

```bash
airelays doctor
```

Use `airelays doctor --skip-response` when you want setup and model-list checks
without sending a generation request.
