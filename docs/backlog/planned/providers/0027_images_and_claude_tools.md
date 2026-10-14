# Planned: Images, files, conversations, `/v1/responses` and tool calls on Claude models (0.17.0)

## Metadata

- Created: 2026-10-14
- Status: Planned
- Target: 0.17.0

## Context

OpenAI-compatible clients send `image_url` content parts and `tools` to any
model. GPT models accept both. Claude models reject both with 422 because the
Claude runtime pipes a plain-text transcript into `claude -p`. The goal is to
behave like an OpenAI endpoint as far as the Claude CLI genuinely allows.

## Evidence (live probes, CLI 2.1.299)

- `--input-format stream-json` (requires `--output-format stream-json`)
  accepts image blocks: PNG, JPEG, GIF and WebP, several images in order, an
  image in an earlier transcript turn, and `{"type": "url"}` sources. Plain
  text gives the same result, usage and stop reason as `-p` text input;
  `--system-prompt`, `--effort` and `--json-schema` still work, and partial
  messages still stream.
- The CLI silently replaces images it cannot process (corrupt data, CMYK
  JPEG, animated WebP) with a text notice and reports success.
  `--replay-user-messages` shows what it actually sent.
- Each stream-json user line is a separate, separately billed model call.
- Tools: a stub MCP server (`--mcp-config`, built-in tools off with
  `--tools ""`, `--permission-prompts none`) makes the client's tools visible;
  the model emits native `tool_use` blocks, including parallel calls, before
  any execution. Stopping the CLI at `message_delta` with
  `stop_reason: "tool_use"` captured every call with zero executions. Prior
  calls and results rendered as transcript text lead to a correct final
  answer and to new native calls on later turns.

## Plan

1. One Claude runner: stream-json in and out for every request, one stdin
   line (the transcript as content blocks), stdout read with a large line
   limit and stopped at the first `result` event; the separate JSON runner is
   removed. The request is prepared once per request, not per account.
2. One shared image module for both runtimes: data URL, local `file_id`,
   https URL or the string shorthand, validated (strict base64, magic-byte
   format sniffing to PNG/JPEG/GIF/WebP, configurable size and count caps),
   then written as Responses `input_image` or a Claude image block. Invalid
   images get 422 on both runtimes, as on OpenAI. On Claude,
   `--replay-user-messages` turns any image the CLI dropped into a 422.
   `detail` is ignored on Claude and listed in `x-airelays-ignored-parameters`.
3. Tools on Claude: the stub MCP server ships in the package and runs with
   the relay's own interpreter; the relay aborts if the CLI's init event
   lists any tool other than the stubs, strips the `mcp__relay__` prefix,
   returns OpenAI `tool_calls` with `finish_reason: "tool_calls"`, and takes
   usage from the streamed message events. Prior calls and results are
   rendered into the transcript. Rejected with 422: `tool_choice` required or
   named, `parallel_tool_calls: false`, `strict: true`, custom tools.
   The run ends with the stream-json `interrupt` control request at
   `stop_reason: "tool_use"`, then stdin closes (a SIGKILL leaves the CLI's
   session record behind until a later start cleans it).

## Tool-call safety evidence (2026-10-15, CLI 2.1.300)

What guarantees that nothing runs, and what is only defence in depth:

| Barrier | Kind | Evidence |
|---|---|---|
| No executable tool exists: `--tools ""` leaves only the stub's tools (init event lists exactly them) | guarantee | every run's init event |
| The stub only lists tools; `tools/call` answers "not executed" | guarantee | code (about 20 lines); the CLI did reach it in some runs, with no effect |
| A `PreToolUse` hook (via `--settings`) denies every call | defence in depth | without a sandbox: 0 stub calls in run-to-end mode, both attempts in `permission_denials`; it runs through a shell and fails open when it cannot start |
| `interrupt` at the call boundary | defence in depth, tidiness | the call can still reach the stub in the window |

`--permission-prompts none` does not block MCP tool calls. A permission
`deny` rule or `--disallowedTools` hides the tool from the model, so neither
can serve as a barrier.

Battery: 11 attacks (shell, file write, network, sub-agent, injection in a
tool description, app tools named `Bash`/`Write`, transcript injection, web
search, slash and `!` commands, MCP self-configuration) on haiku and sonnet,
each in the production configuration and with only the stub as barrier
(no hook, no interrupt, run to the end), with harmless canary files as the
target:
- Unsandboxed, 26 runs: no canary, no call to a non-app tool, no web search,
  no file changed in the working folder or sandbox; production runs reached
  the stub 0 times.
- Under a macOS `sandbox-exec` profile denying every program launch and file
  write outside an allow-list, 32 runs including a control call that must
  succeed: the CLI worked normally and nothing outside the allow-list was
  possible.

What the CLI itself starts on every request, tools or not: a copy of
itself, `git` (hooks disabled; checks the empty working folder for
`.claude/settings.local.json`), `ps` (its own start time), `ioreg` (machine
id) and, on macOS, `/usr/bin/security` (its sign-in). It also updates
`.claude.json` in the profile and keeps a session record with a local
messaging socket while running.

4. `/v1/responses` on Claude models: the same Claude runner behind a
   Responses translation (input items to the transcript, output as Responses
   objects and streamed events). Server-side tools (web search, file search)
   stay unsupported on Claude.
5. Files on Claude models: `file_id` and inline files through the same
   structured input as images: images as image blocks, PDFs as document
   blocks (verified live 2026-10-15: a base64 PDF and a plain-text document
   block were both read and replayed as `document` blocks), text files inlined.
6. Conversations on Claude models: the relay already stores the history, so
   `conversation` works as on GPT models (history prepended to the
   transcript).

## Validation

- Fake-CLI tests for every trap above; opt-in live tests with 8x8 images and
  a tool round trip; an adversarial audit; local redeploy before release.
