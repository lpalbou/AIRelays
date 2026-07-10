# Changelog

## Unreleased

### Added

- Claude subscription usage: `GET /v1/subscription/status?provider=claude` returns the 5-hour and weekly windows (plus per-model weekly caps when reported) in the same normalized shape as OpenAI usage. The desktop Accounts card renders both providers identically — usage bars, reset times, and an "At limit" badge. Credentials resolve from the stored token file first, then the `claude` CLI's own credential store.
- Claude sign-in and sign-out from the desktop app: a split button offering "In a browser (this machine)" and "With a token (any device)" (paste the token from `claude setup-token`), a code-entry field in the sign-in banner for the browser flow's final step, and per-row sign-out with a confirmation that explains the machine-wide effect on the `claude` CLI. New CLI equivalent: `airelays claude logout` (removes the stored token, then runs `claude auth logout`).
- Models listing: a desktop Models tab and a CLI command (`airelays models`, `--json` supported) list every model id the running relay accepts, grouped by provider, with one-click copy of the exact id.
- Desktop supervision: crash auto-restart with capped backoff and native notifications (Settings → Launch, default on; a deliberate Stop never respawns), "Start AIRelays at login" via the OS-native mechanism on all three platforms, and automatic relay start when the app opens (skipped when a relay already answers on the address).
- Tray activity indicator: the tray icon blinks once when new requests were served since the last poll (`requests_total` in `/v1/relay/status`).
- Sign-in flows can be cancelled from the banner; a deliberate cancel is reported as information, not a failure.
- Account balancing proactively skips accounts whose usage report already shows a reached limit, in addition to reacting to live 429s; `POST /v1/relay/accounts/refresh` (CLI: `airelays accounts refresh`) clears the holds and re-checks capacity on demand.
- Multiple-account management from the desktop: per-account sign-out buttons and an "Add account" split button offering browser or device-code sign-in per attempt.

### Changed

- Relay health is now reported truthfully and robustly. The desktop derives liveness from `/healthz` (exempt from auth and rate limits) with brief down-flip debouncing, fetches the rich status payload separately, and shows "Running — not responding" instead of "Stopped" when the relay process is alive but not answering. The dashboard, sidebar, and tray always agree.
- The relay's status route no longer runs provider probes in the request path: Claude CLI probes refresh in the background with hard timeouts, credential reads are briefly cached, and account rediscovery is throttled. Status responses return immediately even when the `claude` binary is slow or hung.
- Per-line stream logging (one record per upstream SSE line, for both providers) is now opt-in via `[logging] stream_lines` / `AIRELAYS_LOG_STREAM_LINES` (default off). Summary records — request, tokens, status, errors — are always logged. This keeps traffic logs compact and the Traffic view complete on busy relays.
- The Overview tab was restructured: one Accounts card with OpenAI and Claude section headers, per-account usage bars labeled with the real window names ("5h window", "Weekly"), a single Refresh action, quieter tertiary buttons, and amber (not red) for self-resetting quota limits. Endpoint URLs appear once, and only real, reachable addresses are listed (self-assigned 169.254.x.x interfaces are filtered out).
- The Claude sign-in button shows an "Off in network mode" badge while the relay is exposed to the network (the runtime is loopback-only); clicking it offers a one-click switch to "This machine only". A signed-in Claude that network mode paused stays visible as a "Paused" row.
- Documented the compatibility layer in README ("What The Relay Changes") and docs/api.md: rejected sampling parameters are stripped and disclosed via the `x-airelays-ignored-parameters` header; `reasoning_effort` passes through verbatim, and omitting it means the upstream's low default — set it explicitly for parity with the official apps.
- Documented upstream terms and personal use: the disclaimer (and docs site) now explain that AIRelays drives provider-owned tooling under the account holder's own sign-ins for ordinary individual use, and link the official Anthropic and OpenAI terms and policy pages to review. A new FAQ entry summarizes the same point.

### Fixed

