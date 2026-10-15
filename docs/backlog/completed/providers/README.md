# Provider runtimes backlog track

## Status

Completed

## Purpose

These completed items record the initial provider-runtime expansion that turned AIRelays into a provider-scoped local relay with a first experimental Claude adapter.

## Completed Items

- [0001_provider_runtime_registry_for_subscription_relays.md](0001_provider_runtime_registry_for_subscription_relays.md): introduced provider-scoped routing, model metadata, and readiness reporting.
- [0010_experimental_claude_subscription_cli_adapter.md](0010_experimental_claude_subscription_cli_adapter.md): added the first local-only experimental Claude text runtime with explicit limits and guardrails.
- [0011_claude_runtime_mainline_graduation.md](0011_claude_runtime_mainline_graduation.md): sampling-parameter adaptation, merge to mainline, label removal, personal-use disclaimer, and the 0.4.0 release.

## Related Material

- [ADR 0002](../../../adr/0002-chatgpt-subscription-backend-compatibility-boundary.md)
- [ADR 0003](../../../adr/0003-local-config-and-default-deny-relay-security.md)
- [ADR 0004](../../../adr/0004-provider-runtime-boundary-and-experimental-local-adapters.md)
- [Architecture](../../../architecture.md)
- [Configuration](../../../configuration.md)
- [Disclaimer](../../../disclaimer.md)
- [0026_chat_completions_usage_contract.md](0026_chat_completions_usage_contract.md): chat and completions report OpenAI's CompletionUsage on both runtimes, with spec-shaped streamed usage
- [0027_images_and_claude_tools.md](0027_images_and_claude_tools.md): Claude models gain images, files, conversations, `/v1/responses` and tool calls (returned to the client, never run)
