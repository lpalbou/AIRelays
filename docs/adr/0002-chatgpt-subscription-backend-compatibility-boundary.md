# 0002: ChatGPT Subscription Backend Compatibility Boundary

- Status: Accepted
- Date: 2026-06-12

## Context

The user requirement is an OpenAI-compatible server that relies on a ChatGPT subscription login, not on a user-supplied OpenAI platform API key. The actual verified backend available through the reused login protocol is the ChatGPT Codex backend, not the general OpenAI platform API.

## Decision

The project will:

- use the ChatGPT Codex backend as the primary upstream
- reuse the upstream login and token refresh protocol without sharing Codex-owned local storage
- expose only the OpenAI-compatible routes that were verified against that upstream
- reconstruct non-stream responses locally because the verified upstream requires streaming
- provide explicit unsupported responses for unverified routes

The project will not:

- claim full parity with every OpenAI platform endpoint
- pretend that platform API scopes are available when they are not
- silently reroute unsupported calls into different semantics

## Consequences

- `/v1/models`, `/v1/responses`, and `/v1/chat/completions` are first-class compatibility targets
- local files and local conversations are part of the compatibility layer because the upstream surface does not provide full OpenAI file/session parity
- some OpenAI endpoints remain intentionally unsupported until they are verified against the subscription backend
