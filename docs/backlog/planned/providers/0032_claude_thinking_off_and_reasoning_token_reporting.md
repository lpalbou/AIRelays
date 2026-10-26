# Planned: thinking off (`reasoning_effort: "none"`) on Claude models, and honest reasoning-token reporting

## Metadata

- Created: 2026-10-26
- Status: Planned
- Area: providers (Claude runtime, `src/airelay/providers.py`)
- Reported by: AbstractCore knowledge-graph extraction benchmark (2026-10-25/26), which could not run Claude
  Haiku 5.5 "thinking off" through AIRelays and found its results silently included hidden reasoning.

## Context

OpenAI-compatible clients turn reasoning off with `reasoning_effort: "none"`. AIRelays accepts it on OpenAI models
(`OPENAI_REASONING_MODES` starts with `none`, and the Codex upstream runs at `none` by default). On Claude models it is
rejected with 422, so a client has no way to ask for a Claude answer without thinking. AbstractCore's unified
`thinking=False` now sends exactly `reasoning_effort: "none"` (abstractcore commit f34a84a1), so every
AbstractCore-driven "thinking off" request to a Claude model through AIRelays either fails or, if the client omits the
field, runs with the CLI's adaptive thinking at its default effort.

The cost is real and invisible today:

- Claude Haiku 5.5 spends 3–9k hidden output tokens per call even for a short prose answer; visible output comes out at
  0.3–1.2 characters per billed token, against about 4.5 for GPT models on the same relay.
- Long structured outputs run out of budget: in the benchmark, 4 of 9 Claude calls with `max_tokens` 12000 ended
  `finish_reason: "length"` with truncated or empty content, while GPT models finished the same task in 1.7–4k tokens.
- `usage` reports these tokens as plain `completion_tokens`; `completion_tokens_details.reasoning_tokens` is absent, so
  clients cannot tell reasoning from answer tokens.

Thinking off is a real capability of the model: the Anthropic API documents disabling thinking for Haiku 5.5 at
effort `high` or below. The open question is how to reach it through the `claude` CLI on a subscription, which is the
only upstream AIRelays uses for Claude (ADR 0004; `strip_api_key_env = true`).

## Current code reality (read 2026-10-26, AIRelays 0.22.0 at 6728f6f)

- `src/airelay/providers.py`: `CLAUDE_REASONING_MODES = ("low", "medium", "high", "xhigh", "max")`; the comment above it
  states the CLI's `--effort` flag accepts exactly these and silently falls back on unknown values.
- `_claude_effort()` validates `reasoning_effort` against the model's modes and raises 422
  `unsupported_for_provider` for anything else, including `"none"` (good: no silent degradation, but no way to say off).
- `ProviderModel.reasoning_default` is documented as "the claude CLI applies its own adaptive default, which is
  model-controlled (reported as null)".
- `ClaudeRuntime._build_command()` runs `claude -p --model <m> --tools "" --setting-sources "" --strict-mcp-config
  --disable-slash-commands --no-session-persistence [--system-prompt …] [--effort <e>] [--json-schema …]
  --input-format stream-json --output-format stream-json --verbose`. `--setting-sources ""` means no user or project
  Claude settings are loaded; nothing in the command or in `_subprocess_env()` touches thinking.
- `--settings settings.json` is passed only for tool requests (`_tool_arguments`, a `PreToolUse` guard hook), never to
  control thinking.
- Usage comes from the streamed message events (backlog 0026); thinking blocks are not counted separately.

## Evidence (measured 2026-10-26, Claude Code CLI 2.1.310, model `claude-haiku-5-5`)

Prompt: "Write exactly 250 words of plain prose explaining what Git is." (about 1,550 visible characters, roughly 400
tokens). Thinking presence read from the `--verbose` JSON transcript (`content` block types); the thinking block text is
redacted (0 characters) in every case.

| Route / flags | Thinking switch | Thinking block | Output tokens | Visible chars |
|---|---|---|---|---|
| AIRelays `/v1/chat/completions` | field omitted | (not visible) | 4,000, `finish_reason` length | 0 |
| AIRelays `/v1/chat/completions` | `reasoning_effort: "none"` | — | HTTP 422 (unsupported) | — |
| AIRelays `/v1/chat/completions` | `thinking: {"type": "disabled"}` | (not visible) | unchanged vs default (extraction task: 11,662 vs 11,765) | same |
| AIRelays `/v1/messages` | native `thinking` | — | HTTP 404 (route does not exist) | — |
| CLI with the relay's exact flags | none | yes | 3,713 | 1,538 |
| CLI with the relay's exact flags | env `CLAUDE_CODE_DISABLE_THINKING=1` | yes | 3,483 | 1,544 |
| CLI, user settings loaded | env `MAX_THINKING_TOKENS=0` | yes | 5,200 | 1,537 |
| CLI, user settings loaded | env `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1` | yes | 3,759 | 1,550 |
| CLI, user settings loaded | `--settings {"alwaysThinkingEnabled": false}` | yes | 2,627–3,789 | ~1,500 |
| CLI, user settings loaded | `--settings {"thinkingEnabled": false}` / `{"disableThinking": true}` | yes | 3,259 / 3,075 | ~1,530 |
| CLI, user settings loaded | all of the above + `--effort low` | yes | 2,800–3,268 | ~1,550 |

