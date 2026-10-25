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

Clients outside the configured [network scope](#who-can-connect) get `403`
on every route, public ones included.

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

Open mode is refused together with `network_scope = "any"` on a non-loopback
listener; see [Who Can Connect](#who-can-connect).

## Who Can Connect

`[security] network_scope` decides which client addresses the relay serves.
Set it in `config.toml`, with `AIRELAYS_NETWORK_SCOPE`, with `airelays serve
--network-scope`, or write it into a new config with `airelays init
--network-scope`. The check runs on every route, public ones included, before
rate limits and authentication, so a client outside the scope cannot even try
API keys:

| Scope | Who may connect | Refused clients get |
| --- | --- | --- |
| `loopback` | this machine only | `403 client_not_on_this_machine` |
| `private` (default) | this machine and private networks | `403 client_not_on_private_network` |
| `any` | every address (requires the API key) | nothing; rate limits and auth still apply |

- **Private networks** are loopback (`127.0.0.0/8`, `::1`), `10.0.0.0/8`,
  `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10` (CGNAT, used by
  Tailscale), link-local `169.254.0.0/16`, IPv6 unique local `fc00::/7` and
  IPv6 link-local `fe80::/10`, including IPv4-mapped IPv6 forms. Documentation
  and benchmarking ranges are not private. This is what the desktop app's
  **Devices on my network** means, enforced by the relay itself rather than by
  the router.
- **A loopback listener is always `loopback`.** When the relay listens on
  `127.0.0.1`, `localhost` or `::1` (the CLI default), only this machine can
  reach it, whatever `network_scope` says; `airelays status` then reports
  `loopback (this machine only); configured any, narrowed by the loopback
  listener`. To serve other devices, listen on `0.0.0.0` as well.
- **`any` requires bearer auth.** The relay refuses to start, and `airelays
  init` refuses to write a config, when the effective scope is `any` and
  `require_bearer_auth = false` (including `--no-auth`). Everyone who has the
  key can spend your subscription quota, and the relay speaks plain HTTP.
  Rate limits and failed-auth blocking apply per client IP.
- Refused requests return OpenAI's error object with type `permission_error`
  and are logged as `endpoint_rejected` traffic records with reason
  `network_scope`.
- `airelays init` and `airelays status` print the scope as **Who can
  connect**; `GET /v1/relay/status` reports `relay.network_scope` (configured),
  `relay.effective_network_scope` and `security.network_scope` (enforced).

### Internet access from a headless relay

```bash
airelays init --host 0.0.0.0 --network-scope any   # add --force to rewrite an existing config
airelays login --device
airelays serve
```

`airelays init` writes `--host`, `--port` and `--network-scope` when it
creates the config file. On an existing file it stops and asks for `--force`
(the file is then rewritten from the current settings plus these flags);
alternatively edit `[server] host` and `[security] network_scope` by hand. You can also leave the file alone and
run `airelays serve --host 0.0.0.0 --network-scope any`. The relay token is
kept; show it with `airelays token show`.

Then make the relay reachable:

1. Forward the relay's TCP port (default `8080` for the CLI, `8317` for the
   desktop app) on your router to this machine's private address.
2. On macOS, allow incoming connections for the relay when the application
   firewall asks; on Linux, open the port in your host firewall.
3. Test from outside your network, for example from a phone on mobile data.
   From inside the same network the public address only answers when the
   router supports NAT loopback (hairpinning).
4. For real use, put the relay behind a TLS reverse proxy (or reach it over a
   VPN such as Tailscale, which `private` already allows) so the API key and
   prompts do not cross the internet in plain HTTP.

### Reverse proxies and `trust_x_forwarded_for`

By default the scope applies to the TCP peer address. A reverse proxy on the
same machine therefore appears as `127.0.0.1`, and every request it forwards
passes a `loopback` or `private` scope. With `trust_x_forwarded_for = true`
the scope, the rate limits and the failed-auth blocks apply to the first
`X-Forwarded-For` address, the original client; a proxy that forwards
internet traffic then needs `network_scope = "any"`. Enable it only behind a
proxy that overwrites that header. The Claude runtime refuses
`trust_x_forwarded_for`.

### Public address lookup (desktop app)

To show the **Internet** address, the desktop app asks a public echo service
for this machine's public IP over HTTPS (`https://api.ipify.org`, then
`https://checkip.amazonaws.com` as a fallback; IPv4 preferred). It does so
only while **Anyone on the internet** is selected: it re-checks a found
address every ten minutes and retries a failed lookup after a minute. With
any other scope it contacts no such service and forgets the address. The
address shown is your router's public IP; clients reach it only once the
router forwards the port.

## Claude Guardrails

When the Claude runtime is enabled:

- the relay's protected or open local auth mode applies
- `trust_x_forwarded_for` is rejected
- each request is a fresh CLI run with session persistence and built-in
  tools off; client tool calls are returned, never run (see
  [docs/api.md](api.md#tool-calls-on-claude-models))
- the runtime runs the `claude` CLI on the relay's machine and is served on the
  relay's listener; in network mode, devices on your network (or, with
  `network_scope = "any"`, anyone who has the key) can use your Claude
  accounts, so keep the API key required there

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

Default single-process protections, applied per client IP after the
[network scope](#who-can-connect) check:

- `120` requests/minute per IP
- burst `40`
- `50` concurrent requests per IP
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
