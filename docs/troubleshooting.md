# Troubleshooting

## Desktop app install

The desktop builds are not notarized (macOS) or code-signed (Windows), so
the operating system may warn when you install them by hand. The one-line
installers in the [README](../README.md#install) avoid these prompts on
macOS and Linux.

- macOS says "AIRelays is damaged" or "cannot be verified" after you
  downloaded the DMG in a browser: clear the quarantine flag, then open the
  app again.

  ```bash
  xattr -dr com.apple.quarantine /Applications/AIRelays.app
  ```

  Alternatively, open **System Settings → Privacy & Security** and choose
  **Open Anyway** for AIRelays.
- The installer reports that no desktop installer exists for your system:
  desktop builds cover Apple Silicon Macs, x86_64 Linux, and x64 Windows.
  On other machines, including Intel Macs, use the headless installer
  (`scripts/install-headless.sh`); it runs the same relay without the tray.
- Windows SmartScreen blocks the setup `.exe`: choose **More info** →
  **Run anyway**, or use the PowerShell installer.
- Linux: the AppImage does not start from a terminal with a FUSE error:
  install `libfuse2` (`libfuse2t64` on Ubuntu 24.04), or run it with
  `APPIMAGE_EXTRACT_AND_RUN=1`. The menu entry created by the installer
  already does this when FUSE 2 is missing. GNOME needs the AppIndicator
  extension to show the tray icon.
- The headless installer warns that `~/.local/bin` is not on your `PATH`:
  add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile and open
  a new terminal.

## Traffic logs report a cleanup error

The tray Settings page or `GET /v1/relay/logging` can report `last_error`, or
`airelays logs` can print a cleanup error. AIRelays pauses traffic-log writes
until it can enforce the configured limit; relay requests continue normally.

Check that the configured log directory is writable and has space available:

```bash
airelays logs
airelays status --json
```

If the limit is smaller than the history you need, archive the relevant log
files first, then raise the total limit or shorten retention:

```bash
airelays logs --retention-days 30 --max-total-mb 2048 --max-file-mb 50
```

Use the same `--config`, `--data-dir`, or `--logs-dir` arguments as the
running relay. Apply the policy again, then confirm that `last_error` is empty
and `over_budget` is false. See [traffic-log retention](configuration.md#traffic-log-retention)
for the policy, ownership boundaries, and permanent-deletion behavior.

## A model is missing or a Claude alias is unclear

Use the Models tab's Refresh button or `GET /v1/models?refresh=true` to
reload both provider catalogs. OpenAI discovery uses `client_version =
"auto"` by default; an explicit older version pin can select an older
catalog. Update the installed Codex CLI to follow newer catalogs.

For Claude, update the configured `claude` CLI and inspect the alias's
`airelays.resolved_model` in `/v1/models`. Concrete ids are also listed.
If discovery fails, inspect `providers.claude.models_discovery_error` in
`GET /v1/relay/status`; configured ids and the last successful catalog
remain available. See [model discovery](api.md#get-v1models) and
[configuration](configuration.md#provider-notes).

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

## An OpenAI account is missing after upgrading to 0.16.1

AIRelays 0.16.1 and later store OpenAI sign-ins only as owner-only files
([ADR 0008](adr/0008-uniform-cross-platform-runtime.md)). Sign-ins that 0.16.0
or earlier kept in the OS keychain, Credential Manager, or Secret Service are
no longer read.

- run `airelays doctor --skip-response`; it lists those accounts under
  `openai_keyring_sign_ins`
- sign in again with `airelays login` (or **Sign in** in the desktop app),
  once per account
- you can remove the old `AIRelays Auth` entries from your OS keychain
  afterwards

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

## `Claude CLI not found`, or a `.cmd` shim is refused (Windows)

- run `airelays doctor --skip-response`; the Claude check shows which `claude`
  the relay found
- AIRelays looks for `claude` on PATH and in the usual install locations; a
  `claude` installed after the tray started is found after a relay restart
- on Windows, install Claude Code with its native installer (`claude.exe`).
  npm's `claude.cmd` shim is refused because cmd.exe would re-parse request
  text; to use an npm install, set `[providers.claude].command` to
  `["node", "<path to the package's cli.js>"]`; with the desktop app, set
  the `AIRELAYS_CLAUDE_COMMAND` environment variable to the same JSON array
  instead, because the app rewrites `config.toml`
- a `413` on a Claude request means its system prompt or JSON schema exceeds
  the OS command-line limit; shorten it

## `503 No Claude account is connected to AIRelays`

AIRelays uses only Claude accounts it owns. Your personal Claude Code sign-in
(`claude auth login` or `/login`), an inherited `CLAUDE_CONFIG_DIR`, and an
exported `CLAUDE_CODE_OAUTH_TOKEN` are ignored, so signing in to Claude Code
does not connect the relay. Until an account is connected, `claude:*`
requests return `503 provider_auth_error` and `/v1/models` lists no Claude
models.

- run `airelays claude login` (desktop: **Sign in** → "In a browser") once
  per subscription
- on a headless host or under systemd/docker, run `claude setup-token` on any
  machine with a browser, then `airelays claude set-token` (desktop: "With a
  token"); the token is stored 0600 in `~/.airelays/claude-token`
- `airelays doctor` reports a `CLAUDE_CODE_OAUTH_TOKEN` set in the relay's
  environment; AIRelays ignores it

After upgrading from 0.15.0 or earlier, installs that relied on the personal
sign-in or an exported token need this once. Claude Code refresh tokens
rotate, so AIRelays cannot copy the personal sign-in. Profiles already
enrolled keep working, and switching or signing out Claude Code does not
affect the relay.

## A Claude request fails: which account or model is affected

AIRelays decides what a Claude CLI failure affects before taking any account
out of rotation, and the error message says which case applies:

- **Your request** (a bad image, an unreachable URL, a prompt that is too
  long): 422 or 413 explaining what to change. No account is paused.
- **One model on one account**: the API does not serve that model to the
  account (a "not found" or permission refusal while a live usage check shows
  the account's sign-in works). Only that model is paused on that account
  (`unavailable_models` in `/v1/relay/status`); other models keep working, and
  another account is tried. If none can serve it, you get 404
  `model_unavailable`; use another model from `/v1/models`.
- **A passing sign-in refusal**: the CLI reported a sign-in error, but the
  live usage check shows the sign-in works (for example a token that was just
  renewed). Nothing is paused; another account is tried, and retrying usually
  works.
- **An account's usage limit or a temporary failure**: the account pauses
  until its reset time (or `account_cooldown_seconds` when the reset is
  unknown), or about 30 seconds after a temporary failure, and another account
  is tried. The message lists each account's state, with `Retry-After`; a wait
  over a minute also carries `x-should-retry: false`. When the check cannot
  tell whether the sign-in works, the account pauses the same way.
- **An account's sign-in**: the usage check confirms the sign-in was
  rejected (an explicit authentication error, such as a revoked token). The
  account is marked as needing a new sign-in, leaves rotation until you sign in again, and the message names
  the command to run (see below).

## `502 invalid_json_output` on a Claude model

You asked for `response_format: {"type": "json_object"}` and the model's
answer was not a single JSON object. AIRelays checks JSON-mode answers before
returning them, so you never receive broken JSON with a success status. Retry
the request, or send `json_schema` with a schema: the Claude CLI enforces
named schemas natively.

## A Claude account needs to sign in again

Use `airelays claude accounts` to find the account id, then
`airelays claude login --replace ACCOUNT`. Choose the same account in the
browser. A different identity is not eligible for requests through the enrolled
profile. To add another subscription, use `airelays claude login` without
`--replace`. The desktop offers **Sign in again** on unavailable profile rows.
For the setup-token account (`default`), run `claude setup-token` and
`airelays claude set-token` again.

Do not move or rename a profile's `config/` directory: the path identifies its
macOS Keychain entry. After a machine migration, sign in again on the new host.
On macOS, run the relay in your login session (the desktop app does), not over
SSH or as a LaunchDaemon, so Claude Code can use the keychain. An unavailable
usage meter alone does not require signing out.

## A Claude usage bar is stale or missing

Usage comes from each profile's Claude Code (`get_usage`). A stale bar shows
the last good reading with its age; the reason is `stale_reason` in
`GET /v1/subscription/status?provider=claude&all_accounts=true`:

- `usage_unavailable`: Claude returned no live data (for example, the upstream
  rate-limited its usage endpoint). Retries back off from 5 to 60 minutes;
  requests are unaffected
- `nonessential_traffic_disabled`: remove
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` from the relay's environment
- `cli_unsupported`: update Claude Code (verified with 2.1.292+)
- `cli_failed`: check that the configured `claude` binary runs; see
  `airelays doctor`

The setup-token account never has a bar (Claude reports no usage for setup
tokens); add a browser profile with `airelays claude login` for a meter.
Stale readings never unblock or rank an account.

## Desktop app shows "Sign-in expired" on an OpenAI account

OpenAI ended that account's stored session (for example after signing in to
Codex elsewhere), so the relay can no longer refresh its token. The account
is out of rotation until you sign in again.

- click **Sign in again** next to the badge and pick the same account in the
  browser — the stored sign-in is replaced in place, no sign-out needed
- the usage bars return within a few seconds of a successful sign-in
- the raw upstream error is available on hover over the note under the row
- from the CLI, `airelays login` does the same repair: signing in with an
  already-enrolled account refreshes that account's stored credentials

## Desktop app shows "Running — not responding"

The relay process is alive but did not answer the app's health probe —
usually heavy system load or a long request burst.

- it recovers on its own once the relay answers again; the label flips back
  to "Running"
- if it persists, open the Console tab for relay output, or use Restart
- Stop/Restart keep working: the app still manages the process

## `404 model_not_found` or `422` on chat and completions

A `404` with code `model_not_found` means the model id is not available:
confirm it appears in `/v1/models` (a discovered alias, concrete
`claude-*` id, configured override, or an OpenAI model your account's
catalog lists).

A `422` names what the relay cannot honor. The Claude runtime supports text
`chat.completions` and text `completions`. Checks:

- remove tools, files, images, audio, and `conversation` on Claude models
  (`response_format` json_schema/json_object is supported on chat completions)
- remove `stop`, `logprobs`, `top_logprobs`, `logit_bias`,
  `web_search_options` and `moderation`: neither upstream can honor them,
  so AIRelays rejects them rather than returning output that ignores them
- remove other unsupported generation controls

If a request succeeds but behaves as if a parameter were missing, check the
`x-airelays-ignored-parameters` response header: it lists every parameter
AIRelays dropped, including ones it does not recognize.

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
