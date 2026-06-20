# Configuration

AIRelays resolves settings from these sources, highest precedence first:

1. explicit CLI flags such as `--config`, `--port`, `--auth-storage`
2. `AIRELAYS_*` environment variables
3. legacy `OPENAI_ENDPOINT_*` environment variables where supported as a migration fallback
4. `~/.config/airelays/config.toml`
5. built-in defaults

If an earlier AIRelay config already exists at `~/.config/airelay/config.toml`, AIRelays can continue using that path for compatibility.

## Default Paths

- config: `~/.config/airelays/config.toml`
- data dir: `~/.airelays`
- upstream auth fallback file: `~/.airelays/auth.json`
- logs dir: `~/.airelays/logs`
- relay token file: `~/.airelays/relay-token`

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
```

## CLI Overrides

These flags override config-file values:

- `--config`
- `--data-dir`
- `--logs-dir`
- `--auth-storage`
- `--bearer-token-file`
- `serve --host`
- `serve --port`
- `serve --no-auth`
- `init --no-auth`

## Relay Token Inputs

AIRelays resolves the relay token for server startup in this order:

1. `AIRELAYS_BEARER_TOKEN`
2. the configured `bearer_token_file`

Default token file:

```text
~/.airelays/relay-token
```

Examples:

```bash
AIRELAYS_BEARER_TOKEN='YOUR_AIRELAYS_TOKEN' airelays serve --port 8080
```

```bash
airelays serve --bearer-token-file /path/to/relay-token --port 8080
```

To disable relay auth for the current process:

```bash
airelays serve --no-auth --port 8080
```

To persist that mode through config or environment:

```bash
airelays init --no-auth
```

```bash
AIRELAYS_REQUIRE_BEARER_AUTH=false airelays serve --port 8080
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
- `AIRELAYS_AUTO_GENERATE_BEARER_TOKEN`
- `AIRELAYS_RATE_LIMIT_PER_MINUTE`
- `AIRELAYS_RATE_LIMIT_BURST`
- `AIRELAYS_CONCURRENT_REQUESTS_PER_IP`
- `AIRELAYS_FAILED_AUTH_WINDOW_SECONDS`
- `AIRELAYS_FAILED_AUTH_MAX_ATTEMPTS`
- `AIRELAYS_FAILED_AUTH_BLOCK_SECONDS`
- `AIRELAYS_TRUST_X_FORWARDED_FOR`
- `AIRELAYS_MAX_UPLOAD_BYTES`
- `AIRELAYS_MAX_TOTAL_UPLOAD_BYTES`

## Notes

- `airelays init` is the normal way to create the relay token. `airelays serve` only auto-generates a token when you explicitly enable `auto_generate_bearer_token`.
- `airelays init --no-auth` writes config with bearer auth disabled and skips relay-token creation.
- `AIRELAYS_BEARER_TOKEN` overrides the token file for the current process.
- `auth.storage = "auto"` prefers the AIRelays keyring namespace and falls back to `~/.airelays/auth.json` when keyring access is unavailable.
- `auth.storage = "auto"` also recognizes earlier `AIRelay Auth` keychain entries and migrates them into the AIRelays-owned namespace when they are encountered.
- `AIRELAYS_TRUST_X_FORWARDED_FOR` should stay `false` unless you intentionally run behind a trusted proxy.
- The listener remains loopback-only by default. Change `host` explicitly if you need broader access.

## Legacy Compatibility

AIRelays keeps compatibility with earlier singular AIRelay naming where it matters for local upgrades:

- legacy config path: `~/.config/airelay/config.toml`
- legacy data dir: `~/.airelay`
- legacy keychain service name: `AIRelay Auth`

If those paths or entries already exist, AIRelays can continue using or importing them instead of forcing a fresh login or a manual migration.
