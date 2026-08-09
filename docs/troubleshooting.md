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

## Claude requests fail even though `claude auth login` succeeded

A stored token (from `airelays claude set-token`) overrides the `claude`
CLI's own sign-in for relay requests. If that stored token is stale, relay
requests keep failing no matter how often you sign in through the CLI.

Checks and fix:

- `airelays status` shows the Claude token source; `file` means a stored
  token is in effect
- remove it with `airelays claude logout` (also signs the CLI out) or, in
  the desktop app, open the Claude token dialog and use "Remove stored
  token" (keeps the CLI sign-in)
- verify with a `claude:*` test request or `airelays doctor`

## Desktop app shows "Running — not responding"

The relay process is alive but did not answer the app's health probe —
usually heavy system load or a long request burst.

- it recovers on its own once the relay answers again; the label flips back
  to "Running"
- if it persists, open the Console tab for relay output, or use Restart
- Stop/Restart keep working: the app still manages the process

## `422` on Claude routes

The current Claude runtime supports only explicit `claude:*` models on text `chat.completions` and text `completions`.

Checks:

- confirm the model id is one of the configured `claude:*` ids
- remove tools, files, images, audio, and `conversation`
  (`response_format` json_schema/json_object is supported on chat completions)
- remove unsupported generation controls

## Cursor custom OpenAI endpoint errors

If Cursor is configured to call AIRelays as a custom OpenAI-compatible
endpoint and Agent/Edit flows fail, first identify which malformed request
family you are seeing.

AIRelays 0.12.2 and later accepts the two Cursor request shapes reported
publicly by Cursor users and staff on February 26, 2026 and July 9, 2026:

- full Responses-style bodies sent to `/v1/chat/completions`
- flat Responses-style `custom` tools / tool choices / tool calls on the
  chat route (for example `ApplyPatch`)

AIRelays 0.12.3 and later also strips unsupported top-level caller
identity fields (`user`, `safety_identifier`) before the upstream request
and rejects OpenAI model ids that are absent from a working live
`/v1/models` catalog, instead of letting those failures happen upstream.

Checks:

- if you see `Only function tools are currently supported on chat routes.`,
  upgrade AIRelays
- if you see `Unsupported parameter: user`, upgrade AIRelays; current
  versions strip `user` (and `safety_identifier`) locally because the
  ChatGPT/Codex backend does not accept caller-supplied end-user ids
- if you see `The '...model...' model is not supported when using Codex
  with a ChatGPT account.`, compare the chosen model against
  `GET /v1/models`; with a working upstream catalog AIRelays now rejects
  unsupported OpenAI ids locally and only `[providers.openai].extra_models`
  should bypass that check. On multi-account relays, if you just added or
  removed an account, current versions refresh that admission cache
  automatically before deciding
- inspect the traffic log under `~/.airelays/logs/...` for a
  `compatibility_adaptation` record showing the chat route accepted a
  Responses-shaped body
- if the request mixes both `messages` and `input`, AIRelays rejects it
  loudly; that payload is ambiguous and must be fixed at the client
- if a `role:"tool"` message does not reference a preceding assistant
  tool call in the same request, AIRelays rejects it as malformed instead
  of guessing the wrong tool type
- if Cursor still fails after the normalization above, capture the inbound
  request body from the traffic log and compare it with the documented
  supported shapes in [docs/api.md](api.md)

## Claude startup refusal

When the Claude runtime is enabled:

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

## `502` / `429` with `server_is_overloaded` or `usage_limit_reached`, or slow answers while the upstream is degraded

- the upstream itself is failing or out of quota; the error body carries the
  upstream's own code and message
- before surfacing the error, the relay retried automatically (default 3
  retries waiting 5s/20s/60s, each re-running account failover), which is why
  a failing request can take a minute or more before answering — check the
  traffic log for `retry_backoff` / `retry_skipped` / `upstream_stream_error`
  records to see what happened
- tune or disable with `retry_attempts` / `retry_backoff_seconds`
  (`[providers.openai]`, or desktop Settings → Providers)
- a `429` that names a reset far in the future is not retried (waiting a
  minute cannot help a window that resets in hours); with several accounts
  enrolled, the message reports the earliest account recovery
- a `400` `invalid_request_error` (for example an input that exceeds the
  model's context window) is answered immediately with the upstream's own
  error: it is deterministic, so it is never retried, never rotated to
  another account, and never benches an account — fix the request instead
- "All N OpenAI accounts are at their limits" is only claimed when every
  account is benched by real limit evidence (a quota rejection or the usage
  report); rounds of transient upstream failures answer
  "All N OpenAI accounts failed for this request" instead

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
