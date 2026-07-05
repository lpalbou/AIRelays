# AIRelays Menu Bar App

This is a native macOS menu bar app for local AIRelays control. It installs and runs as `AIRelays.app` (the `AIRelaysMenuBar` name is only the internal Swift package name).

Build it:

```bash
swift build --package-path macos/AIRelaysMenuBar
```

Run it:

```bash
swift run --package-path macos/AIRelaysMenuBar AIRelaysMenuBar
```

Package and install it into `/Applications`:

```bash
swift build --package-path macos/AIRelaysMenuBar -c release
zsh macos/AIRelaysMenuBar/package_app.sh
ditto macos/AIRelaysMenuBar/build/AIRelays.app /Applications/AIRelays.app
```

The packaged app is self-contained:

- `package_app.sh` downloads a standalone CPython (cached in `.runtime_cache/`, override with `PYTHON_RUNTIME_URL`) and installs `airelays` from the repository into `Contents/Resources/runtime`.
- The app launches the relay with the embedded runtime (`python3 -m airelays`), resolved from the live bundle path, so it keeps working if the app is moved.
- The default working directory is `~/Library/Application Support/AIRelaysMenuBar`; relay config and data stay in their usual `~/.config/airelays` and `~/.airelays` locations.
- Bytecode is precompiled before signing and the relay runs with `PYTHONDONTWRITEBYTECODE=1`, so nothing ever writes into the signed bundle.

When run from a source checkout (`swift run`), the app falls back to the repository's `.venv/bin/airelays`, then to `airelays` on PATH.

## Icons

- `assets/icon_artwork.png` is the source artwork.
- `scripts/make_icons.swift` renders `assets/AppIcon.icns` (squircle-masked on the macOS icon grid) and the color-coded status-bar glyphs in `Sources/AIRelaysMenuBar/Resources/`: green bolt with relay arcs when the relay is reachable, red bolt when it is not.
- Regenerate after changing the artwork:

```bash
swift macos/AIRelaysMenuBar/scripts/make_icons.swift \
  macos/AIRelaysMenuBar/assets/icon_artwork.png \
  macos/AIRelaysMenuBar/assets
iconutil -c icns macos/AIRelaysMenuBar/assets/AppIcon.iconset \
  -o macos/AIRelaysMenuBar/assets/AppIcon.icns
cp macos/AIRelaysMenuBar/assets/menu_bar_icon_*.png \
  macos/AIRelaysMenuBar/Sources/AIRelaysMenuBar/Resources/
```

The app:

- switches auth mode: protected (relay token) or open (`--no-auth`), one click in the dashboard or status-bar menu
- switches network exposure: loopback only or private network (LAN); the app defaults to accepting private-network connections on `0.0.0.0:8317` (8317 avoids the dev-server collisions that 8080 invites)
- disables the loopback-only Claude experimental runtime automatically while the listener is exposed to the LAN
- starts, stops, and restarts the local relay
- edits and writes the AIRelays config file
- toggles protected mode vs `--no-auth`
- rotates or reveals the relay token
- launches OpenAI and Claude login flows
- polls `/v1/relay/status`
- tails AIRelays JSONL traffic logs into a request-oriented view
