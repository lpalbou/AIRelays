# FAQ

## Does AIRelays use my OpenAI platform API key?

No. The OpenAI runtime uses an AIRelays-owned ChatGPT subscription login.

## What token do I give my client?

Use the AIRelays relay token from `airelays init`, `airelays token show`, or `airelays token rotate`.

## Is AIRelays affiliated with any provider?

No. AIRelays is an independent third-party project.

## How are requests spread across my OpenAI accounts?

With more than one enrolled account, the relay routes each request to the
account with the most remaining short-window quota among those that serve
the requested model (the default, `balance = "balanced"`), so consumption
equalizes as a percentage of each plan's own capacity — plans of very
different sizes deplete proportionally. Usage is probed at launch and
refreshed in the background; an account that reaches its limit is benched
until its window resets. Alternatives: `balance = "round_robin"` for
strictly equal request counts, `balance = "ordered"` to drain the first
account before the next. See [Configuration](configuration.md).

## Can I disable relay auth?

Yes. Open local relay mode applies to all enabled providers, including the Claude runtime.

```bash
airelays init --no-auth
airelays serve --no-auth --port 8080
```

## Does AIRelays support Claude?

Yes, in a constrained text-only form.

- explicit `claude:*` models
- local `claude` CLI only
- text `chat.completions`
- text `completions`
- bearer-auth-required
- loopback-only
- stateless

## How do I log in to the Claude runtime?

Use the local Claude CLI:

- browser login: `claude auth login --claudeai`
- headless login: `claude setup-token` on a browser-equipped machine, then
  `airelays claude set-token` on the relay machine (stores the token in a
  0600 file that survives service managers and reboots)

Sign out with `airelays claude logout`. Note that this signs the `claude`
CLI out machine-wide, so other tools using it (including Claude Code) are
signed out too. The desktop app offers the same sign-in and sign-out flows
from the Accounts card.

## Can I see my Claude subscription usage?

Yes: `GET /v1/subscription/status?provider=claude` returns the 5-hour and
weekly windows in the same normalized shape as OpenAI usage. The desktop
app shows both providers' usage bars in the Accounts card. See
[Subscription Status](subscription-status.md).

## Is using my subscription through AIRelays allowed by the providers?

That is defined by your agreement with each provider, and it is your
responsibility to review it. Both providers currently frame subscription
access around ordinary, individual use by the account holder. AIRelays is
built for exactly that shape — one person, local relay, provider-owned
tooling and sign-ins — and is not a mechanism for sharing access with
anyone else. See the [disclaimer](disclaimer.md) for the official terms
links (Anthropic consumer terms, Claude Code authentication policy, OpenAI
terms and usage policies); re-check them periodically, they change.

## How do I control reasoning depth?

Set `reasoning_effort` in your request to one of the model's supported
modes. Every model's modes and default are published in `/v1/models`
under `airelays.reasoning`, shown in the desktop Models tab, and listed
by `airelays models`. OpenAI models accept `none`, `low`, `medium`,
`high`, `xhigh` (omitted means `none`, lower than the official apps'
`medium`); Claude models accept `low`, `medium`, `high`, `xhigh`, `max`
(omitted means the model's adaptive default). See
[API notes](api.md) for details.

## Does AIRelays support Gemini?

No.

## Why do token-limit parameters return `422` on the OpenAI runtime?

The verified OpenAI subscription backend does not currently accept those fields on AIRelays’ OpenAI-shaped text-generation routes, so AIRelays rejects them explicitly.

## Why did I get `401` and then `429`?

The relay token was missing or wrong, and repeated bad attempts triggered the temporary IP block.
