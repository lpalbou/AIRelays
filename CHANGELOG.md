# Changelog

## 0.7.0

### Added

- Reasoning modes are now first-class on both runtimes. `/v1/models` publishes each model's supported modes and default under `airelays.reasoning` (verified against the live upstreams: OpenAI models accept `none`, `low`, `medium`, `high`, `xhigh` and reject `minimal`; Claude models accept `low`, `medium`, `high`, `xhigh`, `max`). `reasoning_effort` now works on `claude:*` requests too, mapped to the local CLI's `--effort` flag with case normalization — unsupported values are rejected with the supported list, because the CLI would otherwise silently fall back to its default. OpenAI forwarding is unchanged (`reasoning_effort` / `reasoning.effort` verbatim; invalid values surface the upstream's own error). The desktop Models tab and `airelays models` advertise each model's reasoning modes alongside the id.

## 0.6.0

### Added

- Configurable extra OpenAI models: `[providers.openai] extra_models` / `AIRELAYS_OPENAI_EXTRA_MODELS` advertises model ids the upstream serves but does not list in its catalog endpoint. Verified live and shipped as defaults: `gpt-5.6-sol` and `gpt-5.6-terra`. Extras are deduplicated if the catalog catches up, only extend a successfully fetched catalog (they never mask an upstream auth error), and unlisted ids continue to pass through to the upstream regardless. The desktop Settings expose the list.
- Accounts card token breakdown: each OpenAI account row gains a "more" hover showing, for the current 5h window, the input/output tokens the relay served per model plus overall totals — the ground truth behind the usage bars. Counts cover traffic through AIRelays only (usage from other apps on the same account is not included, and the panel says so); the tally resets when the window rolls over, survives relay restarts, and is exposed as `window_tokens` on the account status payload.

### Changed

- Multi-account balancing is now capacity-aware by default (`balance = "balanced"`): each request routes to the account with the most remaining short-window quota, so consumption equalizes as a percentage of each plan's own capacity — with plans of very different sizes (e.g. Plus + Enterprise), equal request counts drained the small plan many times faster while the large one idled. Usage is probed at launch and refreshed in the background (~12 probes/hour/account), probes are TTL-cached and single-flighted to protect the upstream endpoint, and every probe also feeds proactive limit-benching. `round_robin` (strictly equal request counts) and `ordered` remain available.
- The running version is now visible everywhere the product presents itself: the desktop window title, tray tooltip, and dashboard sidebar (e.g. "AIRelays 0.6.0"), the relay landing page, and CLI report titles. Each surface reads the version from its component's single canonical source at run time — `airelay.__version__` for the relay (`pyproject.toml` derives from it) and `Cargo.toml` for the desktop app (`tauri.conf.json` inherits it) — so what is displayed can never drift from what is installed. `scripts/set_version.py` bumps every component in one command, and the release workflows enforce agreement before publishing.

## 0.5.0

### Changed

- Multi-account requests are now balanced across accounts with capacity by default (`[providers.openai] balance = "round_robin"`), so charge always spreads instead of draining the first account while others idle. `"ordered"` (first account until its limit) remains available as an opt-in, and the desktop app now writes and exposes the balancing setting (previously the desktop-rendered config could not carry it at all). Balanced selection is least-recently-selected, which stays fair when accounts drop in and out of rotation; conversation affinity is unchanged.
- Launch-time pool warm-up: a multi-account relay probes each account's usage and model catalog in the background right after startup, benching accounts that are already at their limit and enabling model-aware balancing from the very first request — a fresh process no longer relearns an exhausted account by wasting a request on a guaranteed 429. Logged as an `account_pool_warmed` traffic record.
- `POST /v1/relay/accounts/refresh` (desktop Refresh, `airelays accounts refresh`) no longer clears usage-limit holds before re-checking: releases are evidence-gated on a fresh usage report showing capacity. The previous clear-first design opened a window in which live traffic hit a known-exhausted account and earned an extra 429 (observed in production traffic logs). The refresh action now also writes an `accounts_refresh` traffic record.

### Fixed

- Account failover now covers every account-scoped failure, not just upstream HTTP errors: transport failures (DNS, connect, TLS, timeouts) are surfaced as structured 502s and routed to the next account, dead credentials (including persistent 401 after a token refresh) bench the account and fail over instead of failing the request while a healthy account sits idle.
- An account at its very last failover attempt is now benched like any other, so an exhausted final account is no longer re-selected and hammered with guaranteed 429s by subsequent requests.
- Usage-driven benching reads every reached-limit signal the upstream payload carries (`limit_reached`, `allowed`, the nullable reached-type, and per-window percentages) instead of a single undocumented field, benches for the longest exhausted window (a maxed weekly window no longer re-bench-flaps every 5 hours), and a stale usage snapshot can no longer release a bench placed after the snapshot was taken.
- Usage-limit markers in error bodies are only trusted when structured (or on a real 429), so a client error whose body merely echoes marker text can no longer bench the entire pool.
- Failover order now tries healthy accounts before benched ones; a transient-error cooldown can no longer truncate a multi-hour usage bench; benches survive transient account-storage read failures instead of being laundered by pool reloads; dropped accounts' HTTP clients are closed instead of leaked; conversation-affinity eviction no longer wipes every affinity at once; "all accounts unavailable" is only reported when it is true, with an accurate earliest-retry time.

## 0.4.0

### Added

- Claude runtime: explicit `claude:*` model ids served through the local `claude` CLI under its existing subscription sign-in. Text `/v1/chat/completions` and `/v1/completions` (stream and non-stream), stateless, loopback-only; unsupported routes, states, and parameters are rejected locally and explicitly (ADR 0004). Enabled by default; opt out with `[providers.claude].enabled = false` or `AIRELAYS_ENABLE_CLAUDE=false`. `airelays doctor` includes Claude readiness checks.
- Claude sign-in and sign-out: browser flow (`claude auth login --claudeai`), headless flow (`claude setup-token` on any browser-equipped machine, then `airelays claude set-token` — stored 0600, survives service managers and reboots), and `airelays claude logout` (removes the stored token, then runs `claude auth logout`). The desktop app offers the same flows: a sign-in split button ("In a browser (this machine)" / "With a token (any device)"), a code-entry field for the browser flow's final step, and per-row sign-out with a confirmation that explains the machine-wide effect on the `claude` CLI.
- Claude subscription usage: `GET /v1/subscription/status?provider=claude` returns the 5-hour and weekly windows (plus per-model weekly caps when reported) in the same normalized shape as OpenAI usage. The upstream usage endpoint is aggressively rate-limited, so the relay caches briefly, single-flights refreshes, serves honestly-labeled stale snapshots during lockouts, and persists the guardrail state across restarts. The desktop Accounts card renders both providers identically — usage bars, reset times, and an "At limit" badge.
- Provider identity in the model catalog: `/v1/models` Claude entries carry provider, upstream model, and route-capability metadata; `airelays models` and the desktop Models tab group model ids by provider.

### Changed

- The Claude runtime is no longer labeled experimental. The `experimental` field was removed from `/v1/models` entries and provider status payloads, Claude model `owned_by` is now `airelays-claude-subscription`, error messages say "The Claude runtime …", and UI badges and labels dropped the tag. The enable switch is now `AIRELAYS_ENABLE_CLAUDE` (the legacy `AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL` name is still honored, and desktop settings files written under the old name keep loading). The runtime's guardrails are unchanged: local-only, loopback-only, stateless, explicit rejections outside the published route subset.
- Provider routing is explicit and model-driven: `claude:*` ids select the Claude runtime when it is enabled; other ids select the OpenAI runtime. AIRelays rejects requests when the selected runtime is disabled or the route is outside that runtime's published subset.
- The relay's status route refreshes Claude CLI probes in the background with hard timeouts; status responses return immediately even when the `claude` binary is slow or hung.
- Desktop Overview: the Accounts card shows OpenAI and Claude section headers. The Claude sign-in button shows an "Off in network mode" badge while the relay is exposed to the network (the runtime is loopback-only) and offers a one-click switch to "This machine only"; a signed-in Claude that network mode paused stays visible as a "Paused" row.
- Documented upstream terms and personal use: the disclaimer (root and docs site) explains that AIRelays drives provider-owned tooling under the account holder's own sign-ins for ordinary individual use, and links the official Anthropic and OpenAI terms and policy pages to review. A new FAQ entry summarizes the same point.

### Fixed

- Claude routes no longer fail with `422 Claude experimental mode does not support temperature` when standard OpenAI SDK clients send sampling parameters. `temperature`, `top_p`, `presence_penalty`, and `frequency_penalty` now get the same documented adaptation as on the OpenAI runtime: stripped (the local `claude` CLI has no sampling controls), disclosed in the `x-airelays-ignored-parameters` response header, and logged as a `compatibility_adaptation` traffic record.
- Desktop Claude sign-in on a GUI-launched app: the extended startup PATH also locates the `claude` CLI, and on Windows the `claude` .cmd shim is invoked correctly.
- Headless Claude setup: `airelays claude set-token` stores a token that keeps working under systemd, launchd, and docker, where shell exports never reach the relay process.

## 0.3.0

### Added

- Models listing: a desktop Models tab and a CLI command (`airelays models`, `--json` supported) list every model id the running relay accepts, with one-click copy of the exact id.
- Desktop supervision: crash auto-restart with capped backoff and native notifications (Settings → Launch, default on; a deliberate Stop never respawns), "Start AIRelays at login" via the OS-native mechanism on all three platforms, and automatic relay start when the app opens (skipped when a relay already answers on the address).
- Tray activity indicator: the tray icon blinks once when new requests were served since the last poll (`requests_total` in `/v1/relay/status`).
- Sign-in flows can be cancelled from the banner; a deliberate cancel is reported as information, not a failure.
- Account balancing proactively skips accounts whose usage report already shows a reached limit, in addition to reacting to live 429s; `POST /v1/relay/accounts/refresh` (CLI: `airelays accounts refresh`) clears the holds and re-checks capacity on demand.
- Multiple-account management from the desktop: per-account sign-out buttons and an "Add account" split button offering browser or device-code sign-in per attempt.

### Changed

- Relay health is now reported truthfully and robustly. The desktop derives liveness from `/healthz` (exempt from auth and rate limits) with brief down-flip debouncing, fetches the rich status payload separately, and shows "Running — not responding" instead of "Stopped" when the relay process is alive but not answering. The dashboard, sidebar, and tray always agree.
- The relay's status route no longer runs provider probes in the request path: credential reads are briefly cached and account rediscovery is throttled. Status responses return immediately.
- Per-line stream logging (one record per upstream SSE line) is now opt-in via `[logging] stream_lines` / `AIRELAYS_LOG_STREAM_LINES` (default off). Summary records — request, tokens, status, errors — are always logged. This keeps traffic logs compact and the Traffic view complete on busy relays.
- The Overview tab was restructured: one Accounts card with per-account usage bars labeled with the real window names ("5h window", "Weekly"), a single Refresh action, quieter tertiary buttons, and amber (not red) for self-resetting quota limits. Endpoint URLs appear once, and only real, reachable addresses are listed (self-assigned 169.254.x.x interfaces are filtered out).
- Documented the compatibility layer in README ("What The Relay Changes") and docs/api.md: rejected sampling parameters are stripped and disclosed via the `x-airelays-ignored-parameters` header; `reasoning_effort` passes through verbatim, and omitting it means the upstream's low default — set it explicitly for parity with the official apps.

### Fixed

- Desktop: the app now extends its PATH with standard user bin directories at startup (GUI apps inherit a minimal PATH), so PATH-installed relays are found.
- Traffic view: real requests are no longer evicted from the view by monitoring or stream chatter; token counts (input/output) are shown per request; the reader's memory and CPU are bounded.
- Tray icon: the icon re-asserts itself on every poll, so it can no longer stick out of sync with the actual connection state.
- Sign-in flows: replacing or cancelling a sign-in cleans up completely (no orphaned flow holding the OAuth callback port), abandoned flows time out, and failure messages include the CLI's stderr.
- Supervision edge cases: concurrent starts cannot double-spawn the relay; a benign start collision no longer mislabels a healthy relay as Failed; auto-restart stands down when an external relay owns the port; Restart explains itself when the answering relay isn't app-managed.
- Multiple-account discovery derives identity from the auth record (email/plan now always populate), and a newly added account is picked up by the running relay without a restart.
- Model-aware routing for mixed-plan pools: requests route only to accounts that expose the requested model, and `/v1/models` advertises the intersection.
- Headless sign-in: `airelays login` auto-selects the device-code flow on SSH and displayless machines; device-flow errors are readable.

## 0.2.5

- Added `airelays doctor` for local setup checks, relay-token validation, OpenAI login readiness, live upstream `/models` probing, and an optional tiny `/responses` smoke test.
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
