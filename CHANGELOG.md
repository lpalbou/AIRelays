# Changelog

## Unreleased

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
