# FAQ

## Does AIRelays use my OpenAI platform API key?

No. The OpenAI runtime uses an AIRelays-owned ChatGPT subscription login.

## What token do I give my client?

Use the AIRelays relay token from `airelays init`, `airelays token show`, or `airelays token rotate`.

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

Yes, in a constrained text-only form.

- discovered Claude aliases and concrete model ids, plus configured overrides
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

## Why do token-limit parameters return `422` on the OpenAI runtime?

The verified OpenAI subscription backend does not currently accept those fields on AIRelays’ OpenAI-shaped text-generation routes, so AIRelays rejects them explicitly.

## Why did I get `401` and then `429`?

The relay token was missing or wrong, and repeated bad attempts triggered the temporary IP block.
