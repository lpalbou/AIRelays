# Changelog

## Unreleased

- Fixed the tray icon drifting out of sync with the real connection state: a missed or failed icon update used to stick until the next reachability change; the status loop now re-asserts the icon on every poll (no-op when already correct), so any drift self-heals within ~1.5s.
- Added a tray activity blink: the relay counts real served requests (`requests_total` in `/v1/relay/status`) and the tray flashes a bright bolt once whenever new requests were served since the last poll — a lightweight load indicator.
- Closed two CLI/desktop parity gaps: `airelays models` lists every model id the running relay accepts (grouped by provider, `--json` supported), and `airelays claude logout` performs the same complete Claude sign-out as the desktop (stored token first, then `claude auth logout`, results reported separately).

- Added a Models tab to the desktop app: every model id the endpoint accepts, grouped by provider with experimental badges, a filter, and a one-click copy of the exact id for curl/SDK use.
- Added a Cancel button to the "Waiting for sign-in" banner; a deliberate cancel reports as information, never as a sign-in failure.
- Disabled the Claude sign-in button while already signed in (the claude CLI holds a single account; the tooltip points to the row's sign-out for switching).

- Restructured the Accounts card after a two-reviewer adversarial pass: "OpenAI" and "Claude" are now real section headers (13px/700 with hairline rules) above their accounts instead of faint micro-labels weaker than the emails under them, each section carries its own "Sign in" split button in the header (the pooled bottom buttons are gone), emails demoted to regular weight, and the routing caption now visually belongs to the OpenAI section.
- Added Claude sign-out with full parity to OpenAI rows: same icon in the same grid slot, a confirmation dialog that states the real blast radius (signs the claude CLI out machine-wide — including Claude Code — and removes any AIRelays-stored token), token-file-first ordering so a failed CLI step can never leave silent ghost auth, and honest partial-failure reporting with the manual remediation. The sign-out also appears when only a stored token exists (the state where a stale token masks CLI auth), a deliberately signed-out row shows a neutral "Not signed in" badge instead of an amber warning, and "Remove stored token" in the token dialog is demoted to a quiet scoped action.

- Added Claude subscription usage with the exact same rendering as OpenAI accounts: 5h-window and Weekly bars with "x% used · resets in …" details, per-model weekly caps (Sonnet/Opus) when reported, and an "At limit" badge. Source: the same endpoint Claude Code's own `/usage` command calls (undocumented; the relay caches it for 30s, sends the required claude-code User-Agent, and degrades gracefully when unavailable). The relay exposes it as `GET /v1/subscription/status?provider=claude`, normalized to the identical shape as the OpenAI payload; credentials resolve from the stored token file, then the claude CLI's own stores (macOS keychain, `~/.claude/.credentials.json`). The desktop Accounts card now renders one identical block per account for both providers, with faint OPENAI/CLAUDE section labels, and the OpenAI sign-in button is labeled "OpenAI" to match "Claude".

- Fixed Claude sign-in end to end after a three-reviewer adversarial pass (empirical CLI probe, desktop login chain, relay runtime). Confirmed root causes: (1) in "Devices on my network" mode the relay force-disables Claude, yet the dashboard still offered sign-in — a "successful" login that visibly did nothing; the Claude button now only appears in "This machine only" mode, with the existing note explaining why. (2) `claude auth login` has no localhost callback: its browser flow ends in a code the user must give back to the CLI, which is impossible with the GUI's closed stdin — sign-in flows now run with piped stdin, the banner gains a "paste the code" field wired to the CLI, and abandoned flows are killed by a real timeout (previously a hung login blocked future attempts forever). (3) A stale stored Claude token silently overrides the CLI's own sign-in for every relay request; the token dialog now explains this and offers "Remove stored token". Sign-in failure toasts now include stderr (where CLIs print real errors), and three claude login processes hung since Jul 5 were reaped.

- Fixed Claude sign-in from the desktop app. GUI apps inherit a minimal PATH without `~/.local/bin` (or Homebrew paths), so the external `claude` CLI was "not found" when launched from the dashboard even though it works in a terminal; the app now extends its PATH with the standard user bin directories at startup, fixing every child spawn (relay, sign-ins, doctor) at once.
- Gave Claude the same sign-in experience as OpenAI: a split button with "In a browser (this machine)" (runs the Claude CLI's browser flow, with the sign-in URL surfaced for copying like OpenAI's) and "With a token (any device)" (paste the token printed by `claude setup-token` on any browser-equipped machine into a dialog; stored in the same 0600 file as `airelays claude set-token` and picked up without a restart).

- Fixed the Traffic view showing a seemingly random handful of requests (a few from HH:59 of past hours, a few current) on a busy relay. Root cause: the relay logged every raw upstream SSE line (`upstream_stream_line`, ~450 records per streamed response — 128 MB of the 185 MB the log grew in one hour), so the reader's small per-file tail covered only the last minute of each hourly file. Per-line stream logging is now opt-in (`[logging] stream_lines`, `AIRELAYS_LOG_STREAM_LINES`, default off — summary records with tokens/status/errors are always kept), and the desktop reader skips stream chatter before its record budget, reads a larger tail, and clips oversized strings in the detail pane. Verified against the real 185 MB logs: 200 continuous requests spanning the full window instead of 4 random rows.

- Added crash auto-restart to the desktop app: a relay that exits without a user stop is respawned with capped exponential backoff (2s→60s, 5 consecutive attempts before giving up), with native notifications on the crash and on give-up. A deliberate Stop never triggers a respawn, and a healthy run resets the retry budget. Toggle: Settings → Launch → "Restart the relay automatically if it crashes" (default on).
- Added "Start AIRelays at login" (Settings → Desktop App) via the OS-native mechanism on all three platforms (macOS launch agent, Windows registry, Linux autostart entry), and the app now starts the relay automatically when it opens — unless a relay is already answering on the configured address (e.g. CLI-managed), which it respects instead of colliding with. Toggle: Settings → Launch → "Start the relay when this app opens".
- Documented the compatibility layer (README "What The Relay Changes" and docs/api.md): the subscription backend rejects `temperature`/`top_p`/`presence_penalty`/`frequency_penalty` so the relay strips them and discloses it via the `x-airelays-ignored-parameters` header and a `compatibility_adaptation` traffic record; `reasoning_effort` passes through verbatim but omitting it means upstream effort `none` (below the official apps' `medium`) — verified against the live upstream.
- Fixed the endpoint list to show only real, reachable URLs: self-assigned link-local addresses (`169.254.x.x` from bridges/unconfigured interfaces) are filtered out, and the URL list appears once (in "Connect Your App") instead of twice.
- Redesigned the Overview tab after a four-reviewer adversarial pass: merged the duplicated Providers and Usage cards into one Accounts card (email, plan, status badge, sign-out, and usage bars per account on a fixed alignment grid), labeled the usage windows with the upstream's real window names ("5h window", "Weekly") instead of two identical "Requests" rows, reserved red for actual failures (a self-resetting quota limit is amber; warning callouts have neutral body text), demoted tertiary actions to quiet ghost buttons, tightened the type scale to four sizes, and consolidated "Recheck limits" plus usage refresh into one Refresh button.

- Stopped monitoring endpoints (`/v1/relay/status`, `/v1/subscription/status`, `/healthz`, `/v1/account/rate_limits`) from being written to the JSONL traffic log. The desktop app polls these continuously, and they were flooding the log and evicting real requests from the window the Traffic view reads — so a successful request could show as "No requests yet". Real requests now persist and appear.
- Added input/output token counts to the Traffic view (and they were already in the JSONL logs for CLI users), tolerant of both the Responses (`input_tokens`/`output_tokens`) and Chat (`prompt_tokens`/`completion_tokens`) shapes.
- Hardened the Traffic reader so old log files (written before monitoring endpoints were excluded, containing tens of thousands of status-poll lines) can no longer evict real requests from the view: it now skips monitoring records while reading against a real-request budget, and drops orphaned "/" rows whose inbound record aged out.
- Made account balancing proactively skip an account whose usage report already shows a reached limit (benched until the soonest window reset), so the pool no longer wastes a request discovering a limit the usage data already revealed — on top of the existing reactive benching after a live 429.
- Simplified per-account sign-in/sign-out. The desktop app gained a per-account sign-out button with a confirmation dialog (previously sign-out was only possible from the CLI), a single consolidated status badge per account, and an "Add account" split button that offers browser or code sign-in at click time instead of a separate persistent method toggle. The CLI made `logout` the one canonical sign-out command, turned `airelays accounts` into a self-documenting hub that prints how to add/sign-out/reorder, corrected the post-logout summary (it no longer tells you to log in again when other accounts remain), and distinguishes "unknown account" from "ambiguous prefix".

- Fixed multiple-account discovery and made it usable end to end. Account email and plan were read from keys that no real login writes (they live inside the signed token), so extra accounts showed blank identity and email-based selectors (`logout <email>`, `accounts order <email>`) failed; discovery now derives identity from the auth record. A newly added account also required a manual relay restart to take effect — the running relay now hot-reloads its account pool within a couple of seconds, so sign-in through the desktop app or CLI is picked up automatically with no restart and no dropped requests.
- Added model-aware routing for mixed-plan pools (e.g. a Plus account plus an Enterprise account): `/v1/models` now advertises the intersection of every account's models, requests route only to accounts that expose the requested model, and failover skips accounts that cannot serve it — preventing nondeterministic "model unavailable" errors and quota failures masked as model errors.
- Made `airelays doctor` exercise the account pool instead of probing only the first account, so its verdict reflects real request routing and failover.
- Verified the load-balancing failover live: with an exhausted Plus account and an Enterprise account, a real request records `account_selected → upstream_response_error 429 → account_failover → completed`, serving from the account with remaining budget.

- Made headless/server sign-in first-class. `airelays login` now auto-selects the device-code flow on SSH sessions and displayless Linux (override with `--browser`), the browser flow prints an explicit warning that its URL only works on the relay's own machine (with the `ssh -L 1455:localhost:1455` tunnel alternative), and every login hint in `init`/`status`/`doctor` becomes `airelays login --device` on headless machines.
- Hardened the device-code flow: approval-wait heartbeat instead of silence, honest timeout messages using the configured duration, tolerant `user_code`/`usercode` payload parsing, readable errors for unexpected upstream statuses, and a first test suite for the flow. Auth failures now print a clean message instead of a Python traceback.
- Added `airelays claude set-token`: stores a Claude Code OAuth token (from `claude setup-token` run on any browser-equipped machine) in a 0600 file that AIRelays injects into every `claude` invocation — unlike shell exports, it survives systemd/launchd/docker and reboots. Status and doctor now report the Claude token source (`file`/`env`/`none`), and Claude CLI failures surface the real upstream error (e.g. an invalid-token 401) instead of "claude exited with code 1".

- Added multiple-OpenAI-account support for a single user: `airelays login` is now additive (a second sign-in with a different account is stored alongside the first instead of silently overwriting it — fixing a pre-existing data-loss bug), accounts live in per-directory auth slots with isolated keyring entries, and a new `airelays accounts` command lists, reorders, and removes them by email.
- Added account balancing in the relay: ordered spillover by default (first account until its usage limit, then the next, returning when the limit resets) or `balance = "round_robin"` to spread requests evenly; failover triggers on upstream usage-limit and 5xx errors, only before the first streamed byte, with conversations pinned to one account per session. Requests and failovers are recorded in the traffic logs with the serving account.
- Extended status surfaces for multiple accounts: `airelays status` and `doctor` report each account (a healthy primary no longer masks a dead standby login), `/v1/relay/status` lists per-account state, and `/v1/subscription/status` gains `account` and `all_accounts` query parameters. Single-account installs see no change anywhere.
- Updated the desktop app for multiple accounts: per-account provider rows (Active/Standby/Limit reached), per-account usage blocks, an Account column in the Traffic view (shown only when more than one account exists), and the OpenAI sign-in button becomes "Add OpenAI account" once one is ready.
- Documented the feature as one user's own multiple subscriptions (README and DISCLAIMER); Claude multi-account is explicitly deferred.

- Added a branded app icon to the macOS menu bar app: a squircle-masked `AppIcon.icns` generated from `macos/AIRelaysMenuBar/assets/icon_artwork.png` via `scripts/make_icons.swift`, shown in Finder, Dock, and the app switcher.
- Replaced the generic SF Symbol status-bar glyph with custom color-coded icons bundled as SwiftPM resources: a green bolt with relay arcs when the relay is reachable, a red bolt when it is not, rendered @2x and sized 22x18 pt for the menu bar slot.
- Updated `package_app.sh` to ship the icon and resource bundle inside `AIRelaysMenuBar.app`.
- Made the packaged menu bar app self-contained: `package_app.sh` now embeds a standalone CPython runtime with `airelays` installed under `Contents/Resources/runtime`, replacing the previous `bootstrap.json` coupling to a development checkout.
- Changed the app's default launch settings to app-owned locations: the relay command resolves to the embedded runtime (`python3 -m airelays`) from the live bundle path, and the working directory defaults to `~/Library/Application Support/AIRelaysMenuBar`; stale auto-derived launch settings from earlier versions migrate automatically on startup.
- Precompiled the embedded runtime's bytecode at packaging time and set `PYTHONDONTWRITEBYTECODE=1` at run time so the relay never writes into the signed bundle and the code signature stays valid.
- Added first-class auth and network mode controls to the menu bar app: segmented "Protected (token) / Open (no auth)" and "Loopback only / Private network (LAN)" switches in the dashboard, matching check-marked items in the status-bar menu, applied live with an automatic relay restart.
- Changed the app's default listener to `0.0.0.0` so devices on the private network can reach the relay out of the box; existing app settings migrate once from the old loopback default. The `airelays` CLI default remains `127.0.0.1`.
- Enforced the relay's Claude loopback guardrail in the app: when the listener is exposed beyond loopback, the rendered config disables the experimental Claude runtime and the dashboard explains why.
- Redesigned the dashboard as a compact tabbed window (Overview, Traffic, Console) replacing the single long scroll: copyable local and LAN endpoint URLs, inline access-mode warnings, a sortable request table with a detail pane, and an auto-scrolling console. Split the monolithic `Views.swift` into focused view files.
- Made error alerts concise: alerts now show only the failing line (e.g. the final line of a Python traceback) and point to the Console tab, which keeps the full output.
- Changed the app's default relay port from collision-prone 8080 to 8317 (IANA-unregistered, not a common tool default); existing app settings still on 8080 migrate once. The `airelays` CLI default remains 8080.
- Renamed the shipped app to `AIRelays.app` (bundle name, display name, executable, and bundle identifier); the Swift package keeps its internal `AIRelaysMenuBar` name. App settings migrate automatically from the old `Application Support/AIRelaysMenuBar` folder.
- Added a cross-platform desktop app under `desktop/` (Tauri v2): a Rust core supervises the relay (embedded standalone CPython, process-group/Job-Object tree cleanup, live status polling) and owns the system tray on macOS, Windows, and Linux; the dashboard is one web codebase (Overview, Traffic, Console, Settings) with copyable endpoint URLs and a masked, copyable API key. Installers (DMG, NSIS, AppImage, deb) build from `.github/workflows/desktop.yml`.
- Hardened the desktop app after a six-reviewer adversarial pass: typed TOML rendering (no injection/escaping bugs), non-blocking process control, first-run auto-token plus dashboard auto-open, streamed login output with browser opening by default, concise error surfacing, settings validation, and WCAG-compliant dashboard contrast.
- Fixed the Swift menu bar app to also disable Claude experimental when `trust_x_forwarded_for` is enabled, matching the relay's guardrail exactly.

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
