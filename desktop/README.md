# AIRelays Desktop

Cross-platform tray app for AIRelays, built with Tauri v2: a Rust core
supervises the relay process and owns the system tray; the dashboard is one
web codebase shared across macOS, Windows, and Linux.

## Install a release

The one-line installers download the newest release installer, verify its
SHA-256 digest, and install it without sudo or admin rights:

```bash
# macOS (Apple Silicon) and Linux (x86_64)
curl -fsSL https://raw.githubusercontent.com/lpalbou/AIRelays/main/scripts/install-desktop.sh | bash
```

```powershell
# Windows (x64), PowerShell
irm https://raw.githubusercontent.com/lpalbou/AIRelays/main/scripts/install-desktop.ps1 | iex
```

See the [README's install section](../README.md#install) for what each
platform installs and the `AIRELAYS_VERSION` / `AIRELAYS_NO_LAUNCH` options.

## Dashboard

### Overview: Connect Your App

**Connect Your App** combines the endpoint addresses and relay key with
**Authentication** and **Who can connect** (**This machine only**, **Devices
on my network**, or **Anyone on the internet**). Access changes take effect
immediately and restart a running relay; the card explains open-mode risks.

- **Devices on my network** is enforced by the relay, which refuses public
  client addresses (`network_scope = "private"`).
- **Anyone on the internet** first shows what it means (router port
  forwarding, quota exposure, plain HTTP without TLS) and applies only after
  **Enable internet access**. It turns the API key on and keeps **Open (no
  key)** disabled while selected.
- The address rows list **Local**, each **LAN** address and, while **Anyone on
  the internet** is selected, an **Internet** row with this machine's public
  address. The app looks it up from `api.ipify.org`, then
  `checkip.amazonaws.com`, only in that scope (re-checked every ten minutes;
  a failed lookup is retried after a minute). A note under the rows explains
  port forwarding and NAT loopback: from inside your own network the public
  address only answers when the router supports hairpinning, so test it from
  a phone on mobile data.
- Claude follows the same choice as every other model.

See [Who Can Connect](../docs/security.md#who-can-connect) for the address
ranges and the headless equivalent.

**Try it**, beside the card, shows one chat completion in **curl**,
**Python**, **JavaScript / TypeScript** or **Rust** (the official `openai`
SDKs and `async-openai`). Choose the base URL (**Local**, **LAN** or
**Internet**) above the code. The example uses the cheapest chat model in the
relay's catalog (lowest published output price, then input price; unpriced,
upstream-hidden and image models are skipped). The API key is masked on
screen; the panel's copy button copies the example with the real key (in open
mode, a placeholder key and no `Authorization` header). The curl example has
no comment lines, so it pastes into an interactive zsh unchanged. The chosen
language is remembered.

### Models, Traffic, Settings

- **Models** shows one table per provider: Model (click or Enter copies the
  id), Accounts (coverage bar), Reasoning (the default mode highlighted),
  Structured output, **Cost / 1M** and Notes. Cost is the provider's
  published API list price per 1M tokens, input / output, with a `$` to
  `$$$$$` tier; hover for cached-input price, basis and source. It is a
  relative cost indicator only: subscription requests are not billed per
  token. Models without a published price show "—". Arrow keys move between
  rows; the table fits the 760 px minimum window width.
- **Traffic** lists one row per request. A selected request stays selected
  while new requests arrive; its detail pane updates only when that request
  changes, the table keeps its scroll position, and the pane's copy button
  copies the raw records. A request that leaves the recent list keeps its
  detail with a "No longer in the recent list" note.
- **Settings → Traffic log retention** offers week/month presets, custom disk
  and file limits, and current usage. **Apply log retention** calls the
  running relay API immediately, independently of Save & Restart. The saved
  policy belongs to the relay's log directory, so desktop config generation
  cannot overwrite changes made through the CLI or API. With the relay
  stopped, use `airelays logs` to configure the same policy offline.

The Anthropic card's **Sign in** / **Add account** menu offers **In a
browser** (a new AIRelays-owned Claude Code profile) or **With a token** (a
`claude setup-token` token; inference-only, so its row explains that Claude
reports no usage for setup tokens instead of showing bars). AIRelays never
uses your personal Claude Code sign-in, and sign-out on a row affects only
that AIRelays account.

## Layout

- `src-tauri/` — Rust core: tray, relay supervision, config rendering,
  status polling, public-address lookup, and the command layer the dashboard
  calls.
- `ui/` — the dashboard (vanilla HTML/CSS/JS, no build step): Overview,
  Models, Traffic, Console, Settings, and About views.
- `scripts/bundle_runtime.sh` — embeds a standalone CPython with `airelays`
  installed into `src-tauri/runtime/`, making the app self-contained.

