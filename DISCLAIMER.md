# Disclaimer

AIRelays is an independent third-party project. It is not affiliated with, endorsed by, or sponsored by any provider.

Provider and product names are used only to describe compatibility targets, client shapes, and upstream protocol behavior.

AIRelays is designed for a single user operating a local relay for personal convenience. It is not presented as a shared, pooled, multi-user, or resale service.

Support for multiple accounts exists so one person can use their own subscriptions from one relay. It is not a mechanism for sharing, pooling, or reselling access among multiple users, and upstream terms and usage limits continue to apply to each account individually.

The Claude runtime is loopback-only and driven by the local Claude CLI. Relay bearer authentication is enabled by default and can be disabled explicitly. These safeguards do not establish provider authorization for AIRelays.

You are responsible for reviewing and complying with the terms, policies, and usage limits that apply to any upstream account or subscription you use with AIRelays.

## Upstream Terms And Personal Use

The OpenAI runtime implements a compatible subscription OAuth login and token-refresh flow and calls the ChatGPT Codex backend directly; it does not use the public OpenAI Platform API. The Claude runtime invokes the official local `claude` CLI, can store a user-supplied OAuth token, and can read local Claude credentials to query an undocumented subscription-usage endpoint. Use of an official CLI or a successful login does not establish authorization for every part of this relay.

Your agreement with each provider determines which access methods, automation, credential handling, and workloads are permitted. Personal, local, or noncommercial use does not by itself establish permission, and AIRelays does not certify compliance. Restrictions are not a blanket distinction between commercial and noncommercial work: permission for a provider's own CLI or SDK does not necessarily extend to direct backend access or third-party credential handling.

Use only accounts you own and access methods and workloads your provider permits. Do not use AIRelays to share or resell subscription access, bypass protective measures, or evade usage limits. Support for multiple accounts is not permission to circumvent restrictions.

Provider policies, authentication requirements, quotas, and backend behavior may change. Use that a provider considers unauthorized may result in suspended or terminated access; compatibility may also stop working without notice. AIRelays' software license grants no rights to upstream services, and this disclaimer does not override provider terms.

Review the official terms yourself and re-check them periodically; they change:

- [Anthropic Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
- [Claude Code legal and compliance — authentication and credential use](https://code.claude.com/docs/en/legal-and-compliance)
- [Claude plans and the Agent SDK / `claude -p`](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)
- [OpenAI Terms of Use](https://openai.com/terms)
- [OpenAI European Terms of Use](https://openai.com/policies/eu-terms-of-use/)
- [OpenAI Usage Policies](https://openai.com/policies/usage-policies/)

None of this is legal advice. You remain responsible for how you use your accounts.
