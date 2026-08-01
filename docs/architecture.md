# Architecture

## Overview

AIRelays is an OpenAI-shaped edge over provider-specific local runtimes.

- The default runtime uses the ChatGPT Codex subscription backend and
  balances requests across every enrolled OpenAI account with capacity.
- The Claude runtime uses isolated local `claude -p` subprocesses.

```mermaid
flowchart LR
    Client["OpenAI-compatible client"] -->|"bearer token"| Edge

    subgraph Relay["AIRelays (local)"]
        Edge["FastAPI edge\nauth · rate limits · traffic log"] --> Registry["Provider registry\nmodel id → runtime"]
        Registry -->|"other model ids"| Retry["Retry layer\nexponential backoff\npre-first-byte only"]
        Retry --> Pool["OpenAI account pool\nbalanced selection · benching · failover"]
        Registry -->|"claude:* model ids"| ClaudeRT["Claude runtime\nvalidation · text transcript"]
        Pool --> B1["Backend adapter\naccount 1"]
        Pool --> B2["Backend adapter\naccount N"]
    end

    B1 --> Upstream["ChatGPT subscription backend"]
    B2 --> Upstream
    ClaudeRT --> CLI["local claude CLI\n(claude -p subprocess)"]
    CLI --> Anthropic["Claude subscription"]
```

## Request Flow

1. FastAPI receives an OpenAI-shaped request.
2. Middleware enforces relay auth and local abuse controls.
3. AIRelays resolves the request model id to a provider runtime.
4. Claude-specific validation and invocation stay inside the Claude runtime, while the OpenAI runtime currently uses shared request/response transforms plus the OpenAI backend adapter.
5. On the OpenAI runtime, the account pool picks the account: conversation affinity first, then — among accounts with capacity that serve the requested model — the one with the most remaining quota in its longest usage window (`balance = "balanced"`, the default), strict rotation (`"round_robin"`), or the first such account (`"ordered"`). Which usage windows an account reports is plan-dependent, so the pool ranks windows by duration and balances on the longest (weekly) budget — the scarce one — while short-window exhaustion is handled by benching and failover. Accounts already benched (a usage probe or an earlier request showed their window is spent) are skipped outright, so requests route straight to accounts with capacity. Account-scoped failures (usage limits, in-stream failure events, dead credentials, transport errors) bench the account until it recovers and fail over to the next one — only before any content byte reaches the client; on streams, events that precede content are buffered so this guarantee holds.
6. If every account fails, the retry layer waits an exponential backoff (`retry_attempts`, default 3 retries at 5s/20s/60s) and re-runs the whole pool pass — again only while no response byte has reached the client. Quota errors whose reset lies beyond the backoff budget return immediately.
7. The selected runtime returns streamed or aggregated output in the matching OpenAI-shaped envelope; failures return OpenAI-shaped error JSON with the upstream's own reason, and failures after streaming started surface in-band.
8. AIRelays logs the request, runtime selection, account selection, retries, and result.

## Account Pool Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Probing: relay starts (2+ accounts)
    Probing --> Serving: usage + model catalogs fetched
    Serving --> Benched: usage limit / dead credentials / transport failure
    Benched --> Serving: window reset reached, or a fresh usage probe shows capacity
    note right of Benched
        Releases are evidence-gated:
        only a usage snapshot newer than
        the bench can lift it early.
    end note
```

At launch, a multi-account pool probes each account's usage and model
catalog in the background, so accounts already at their limit are benched
and model-aware balancing works from the first request.

## Main Components

### `airelays.config`

- config resolution
- local paths
- relay token state
- provider toggles and runtime guardrails

### `airelays.security`

- relay bearer auth
- per-IP limits
- temporary bad-token blocks

### `airelays.auth`

- AIRelays-owned OpenAI subscription auth
- browser and device login
- token refresh

### `airelays.accounts`

- multi-account discovery and storage slots
- balanced account selection (capacity-aware default; round-robin and ordered opt-ins)
- usage-limit benching with evidence-gated release
- cached, single-flighted usage probes with a background refresher
- account failover and launch-time capacity/model warm-up

### `airelays.backend`

- OpenAI runtime HTTP calls to the verified ChatGPT backend
- structured errors for upstream HTTP, transport, and in-stream failures

### `airelays.retry`

- automatic retry with exponential backoff for failed OpenAI upstream calls
- runs only while no response byte has reached the client
- skips retries a quota reset horizon proves futile; stops for disconnected clients

### `airelays.providers`

- provider registry
- provider model catalogs
- provider readiness
- Claude runtime

### `airelays.transforms`

- OpenAI runtime request and response translation

### `airelays.store`

- local files
- local OpenAI conversation state

### `airelays.traffic`

- redacted JSONL logging

## State Model

OpenAI runtime:

- supports AIRelays local conversations
- supports local file reuse

Claude runtime:

- stateless only
- no local conversation reuse
- no file reuse

## Intentional Boundaries

- no silent fallback across providers
- no blanket parity claim across providers
- no silent truncation
- no fake token budgets
- no reuse of upstream subscription auth as relay-client auth
