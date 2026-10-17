# Security

AIRelays separates two authority domains:

- upstream provider auth
- local client access to AIRelays itself

The upstream subscription login is not the relay access credential.

## Protected Routes

Protected by default:

- `/v1/*`
- `/no-tools/v1/*`

Public:

- `/`
- `GET /healthz`

Detailed diagnostics live behind `GET /v1/relay/status` unless you intentionally launch an open local relay.

## Relay Token

The normal path is:

```bash
airelays init
```

Show the current token:

```bash
airelays token show
```

Rotate it:

```bash
airelays token rotate
```

## Credential Storage

AIRelays stores its secrets the same way on macOS, Linux and Windows
([ADR 0008](adr/0008-uniform-cross-platform-runtime.md)):

- OpenAI sign-ins, the relay token and the Claude setup token are files in the
  data directory. On macOS and Linux the directories AIRelays creates are
  owner-only (0700) and the files are created owner-only (0600); on Windows
  they inherit the user profile's permissions, so keep the data directory
  inside your profile.
- Files are replaced atomically, and a lock file next to each sign-in keeps
  two AIRelays processes (for example the tray relay and a CLI command) from
  refreshing the same rotating token at once.
- AIRelays does not use the OS keychain, Credential Manager, or Secret
  Service. Protection at rest comes from file permissions and disk
  encryption; enable FileVault, BitLocker, or LUKS if the machine may be lost.
- Claude browser sign-ins stay with Claude Code, which keeps them in the macOS
  keychain or in each profile's folder on Linux and Windows (see
  [Claude Guardrails](#claude-guardrails)).

## Open Local Relay Mode

Open local relay mode applies to all enabled providers, including the Claude runtime.

```bash
airelays init --no-auth
airelays serve --no-auth --port 8080
```

Equivalent environment override:

```bash
AIRELAYS_REQUIRE_BEARER_AUTH=false airelays serve --port 8080
```

## Claude Guardrails

When the Claude runtime is enabled:

- the relay's protected or open local auth mode applies
- loopback binding is required
- `trust_x_forwarded_for` is rejected
- each request is a fresh CLI run with session persistence and built-in
  tools off; client tool calls are returned, never run (see
  [docs/api.md](api.md#tool-calls-on-claude-models))
- the runtime is local-only

AIRelays uses only Claude accounts it owns: profiles added through
`airelays claude login`, each with its own CLI-owned credential store, and an
optional setup token stored 0600 by `airelays claude set-token`. The personal
Claude Code sign-in, an inherited `CLAUDE_CONFIG_DIR`, and an inherited
`CLAUDE_CODE_OAUTH_TOKEN` are never used. AIRelays stores identity metadata
and usage state beside each profile. It never reads Claude credentials
(no keychain or credential-file reads) and does not implement OAuth login or
refresh; usage comes from Claude Code's `get_usage` request inside each
profile. Signing out runs `claude auth logout` only inside that relay-owned
profile, or deletes the stored setup token; it never signs out the personal
Claude Code or other accounts.

Every Claude Code process runs with an explicit environment, its profile's
`CLAUDE_CONFIG_DIR`, and a relay-owned working directory. Processes are ended
gracefully (stdin closed or SIGTERM before SIGKILL) so a token refresh in
progress can finish. Claude Code still derives a few files from the home
directory (for example device keys) regardless of `CLAUDE_CONFIG_DIR`. On
macOS, run the relay in your login session, not over SSH or as a
LaunchDaemon, so Claude Code can use the keychain.

## Desktop Updates

The desktop app's update check reads the public GitHub Releases list for
`lpalbou/AIRelays`; it sends no account, token, or usage data. An in-place
update installs only the release that check offered, and the installer
refuses a download whose SHA-256 differs from the digest GitHub publishes
for that release asset. Downloads are trusted through HTTPS to GitHub; the
installers are not code-signed. See
[desktop/README.md](../desktop/README.md#about-and-updates).

## Rate Limits

Default single-process protections:

- `120` requests/minute per IP
- burst `40`
- `8` concurrent requests per IP
- temporary IP block after repeated bad tokens

## Upload Ceilings

- `32` MiB per file
- `256` MiB total stored file bytes

## Logging

AIRelays logs:

- inbound requests
- provider resolution
- endpoint rejects
- provider and upstream requests
- provider and upstream responses
- provider stream lines
- usage summaries

Request and response contents, including prompts and model outputs, can be written to the local AIRelays log files. Raw bearer tokens are redacted. The logs directory is owner-only, like the rest of the data directory.

Traffic logs default to a 7-day age limit and 1 GiB disk budget, with hourly/
50 MiB rotation. The oldest whole files are permanently deleted, including
eligible logs already present when upgrading. Use the tray's retention controls,
`airelays logs`, or the authenticated `/v1/relay/logging` API to change the policy.
This is log retention only; stored uploads and conversations are separate.
See [configuration](configuration.md#traffic-log-retention) for details.
