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

## Layout

In Overview, **Connect Your App** combines the endpoint and relay key with
**Authentication** and **Who can connect**. Access changes take effect
immediately and restart a running relay; the card explains open-mode risks
and when network access pauses Claude.

- `src-tauri/` — Rust core: tray, relay supervision, config rendering,
  status polling, and the command layer the dashboard calls.
- `ui/` — the dashboard (vanilla HTML/CSS/JS, no build step): Overview,
  Traffic, Console, and Settings views.
- `scripts/bundle_runtime.sh` — embeds a standalone CPython with `airelays`
  installed into `src-tauri/runtime/`, making the app self-contained.

## Develop

The Settings tab includes **Traffic log retention**, with week/month presets,
custom disk and file limits, and current usage. **Apply log retention** calls
the running relay API immediately, independently of Save & Restart. The saved
policy belongs to the relay's log directory, so desktop config generation
cannot overwrite changes made through the CLI or API. With the relay stopped,
use `airelays logs` to configure the same policy offline.

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
- Clicking outside the dashboard hides it without quitting the app or
  stopping the relay; the page and unsaved edits stay open. On macOS and
  Windows, left-click the tray icon to reopen it, or right-click for the
  menu and **Open Dashboard**. On Linux, use **Open Dashboard** in the tray
  menu. Automatic hiding is disabled if tray initialization fails.
- On macOS, opening the already-running app also reopens its dashboard.
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
