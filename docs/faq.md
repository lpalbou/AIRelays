# FAQ

## Does AIRelays use my OpenAI platform API key?

No. The OpenAI runtime uses an AIRelays-owned ChatGPT subscription login.

## What token do I give my client?

Use the AIRelays relay token from `airelays init`, `airelays token show`, or `airelays token rotate`.

## Where does AIRelays keep my sign-ins?

In owner-only files in the data directory (`~/.airelays` by default), the same
way on macOS, Linux and Windows. AIRelays does not use the OS keychain. See
[Credential Storage](security.md#credential-storage). If you used 0.16.0 or
earlier, OpenAI accounts that version kept in the keychain need one new
sign-in; `airelays doctor` lists them.

## Is AIRelays affiliated with any provider?

No. AIRelays is an independent third-party project.

## How are requests spread across my OpenAI accounts?

With more than one enrolled account, the relay routes each request to the
account with the most remaining quota in its longest usage window — the
weekly budget — among those that serve the requested model (the default,
`balance = "balanced"`), so consumption equalizes as a percentage of each
plan's own capacity — plans of very different sizes deplete
proportionally. Which windows a plan reports is upstream policy (some
plans report only a weekly window), so the relay ranks windows by
duration rather than trusting their position in the payload. Usage is
probed at launch and refreshed in the background; an account that reaches
a limit on any window is benched until that window resets. Alternatives:
`balance = "round_robin"` for strictly equal request counts,
`balance = "ordered"` to drain the first account before the next. See
[Configuration](configuration.md).

## Can I disable relay auth?

Yes. Open local relay mode applies to all enabled providers, including the Claude runtime.

```bash
airelays init --no-auth
airelays serve --no-auth --port 8080
```

## Does AIRelays support Claude?

Yes, through the same OpenAI-compatible endpoint as GPT models.

- discovered Claude aliases and concrete model ids, plus configured overrides
- local `claude` CLI only
- `chat.completions`, `completions` and `responses`
- images, PDFs and text files as input; local files and conversations
- function tool calls returned to your application, which runs them;
  AIRelays never runs a tool (see [docs/api.md](api.md#tool-calls-on-claude-models))
- follows the relay's protected or open local authentication mode
- loopback-only

## How do I log in to the Claude runtime?

Use the local Claude CLI through AIRelays:

- browser login: `airelays claude login`; repeat to add another of your accounts
- headless login: `claude setup-token` on a browser-equipped machine, then
  `airelays claude set-token` on the relay machine (stores the token in a
  0600 file that survives service managers and reboots). It appears as the
  account `default` and is inference-only, without a usage bar

List accounts with `airelays claude accounts`, renew one with
`airelays claude login --replace ACCOUNT`, and sign out one with
`airelays claude logout ACCOUNT`. The desktop offers **Sign in** / **Add
account** ("In a browser" or "With a token") and per-account sign-out. Each
browser account is its own AIRelays-owned profile; signing out one leaves the
others intact. `airelays claude logout default` only deletes the stored token.

## Does AIRelays use my Claude Code sign-in?

No. AIRelays never uses your personal Claude Code sign-in (`~/.claude`), an
inherited `CLAUDE_CONFIG_DIR`, or an exported `CLAUDE_CODE_OAUTH_TOKEN`, and it
never signs Claude Code out. Switching or signing out Claude Code does not
affect the relay. If you upgraded from 0.15.0 or earlier and Claude only
worked through one of those, connect it once with `airelays claude login` or
`airelays claude set-token`. AIRelays cannot copy the personal sign-in
because Claude Code refresh tokens rotate. Profiles already enrolled keep
working.

Claude requests balance across eligible accounts with capacity. The relay
uses fresh weekly usage when available, rotates when usage is unknown, and
can fail over before response bytes reach the client. Subscription tokens
remain subscription tokens; adding accounts does not enable API-key auth.

## Can I see my Claude subscription usage?

Yes: `GET /v1/subscription/status?provider=claude` returns the 5-hour and
weekly windows in the same normalized shape as OpenAI usage. The desktop
app shows per-account usage in the OpenAI and Anthropic cards. Use **?** beside
an account name for details. Readings come from each profile's Claude Code
(`get_usage`, verified with 2.1.292+) every five minutes and are marked stale
with a reason when Claude returns no live data. Setup-token accounts report
no usage. See [Subscription Status](subscription-status.md).

## Is using my subscription through AIRelays allowed by the providers?

AIRelays does not certify that your use is permitted. Personal, local, or
noncommercial use and a successful login do not by themselves establish
authorization. Your provider's terms govern access methods, automation,
credential handling, and workloads; permission for its own CLI or SDK does
not necessarily cover every part of AIRelays. Unauthorized use may lead to
account suspension or termination, and compatibility may change without
notice. See the [disclaimer](disclaimer.md) for the access mechanisms used
by each runtime and official terms to review.

## How do I control reasoning depth?

Set `reasoning_effort` in your request to one of the model's supported
modes. Every model's modes and default are published in `/v1/models`
under `airelays.reasoning`, shown in the desktop Models tab, and listed
by `airelays models`. Supported modes vary by model and are read from
provider catalogs when available, including `max` and `ultra` where
reported. Omitted effort uses the provider's default; Claude can use an
adaptive default. See
[API notes](api.md) for details.

## Which model is behind a Claude alias?

The Models tab and `airelays models` show the concrete model reported by
the installed Claude CLI. For example, a CLI can resolve `claude:fable`
to `claude-fable-5-1`. The alias follows CLI updates and account settings;
the discovered concrete id pins that version. Use Refresh to query the
CLI again. A `configured` label means the catalog did not confirm that
selector, so AIRelays does not claim a concrete resolution for it.

## Does AIRelays support Gemini?

No.

## Why does the dashboard disappear when I click another window?

The dashboard hides when it loses focus; the relay keeps running and unsaved
edits remain in the window. On macOS and Windows, left-click the tray icon to
reopen it, or right-click for the menu. **Open Dashboard** is available in the
tray menu on all platforms. In Overview, **Connect Your App** contains both
connection details and the authentication/network access controls.

## Are output-token limits enforced?

Yes, on both account types. `max_completion_tokens`, `max_tokens` (chat and
completions) and `max_output_tokens` (`/v1/responses`) are enforced by the
providers themselves: GPT models apply the limit upstream (the minimum is 16),
and on Claude models the run stops when the model reaches the limit. An answer
cut by the limit reports `finish_reason: "length"` (`status: "incomplete"` on
`/v1/responses`), as on OpenAI. See [Capabilities](capabilities.md).

## Why did I get `401` and then `429`?

The relay token was missing or wrong, and repeated bad attempts triggered the temporary IP block.
