# Architecture

## Overview

AIRelays is a thin compatibility layer between OpenAI-shaped client requests and the ChatGPT Codex subscription backend. The route envelopes are intentionally OpenAI-shaped, but some parameter surfaces are narrower because the subscription backend accepts a slightly different contract.

Request flow:

1. FastAPI receives an OpenAI-compatible request.
2. Endpoint middleware enforces bearer auth and local abuse controls for protected routes.
3. The compatibility layer validates and translates the request into the subscription backend format.
4. Upstream auth is loaded from AIRelays-owned storage under the AIRelays data directory or AIRelays keyring namespace.
5. Inference requests are sent to `chatgpt.com/backend-api/codex`, while subscription-status requests are sent to `chatgpt.com/backend-api/wham/usage`.
6. Upstream SSE events are either streamed through directly or aggregated into a final JSON response.
7. Every ingress and egress step is logged to hourly JSONL files.

## Components

### `airelays.config`

- resolves config from CLI flags, env, config file, and defaults
- owns local paths and relay-security defaults
- resolves the relay bearer token from explicit override or token file
- supports explicit token generation through `airelays init` and optional startup auto-generation when configured

### `airelays.security`

- enforces route protection on `/v1/*` and `/no-tools/v1/*`
- validates the relay bearer token
- applies per-IP rate limits and temporary blocks after repeated bad tokens
- emits security events to the normal traffic log

### `airelays.auth`

- loads upstream ChatGPT subscription auth from AIRelays-owned file, keyring, or auto mode
- refreshes tokens
- supports browser and device login
- keeps login protocol compatibility without sharing Codex-owned storage

### `airelays.backend`

- calls the verified subscription backend routes for inference, model listing, and usage introspection
- normalizes streamed event handling
- reconstructs non-stream responses from SSE output items
- logs usage summaries from `response.completed`

### `airelays.transforms`

- maps OpenAI-compatible requests into the upstream request shape
- maps response payloads back into `chat.completions`
- expands local uploaded images and text files
- rejects unverified fields explicitly

### `airelays.store`

- stores uploaded files locally with explicit per-file and total-byte ceilings enforced at ingress
- stores local conversation metadata and latest upstream response ids
- provides the opt-in stateful session layer

### `airelays.traffic`

- writes redacted JSONL logs
- stores text bodies directly
- stores binary payload summaries explicitly with SHA-256 digests

## Session Model

Stateless requests omit `conversation`.

Stateful requests create a local conversation and pass that local id back on later `responses` or `chat.completions` requests. The server reuses that id as the upstream `session_id` header and tracks the latest response id locally.

## Security Model

- upstream provider login is separate from relay-client authorization
- the relay bearer token is local-only and is used by callers of AIRelays
- route protection is middleware-level so local-only routes such as files and conversations are covered too
- current rate limiting is in-memory and single-process by design
- public HTTP is limited to the landing page and a minimal `/healthz`; detailed relay diagnostics live behind relay auth at `/v1/relay/status`

## Intentional Boundaries

- no silent truncation
- no fake token budgets
- no silent fallback for unsupported endpoints
- no claim of parity beyond routes verified against the subscription backend
- no reuse of upstream subscription auth as relay-client auth
