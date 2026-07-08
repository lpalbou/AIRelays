# Architecture

## Overview

AIRelays is an OpenAI-shaped edge over the OpenAI subscription runtime.

- The runtime uses the ChatGPT Codex subscription backend.

## Request Flow

1. FastAPI receives an OpenAI-shaped request.
2. Middleware enforces relay auth and local abuse controls.
3. AIRelays resolves the request model id to the provider runtime.
4. The runtime uses shared request/response transforms plus the OpenAI backend adapter.
5. The runtime returns streamed or aggregated output in the matching OpenAI-shaped envelope.
6. AIRelays logs the request, runtime selection, and result.

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

### `airelays.backend`

- OpenAI runtime HTTP calls to the verified ChatGPT backend

### `airelays.providers`

- provider registry
- provider model catalogs
- provider readiness

### `airelays.transforms`

- OpenAI runtime request and response translation

### `airelays.store`

- local files
- local OpenAI conversation state

### `airelays.traffic`

- redacted JSONL logging

## State Model

- supports AIRelays local conversations
- supports local file reuse

## Intentional Boundaries

- no silent truncation
- no fake token budgets
- no reuse of upstream subscription auth as relay-client auth
