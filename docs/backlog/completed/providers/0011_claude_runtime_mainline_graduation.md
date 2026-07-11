# Completed: Claude runtime mainline graduation

## Metadata

- Created: 2026-07-10
- Status: Completed
- Completed: 2026-07-10
- Version: 0.4.0

## ADR status

- Governing ADRs: 0001, 0003, 0004
- ADR impact: ADR 0004 amended (label removal recorded; guardrails unchanged)

## Context

The Claude runtime lived on a local `experimental` branch while public releases (0.2.5, 0.3.0) shipped from `main` without it. Standard OpenAI SDK clients could not use `claude:*` models at all because the runtime rejected default sampling parameters with HTTP 422.

## What we did

- Applied the documented strip-and-disclose compatibility adaptation to the Claude routes: `temperature`, `top_p`, `presence_penalty`, and `frequency_penalty` are removed (the local `claude` CLI has no sampling controls), disclosed via `x-airelays-ignored-parameters`, and logged as `compatibility_adaptation` records.
- Merged the full Claude runtime to `main` via PR #1 with reconciled version identity and changelog history.
- Removed the "experimental" label across the API surface, UI, CLI output, and docs; renamed the enable switch to `AIRELAYS_ENABLE_CLAUDE` with the legacy name still honored; desktop settings files keep loading via a serde alias.
- Documented the upstream-terms/personal-use boundary in the disclaimer with links to the official Anthropic and OpenAI policy pages.
- Released 0.4.0: PyPI package, GitHub Release with desktop installers (DMG/NSIS/AppImage/deb) attached to the same `v0.4.0` release, docs deployed.

## Validation

- `pytest -q` (148 at release time), `python -m compileall src tests`, `mkdocs build -q`, `cargo check`
- Live per-model verification on a spare port: 13/13 checks across all 8 advertised models (realistic chat, stream, completions, responses), disclosure headers verified
- Release workflow green end to end: PyPI publish, GitHub Release, docs deploy; desktop workflow attached installers after version-guard checks

## Residual risks / follow-ups

- The `claude setup-token` paste flow accepts truncated tokens; see proposed item 0015.
- Anthropic's subscription-usage policy is in flux (June 15 pause note); the disclaimer links the official pages and users must re-check them.
