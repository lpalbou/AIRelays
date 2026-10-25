# Documentation

This is the index of the AIRelays documentation. Start with the
[README](../README.md) for the overview and quick start.

- [Getting Started](getting-started.md)
  - one-line and PyPI installation, custom CLI configuration, multi-account sign-in, serving other devices, and verification
- [Desktop App](../desktop/README.md)
  - the cross-platform tray app: Connect card and **Try it** examples, who can connect, Models table, Traffic detail, tray menu, install, updates, and supervision
- [Configuration](configuration.md)
  - config file shape, listener and network scope, environment overrides, provider toggles, and traffic-log retention
- [Security](security.md)
  - relay auth, who can connect (`network_scope`) and internet setup, open local relay mode, Claude guardrails, and desktop update integrity
- [Capabilities](capabilities.md)
  - what works on GPT models and on Claude models, side by side
- [API Notes](api.md)
  - supported routes, model metadata and reference pricing, errors, and provider-scoped limits
- [Architecture](architecture.md)
  - components, the request admission pipeline, request flow, and module boundaries
- [Subscription Status](subscription-status.md)
  - usage reporting for the OpenAI and Claude runtimes
- [FAQ](faq.md)
  - common usage, access, pricing, and provider questions
- [Troubleshooting](troubleshooting.md)
  - desktop install, network access, auth, provider, and route troubleshooting
- [Disclaimer](disclaimer.md)
  - independence and intended use
- [index.md](index.md) and [docs-index.md](docs-index.md)
  - the home page and page list of the published docs site
- [ADR Index](adr/README.md)
  - durable design decisions
- [Agent skill `airelays`](../skills/airelays/SKILL.md)
  - an operating guide that teaches AI agents to run and call the relay
