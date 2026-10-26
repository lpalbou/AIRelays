# Provider runtimes backlog track

## Status

One active planned item: 0032.

## Purpose

These items define how AIRelays grows from one subscription-backed runtime into a provider-scoped relay that can expose multiple upstreams without pretending they behave the same.

## Items

- [0032_claude_thinking_off_and_reasoning_token_reporting.md](0032_claude_thinking_off_and_reasoning_token_reporting.md) — thinking off (`reasoning_effort: "none"`) on Claude models and honest reasoning-token reporting.

The initial provider-runtime work lives under [completed/providers/README.md](../../completed/providers/README.md).

## Order

Use the completed track as the implementation record for the initial provider-runtime expansion.

## Related material

- [ADR 0002](../../../adr/0002-chatgpt-subscription-backend-compatibility-boundary.md)
- [ADR 0003](../../../adr/0003-local-config-and-default-deny-relay-security.md)
- [ADR 0004](../../../adr/0004-provider-runtime-boundary-and-experimental-local-adapters.md)
- [Architecture](../../../architecture.md)
- [Configuration](../../../configuration.md)
- [Disclaimer](../../../disclaimer.md)

## Non-goals

- Declaring blanket parity across providers
- Shipping Claude tools, files, images, or structured outputs in the first slice
- Treating experimental local adapters as equivalent to sanctioned API-backed integrations
