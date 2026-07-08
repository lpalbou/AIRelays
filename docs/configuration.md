# Configuration

AIRelays resolves settings in this order:

1. CLI flags
2. `AIRELAYS_*` environment variables
3. legacy `OPENAI_ENDPOINT_*` migration variables where supported
4. `~/.config/airelays/config.toml`
5. built-in defaults

## Default Paths

- config: `~/.config/airelays/config.toml`
- data dir: `~/.airelays`
- logs dir: `~/.airelays/logs`
- auth fallback file: `~/.airelays/auth.json`
- relay token file: `~/.airelays/relay-token`

Earlier singular AIRelay paths remain compatible for local upgrades.

## Sample Config

```toml
[server]
host = "127.0.0.1"
port = 8080

[paths]
data_dir = "~/.airelays"
logs_dir = "~/.airelays/logs"

[auth]
storage = "auto"
browser_open = false
login_timeout_seconds = 900

[upstream]
base_url = "https://chatgpt.com/backend-api/codex"
issuer_base_url = "https://auth.openai.com"
client_id = "app_EMoamEEZ73f0CkXaXp7hrann"
client_version = "0.124.0"
request_timeout_seconds = 120.0

[security]
require_bearer_auth = true
bearer_token_file = "~/.airelays/relay-token"
auto_generate_bearer_token = false
rate_limit_per_minute = 120
rate_limit_burst = 40
concurrent_requests_per_ip = 8
failed_auth_window_seconds = 300
failed_auth_max_attempts = 8
failed_auth_block_seconds = 900
trust_x_forwarded_for = false

[uploads]
max_upload_bytes = 33554432
max_total_upload_bytes = 268435456

[logging]
# Opt-in: log every raw upstream SSE line. Hundreds of records per
# streamed response (~50x log growth under load); summary records
# (request, usage, response, errors) are always logged regardless.
stream_lines = false

[providers.openai]
enabled = true
models_cache_ttl_seconds = 300.0
```

## Important Environment Variables

- `AIRELAYS_CONFIG`
- `AIRELAYS_HOST`
- `AIRELAYS_PORT`
- `AIRELAYS_DATA_DIR`
- `AIRELAYS_LOGS_DIR`
- `AIRELAYS_AUTH_STORAGE`
- `AIRELAYS_BROWSER_OPEN`
- `AIRELAYS_LOGIN_TIMEOUT_SECONDS`
- `AIRELAYS_UPSTREAM_BASE_URL`
- `AIRELAYS_ISSUER_BASE_URL`
- `AIRELAYS_CLIENT_ID`
- `AIRELAYS_CLIENT_VERSION`
- `AIRELAYS_REQUEST_TIMEOUT_SECONDS`
- `AIRELAYS_REQUIRE_BEARER_AUTH`
- `AIRELAYS_BEARER_TOKEN`
- `AIRELAYS_BEARER_TOKEN_FILE`
- `AIRELAYS_LOG_STREAM_LINES`
- `AIRELAYS_ENABLE_OPENAI`
- `AIRELAYS_OPENAI_MODELS_CACHE_TTL_SECONDS`

## Relay Token Inputs

AIRelays resolves the relay token in this order:

1. `AIRELAYS_BEARER_TOKEN`
2. the configured `bearer_token_file`

Override examples:

```bash
AIRELAYS_BEARER_TOKEN='YOUR_AIRELAYS_TOKEN' airelays serve --port 8080
```

```bash
airelays serve --bearer-token-file /path/to/relay-token --port 8080
```

## Provider Notes

OpenAI runtime:

- enabled by default
- uses AIRelays-owned auth storage
- `airelays login` manages its subscription session
- caches successful upstream model-list responses for `models_cache_ttl_seconds` seconds
  by default
- the model-list cache is process-local, in-memory, and disabled when
  `models_cache_ttl_seconds = 0`
- cache state is visible under `providers.openai.models_cache` in
  `GET /v1/relay/status`

`AIRELAYS_MODELS_CACHE_TTL_SECONDS` remains accepted as a shorter alias for
`AIRELAYS_OPENAI_MODELS_CACHE_TTL_SECONDS`.