Thinking-related names present in the CLI binary (candidates, not all verified to exist as public switches):
`CLAUDE_CODE_DISABLE_THINKING`, `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING`, `DISABLE_INTERLEAVED_THINKING`,
`MAX_THINKING_TOKENS`, `CLAUDE_CODE_EFFORT_LEVEL`, settings keys `alwaysThinkingEnabled`, `thinkingEnabled`,
`disableThinking`, `thinkingBudget`, `effortLevel`.

Not yet tested with the relay's exact flags (a shell quoting error voided these probes): `--effort low|medium` alone,
`--settings` thinking keys, and all switches combined; other models (`sonnet`, `opus`, `fable`); the stream-json
`initialize` control request (the Agent SDK path) with a thinking option such as `maxThinkingTokens: 0`.

## Plan

1. **Probe matrix (spike, untracked script).** For each Claude model the relay lists × each candidate mechanism
   (env switches, `--settings` JSON keys, `--effort` levels ≤ high, combinations, stream-json `initialize` control options)
   run the relay's exact command and record: thinking block present or not (stream-json events), output tokens,
   visible characters, latency. Success criterion for "off": no thinking block and output tokens within ~1.3× the
   visible-text token count. Record the CLI version; re-run on CLI upgrades.
2. **If a mechanism works:** accept `reasoning_effort: "none"` for the models where it was verified (per-model
   `reasoning_modes`, discovered or verified, not a blanket constant), map it in `_build_command` / `_subprocess_env`
   to that mechanism, and advertise `none` in `/v1/models`. `none` must never fall back silently: if the CLI still emits
   thinking under it (detected from the stream), fail loudly or flag the response (`x-airelays-…` header and a
   disclosure), never claim "off".
3. **If no mechanism works on the subscription path:** keep the 422 but make it actionable (say thinking cannot be
   disabled through the Claude CLI for this model, suggest `low`), document it in `docs/capabilities.md`, and set
   `reasoning_default` / `/v1/models` metadata so clients can see that Claude always reasons here.
4. **Either way, report reasoning tokens.** Split usage into answer vs reasoning from the stream (thinking block deltas
   or a per-block token count when available) and return `completion_tokens_details.reasoning_tokens`, so clients can
   size `max_tokens` and see what they pay for. Extends backlog 0026's usage contract.
5. Docs: `docs/capabilities.md` (Claude reasoning: modes, default, off support per model), `docs/api.md`,
   CHANGELOG; regenerate `llms-full.txt`.

## Acceptance criteria

- `POST /v1/chat/completions` with `model: claude-haiku-5-5`, `reasoning_effort: "none"` either returns an answer with
  no thinking (verified: no thinking block in the CLI stream, `reasoning_tokens` 0, prose ≥ 3 visible characters per
  output token) or a 422 that says why, per model, matching `/v1/models`.
- `usage.completion_tokens_details.reasoning_tokens` is present for Claude responses and non-zero whenever the stream
  contained thinking.
- A regression test pins the command/env for each `reasoning_effort` value and fails if `none` is accepted without the
  verified mechanism.

## Non-goals

- Changing the user's global Claude settings (`~/.claude/settings.json`) or relying on them: the relay deliberately runs
  with `--setting-sources ""`.
- Switching Claude to an API-key upstream; that is a separate product decision under ADR 0002/0004.

## Validation

- `pytest -q`; the probe matrix re-run live on haiku, sonnet and opus with results recorded in the completion report;
  an end-to-end check through AbstractCore: `create_llm("openai-compatible", model="claude-haiku-5-5", …)` with
  `thinking=False` returns content with `reasoning_tokens` 0.

## Related

- Backlog 0026 (chat-completions usage contract), 0027 (stream-json runner, tools on Claude), 0011 (Claude runtime).
- ADR 0004 (provider runtime boundary and local adapters).
- AbstractCore: `docs/reasoning-control.md` precedence rule (commit f34a84a1); benchmark notes in
  `abstractcore/untracked/hy-memory/kg/judge/FINAL.md`.