- Claude routes no longer fail with `422 Claude experimental mode does not support `temperature`` when standard OpenAI SDK clients send sampling parameters. `temperature`, `top_p`, `presence_penalty`, and `frequency_penalty` now get the same documented adaptation as on the OpenAI runtime: stripped (the local `claude` CLI has no sampling controls), disclosed in the `x-airelays-ignored-parameters` response header, and logged as a `compatibility_adaptation` traffic record.
- Desktop Claude sign-in: the app now extends its PATH with standard user bin directories at startup (GUI apps inherit a minimal PATH), so the `claude` CLI and PATH-installed relays are found; on Windows the `claude` .cmd shim is invoked correctly.
- Traffic view: real requests are no longer evicted from the view by monitoring or stream chatter; token counts (input/output) are shown per request; the reader's memory and CPU are bounded.
- Tray icon: the icon re-asserts itself on every poll, so it can no longer stick out of sync with the actual connection state.
- Sign-in flows: replacing or cancelling a sign-in cleans up completely (no orphaned flow holding the OAuth callback port), abandoned flows time out, and failure messages include the CLI's stderr.
- Supervision edge cases: concurrent starts cannot double-spawn the relay; a benign start collision no longer mislabels a healthy relay as Failed; auto-restart stands down when an external relay owns the port; Restart explains itself when the answering relay isn't app-managed.
- Multiple-account discovery derives identity from the auth record (email/plan now always populate), and a newly added account is picked up by the running relay without a restart.
- Model-aware routing for mixed-plan pools: requests route only to accounts that expose the requested model, and `/v1/models` advertises the intersection.
- Headless sign-in: `airelays login` auto-selects the device-code flow on SSH and displayless machines; device-flow errors are readable; `airelays claude set-token` stores a Claude token that survives service managers and reboots.

## 0.2.5

- Added `airelays doctor` for local setup checks, relay-token validation, OpenAI login readiness, live upstream `/models` probing, an optional tiny `/responses` smoke test, and Claude runtime readiness checks when enabled.
- Cached successful OpenAI upstream model-list responses for five minutes by default, with an explicit `models_cache_ttl_seconds` setting, auth/account-scoped invalidation, and `/v1/relay/status` cache diagnostics.
- Documented the OpenAI model-list cache controls, including `AIRELAYS_OPENAI_MODELS_CACHE_TTL_SECONDS` and `models_cache_ttl_seconds = 0` to disable caching.

## 0.2.4

- Normalized verified `/v1/responses` file-input paths so local `POST /v1/files` ids and raw Base64 `input_file.file_data` plus `filename` are accepted on the subscription-backed route.
- Rejected unsupported token-limit parameters explicitly across the OpenAI-shaped text-generation routes: `max_output_tokens` on `/v1/responses`, `max_completion_tokens` on `/v1/chat/completions`, and `max_tokens` on `/v1/completions`.
- Redacted inline `file_data` payloads from JSONL traffic logs and refreshed the user documentation to match the verified compatibility boundary.

## 0.2.3

- Removed the remaining OpenAI API-key exchange, storage, and setup hints so AIRelays stays strictly subscription-backed for upstream inference.
- Improved native `POST /v1/responses` compatibility by normalizing direct `text.format.type=json_schema` requests and accepting `conversation` as either a string id or `{ "id": "..." }`.
- Clarified the `/v1/responses` compatibility boundary across the user documentation and LLM index files.

## 0.2.2

- Reused and migrated legacy AIRelay macOS keychain sessions so existing subscription logins continue to work after upgrading to AIRelays.
- Clarified compatibility with earlier AIRelay config, data-dir, and keychain naming in the user documentation.
- Added explicit protected-mode and open-mode `curl` verification examples for listing models and sending a simple query.

## 0.2.1

- Added first-class open local relay mode through `airelays init --no-auth`, `airelays serve --no-auth`, and `AIRELAYS_REQUIRE_BEARER_AUTH=false`.
- Clarified protected and open launch modes across the CLI output, landing page, and user documentation.
- Added a MkDocs documentation site and GitHub release workflow for tagged releases, GitHub Releases, docs deployment, and PyPI trusted publishing.

## 0.2.0

- Published `AIRelays` with package name `airelays` and CLI command `airelays`.
- Added `airelays init`, `airelays status`, `airelays token show`, and `airelays token rotate` for first-run setup and local token management.
- Added a local config file at `~/.config/airelays/config.toml` with `AIRELAYS_*` overrides and legacy `OPENAI_ENDPOINT_*` fallback support.
- Added default bearer-token protection for `/v1/*` and `/no-tools/v1/*`.
- Added in-memory per-IP request rate limits, concurrent-request caps, and temporary blocks after repeated invalid tokens.
- Added protected `GET /v1/relay/status` diagnostics and reduced public `GET /healthz` to a minimal liveness payload.
- Added bounded local upload limits with explicit `413` failures instead of unbounded buffering.
- Made bearer-token bootstrap explicit by default: `airelays init` creates the token, while `airelays serve` fails fast if bearer auth is enabled and no token is configured.
- Clarified relay-token setup and client usage in the CLI output, startup banner, and user documentation.
- Corrected auth readiness so API-key-only state is not reported as request-ready.
- Moved upstream auth storage to AIRelays-owned state instead of reusing Codex-owned auth storage.
- Removed `codex_home` from the public AIRelays configuration surface.
- Added structured security events to the JSONL traffic log.
- Updated the landing page, docs, and API guidance to use the AIRelays token as the client credential.
- Added GitHub Actions workflows for CI and PyPI publishing preparation.
