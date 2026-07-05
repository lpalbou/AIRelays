# AIRelays Desktop

Cross-platform tray app for AIRelays, built with Tauri v2: a Rust core
supervises the relay process and owns the system tray; the dashboard is one
web codebase shared across macOS, Windows, and Linux.

## Layout

- `src-tauri/` — Rust core: tray, relay supervision, config rendering,
  status polling, and the command layer the dashboard calls.
- `ui/` — the dashboard (vanilla HTML/CSS/JS, no build step): Overview,
  Traffic, Console, and Settings views.
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

## Platform notes

- Tray state: bolt with relay arcs when the relay answers, bolt alone when
  it does not (green/red on Windows and Linux, monochrome template on
  macOS). Security toggles live only in the dashboard, where their
  consequences are explained.
- First run (or a failed tray) opens the dashboard window automatically.
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

## Quality process

The initial implementation went through a six-reviewer adversarial pass
(correctness, architecture-fit, frontend contract, cross-platform &
packaging, naive-user UX, expert UX + visual design). All blocking findings
were fixed; residual risks are the unsigned macOS artifact, the not yet
exercised Windows/Linux CI legs, and the config-ownership overlap noted
above.
