# Configuration

AIRelays resolves settings in this order:

1. CLI flags
2. `AIRELAYS_*` environment variables
3. legacy `OPENAI_ENDPOINT_*` migration variables where supported
4. `~/.config/airelays/config.toml`
5. built-in defaults

Traffic-log retention is live configuration: a saved policy in the selected
log directory takes precedence over its `[logging]` defaults. See
[Traffic log retention](#traffic-log-retention) below.

## Traffic log retention

Traffic logs rotate hourly and at a size limit. Cleanup runs on startup,
every 60 seconds while the relay is running (including when idle), on rotation,
and when applying a policy. Both age and size limits apply; whichever requires
removing a file first wins. Defaults are deliberately bounded on upgrades too.

| Parameter | Default | Meaning |
| --- | --- | --- |
| `retention_days` | `7` | Maximum age since a file's last write; `30` means 30 days, `0` disables only the age limit. Range: 0–36500. |
| `max_total_mb` | `1024` | Total managed traffic-log budget in MiB (1 GiB by default). Range: 1–1048576. |
| `max_file_mb` | `50` | Rotate before the next record would exceed this size in MiB. Range: 1–10240; must not exceed the total budget. |

Retention is an upper limit, not a promise of seven days of history: under load,
the disk budget removes older files sooner. Cleanup reserves room for the active
file, so usage can be below the configured budget by up to a chunk plus the last
whole file removed. Files are deleted whole, oldest modification time first.
The budget counts file contents; filesystem allocation and metadata overhead
are not included in `usage_bytes`.

From the CLI (also works with the relay stopped):

```bash
airelays logs                         # policy and current usage; no cleanup
airelays logs --retention-days 30      # keep up to a month, save and apply
airelays logs --retention-days 7 --max-total-mb 1024 --max-file-mb 50
airelays logs --json                  # machine-readable policy, usage, errors
airelays logs --config /path/to/config.toml --retention-days 14
```

The selected `--config`, `--data-dir`, and `--logs-dir` determine which log
directory is managed. Use the same directory as the running relay.

In the cross-platform tray app, open **Settings → Traffic log retention**.
Choose **1 week**, **1 month (30 days)**, or a custom duration and disk limit,
then **Apply log retention**. The relay must be running for the tray controls;
the CLI can configure it offline. This section applies independently of the
other settings' Save & Restart workflow. It shows disk usage and cleanup errors.

The [HTTP API](api.md#traffic-log-retention-api) exposes the same policy.
API/tray updates apply immediately. Other relay processes sharing the directory
pick up the saved policy on their next write or within 60 seconds while idle.

Policy changes are saved atomically to `<logs_dir>/.retention.json`. That file
overrides the three TOML retention defaults and survives restarts, CLI sessions,
and the tray's regeneration of `config.toml`. Change it through the API or CLI;
editing the TOML defaults after saving a policy does not override that policy.
The `stream_lines` option remains a separate startup setting.

Cleanup includes existing `YYYY/MM/DD-HH.log` files and new size-rotated
`YYYY/MM/DD-HH.<uuid>.log` files. It does not manage console/stdout logs, uploads,
conversations, unrelated files, symlinks, or hard links. Use an app-owned local
log directory; every writer sharing it must run a retention-aware AIRelays
version. External writers or an older relay can defeat the disk limit.

A record larger than `max_file_mb` becomes an explicit `log_record_omitted`
JSON record with its request ID, phase, byte count and SHA-256; the payload is
omitted from the log without altering the actual request or response. Cleanup
and I/O failures are reported on stderr and through `last_error`; traffic
logging pauses on cleanup failure and retries, rather than growing past the
budget or failing client requests. Deletion is permanent: archive any logs you
need before first starting the upgraded relay or reducing a policy.

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
client_version = "auto"
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
retention_days = 7
max_total_mb = 1024
max_file_mb = 50

[providers.openai]
enabled = true
models_cache_ttl_seconds = 300.0
# Multi-account routing: "balanced" (default) routes to the account with
# the most remaining weekly quota so consumption equalizes as a
# percentage of each plan's capacity; "round_robin" sends equal request
# counts; "ordered" drains the first account first.
balance = "balanced"
# Extra model ids to advertise in /v1/models beyond the upstream catalog,
# for operator-confirmed models omitted from automatic discovery.
extra_models = []
# Fallback bench duration (seconds) when a limited account's reset time is
# unknown; upstream-reported reset times are used when available.
account_cooldown_seconds = 300
# Automatic retry for failed upstream LLM calls. Each retry re-runs the
# full account-pool failover pass, then waits the next backoff delay.
# 0 disables. A schedule shorter than the attempt count repeats its last
# delay. Retries only happen before any response byte reached the client
# (non-streaming requests, and the pre-header phase of streaming ones);
# retries that cannot succeed (a quota window that resets far beyond the
# backoff budget) are skipped so the honest 429 is not delayed.
retry_attempts = 3
retry_backoff_seconds = [5, 20, 60]

[providers.claude]
enabled = false
bin = "claude"
timeout_seconds = 600.0
max_concurrent_requests = 2
strip_api_key_env = true
models = ["claude:sonnet", "claude:opus", "claude:haiku", "claude:fable"]
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
- `AIRELAYS_OPENAI_BALANCE` (`balanced` default, `round_robin`, or `ordered`)
- `AIRELAYS_OPENAI_EXTRA_MODELS` (comma-separated ids advertised beyond the upstream catalog)
- `AIRELAYS_OPENAI_ACCOUNT_COOLDOWN_SECONDS`
- `AIRELAYS_OPENAI_RETRY_ATTEMPTS` (automatic retries for failed upstream calls; `3` default, `0` disables)
- `AIRELAYS_OPENAI_RETRY_BACKOFF_SECONDS` (comma-separated wait before each retry; `5,20,60` default)
- `AIRELAYS_ENABLE_CLAUDE` (legacy `AIRELAYS_ENABLE_CLAUDE_EXPERIMENTAL` is still honored)
- `AIRELAYS_CLAUDE_BIN`
- `AIRELAYS_CLAUDE_TIMEOUT_SECONDS`
- `AIRELAYS_CLAUDE_MAX_CONCURRENT_REQUESTS`
- `AIRELAYS_CLAUDE_STRIP_API_KEY_ENV`
- `AIRELAYS_CLAUDE_MODELS`

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

`[upstream] client_version = "auto"` discovers the installed Codex CLI's
version for model-catalog requests, with a tested minimum of `0.153.4` for
standalone installations. The local version probe is bounded to three
seconds and cached for five minutes. Updating Codex allows discovery to
follow newer version-gated catalogs without adding model names to AIRelays.
The former shipped value `0.124.0` is treated as `auto`, including existing
desktop configuration files. Other explicit version pins are honored and
can select an older catalog.

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
- `extra_models` defaults to an empty list. Existing configured entries
  continue to extend a successful catalog and are deduplicated against it.

`AIRELAYS_MODELS_CACHE_TTL_SECONDS` remains accepted as a shorter alias for
`AIRELAYS_OPENAI_MODELS_CACHE_TTL_SECONDS`.

Claude runtime:

- discovers models and alias resolutions from the installed CLI, without
  generating text; its model catalog uses `models_cache_ttl_seconds` too
- configured `models` extend discovery and remain available as fallback
  when CLI discovery is unavailable
- use `GET /v1/models?refresh=true` or the desktop Models Refresh button
  to reload provider catalogs immediately

- enabled by default; set `[providers.claude].enabled = false` or `AIRELAYS_ENABLE_CLAUDE=false` to opt out (requests still require the local `claude` CLI to be installed and signed in)
- uses the local `claude` CLI
- browser login is handled by `claude auth login --claudeai`
- headless login is handled by `claude setup-token` plus `CLAUDE_CODE_OAUTH_TOKEN`
- follows the relay's protected or open local auth mode
- requires loopback binding
