# Disclaimer

AIRelays is an independent third-party project. It is not affiliated with, endorsed by, or sponsored by any provider.

Provider and product names are used only to describe compatibility targets, client shapes, and upstream protocol behavior.

AIRelays is designed for a single user operating a local relay for personal convenience. It is not presented as a shared, pooled, multi-user, or resale service.

The experimental Claude runtime is local-only, bearer-auth-required, loopback-only, and driven by the local Claude CLI. AIRelays does not present it as a sanctioned provider integration path or a shared gateway feature.

You are responsible for reviewing and complying with the terms, policies, and usage limits that apply to any upstream account or subscription you use with AIRelays.

## Upstream Terms And Personal Use

AIRelays drives provider-owned tooling with credentials you already hold: the OpenAI runtime uses your own ChatGPT subscription sign-in, and the Claude runtime shells out to the official local `claude` CLI under its existing login. AIRelays does not reimplement a provider's authentication protocol and does not offer provider sign-in to anyone else.

Whether a given use of your subscription is permitted is defined by your agreement with the provider, not by AIRelays. Both providers currently frame subscription access around ordinary, individual use by the account holder. Anthropic's published policy explicitly does not permit routing requests through personal-plan credentials on behalf of other users — which matches this project's single-user design: the moment anyone else's requests flow through your relay, you are outside both the provider's terms and AIRelays' intended use.

Review the official terms yourself and re-check them periodically; they change:

- [Anthropic Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
- [Claude Code legal and compliance — authentication and credential use](https://code.claude.com/docs/en/legal-and-compliance)
- [Claude plans and the Agent SDK / `claude -p`](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- [OpenAI Terms of Use](https://openai.com/terms)
- [OpenAI Usage Policies](https://openai.com/policies/usage-policies/)

None of this is legal advice. You remain responsible for how you use your accounts.
