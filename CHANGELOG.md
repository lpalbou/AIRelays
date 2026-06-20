# Changelog

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