## Develop

```bash
cd desktop
npm install
npx tauri dev
```

Without an embedded runtime the app falls back to `airelays` on PATH; the
Settings tab's "Relay command override" can point anywhere.

The dashboard can also be opened in a plain browser for UI work — a mock
backend with representative data takes over automatically:

```bash
cd desktop/ui && python3 -m http.server 8899
```

## Build installers

```bash
cd desktop
./scripts/bundle_runtime.sh          # embed relay runtime for this platform
npx tauri build                      # DMG / NSIS+MSI / AppImage+deb
```

CI builds all three platforms from `.github/workflows/desktop.yml` on
`desktop-v*` tags.

## About and updates

The **About** page shows the installed version, the author
(Laurent-Philippe Albou, PhD, contact@abstractframework.ai) and license
(MIT), and links to the
documentation, changelog, issue tracker, source code, license, and
disclaimer. Opening it checks GitHub Releases once per app session for a
newer stable release with an installer for your system; **Check for
updates** asks again. When one exists, the page shows its release notes and
an **Update to X.Y.Z** button.

From 0.19.0 on, updating closes the app, stops the relay, runs the same one-line installer
described in the [README](../README.md#install) pinned to that release and
to the SHA-256 digest GitHub publishes for its installer, then reopens
AIRelays. Accounts, the API key, and settings are kept. If the install
fails, the current version reopens and reports why. Progress is written to
`~/.airelays/update/update.log`.

In-place updates apply to copies installed by the one-line installers:
`%LOCALAPPDATA%\AIRelays` on Windows, `AIRelays.app` in an Applications
folder on macOS, and `~/.local/bin/airelays-desktop` on Linux. Other copies
(the `.deb`, an AppImage elsewhere, a development build) show the release
page instead.

## Supervision behavior

- On app open, the relay starts automatically unless one is already
  answering on the configured address (e.g. started by the CLI), which the
  app respects instead of colliding with. Toggle: Settings → Launch.
- A relay that exits without a user stop is respawned with capped
  exponential backoff (2s doubling to 60s, giving up after 5 consecutive
  failures) and a native notification; a deliberate Stop never triggers a
  respawn. Toggle: Settings → Launch.
- "Start AIRelays at login" (Settings → Desktop App) registers the
  OS-native mechanism: launch agent on macOS, registry Run key on Windows,
  autostart `.desktop` entry on Linux.

## Platform notes

- Tray state: bolt with relay arcs when the relay answers, bolt alone when
  it does not (glowing green / red on every platform). Served requests play
  a ~1s pulse: the bolt swells into a brighter green with a ripple ring,
  then eases back. Security toggles live only in the dashboard, where their
  consequences are explained.
- First run (or a failed tray) opens the dashboard window automatically.
- Tray clicks: on macOS and Windows, left click opens the dashboard and
  right click opens the menu. On Linux the AppIndicator host shows the menu
  on every click, so the menu starts with **Open Dashboard**.
- Clicking outside the dashboard hides it without quitting the app or
  stopping the relay; the page and unsaved edits stay open. A window that a
  tray click brought forward stays open. Automatic hiding is disabled if tray
  initialization fails.
- The tray menu's **Copy Address** submenu lists every URL the relay answers
  at, labelled by place: this machine first, then each private-network
  address (network), then the public address (internet) while **Anyone on
  the internet** is selected. Choosing one copies it to the clipboard and
  confirms with a notification. The list refreshes when Wi-Fi or settings
  change; while the public address is unknown, a disabled entry explains that
  internet clients use the router's public IP and forwarded port.
- On macOS, opening the already-running app also reopens its dashboard.
- macOS: the app runs the relay in your login session, which Claude Code
  needs for keychain access. A relay started over SSH or as a LaunchDaemon
  cannot use the Claude profiles' keychain entries.
- Linux: GNOME needs the AppIndicator extension to show tray icons; the deb
  declares `libayatana-appindicator3-1` and `xdg-utils` as dependencies.
- Windows: the relay tree is supervised through a Job Object, so stopping
  or quitting never orphans processes. Unsigned installers trigger
  SmartScreen; sign or ship via winget.
- macOS: release DMGs need Developer ID signing + notarization before
  distribution (ad-hoc-signed downloads are rejected by Gatekeeper), and
  the embedded runtime's Mach-O files must be included in signing. Not yet
  wired into CI.
- The relay's config, data, and logs stay in `~/.config/airelays` and
  `~/.airelays`, shared with the CLI and the native macOS menu bar app.
  Saving settings rewrites `config.toml`; hand-edits to keys the app does
  not manage are not preserved.
