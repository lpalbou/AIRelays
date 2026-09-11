#!/usr/bin/env bash
# Rebuild and install the self-contained macOS desktop app, then launch it.
set -euo pipefail

if [[ "$(uname -s)" != Darwin ]]; then
  echo "This installer requires macOS." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="/Applications/AIRelays.app"
# Keep installer artifacts separate from old or relocated development caches.
BUILD_TARGET="$REPO_DIR/desktop/src-tauri/target/app-install"
BUILD_APP="$BUILD_TARGET/release/bundle/macos/AIRelays.app"
for tool in npm cargo swift codesign ditto; do
  command -v "$tool" >/dev/null || { echo "Missing build tool: $tool" >&2; exit 1; }
done
[[ -w /Applications ]] || { echo "/Applications is not writable by this user." >&2; exit 1; }

cd "$REPO_DIR/desktop"
npm ci
swift scripts/make_tray_icons.swift src-tauri/icons
bash scripts/bundle_runtime.sh
CARGO_TARGET_DIR="$BUILD_TARGET" npm run build -- --bundles app
[[ -x "$BUILD_APP/Contents/MacOS/airelays-desktop" ]]

# Prepare and verify the full replacement before interrupting the old app.
INSTALL_DIR="$(mktemp -d /Applications/.airelays-install.XXXXXX)"
ditto "$BUILD_APP" "$INSTALL_DIR/AIRelays.app"
codesign --force --deep --sign - "$INSTALL_DIR/AIRelays.app"
codesign --verify --deep --strict "$INSTALL_DIR/AIRelays.app"

# Match only this installed app and its embedded relay, never other relays.
APP_PIDS="$(pgrep -f '^/Applications/AIRelays[.]app/Contents/MacOS/airelays-desktop( |$)' || true)"
RELAY_PIDS="$(pgrep -f '^/Applications/AIRelays[.]app/Contents/Resources/runtime/bin/python3( |$)' || true)"
if [[ -n "$APP_PIDS" ]]; then
  echo "Stopping the installed app for replacement…"
  while read -r pid; do kill -TERM "$pid" 2>/dev/null || true; done <<< "$APP_PIDS"
fi
if [[ -n "$RELAY_PIDS" ]]; then
  while read -r pid; do
    # The desktop starts its relay in a dedicated process group.
    pgid="$(ps -o pgid= -p "$pid" | tr -d ' ' || true)"
    if [[ "$pgid" == "$pid" ]]; then
      kill -TERM -- "-$pid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done <<< "$RELAY_PIDS"
fi
for ((attempt = 0; attempt < 30; attempt++)); do
  if ! pgrep -f '^/Applications/AIRelays[.]app/Contents/(MacOS/airelays-desktop|Resources/runtime/bin/python3)( |$)' >/dev/null; then
    break
  fi
  sleep 1
done
if pgrep -f '^/Applications/AIRelays[.]app/Contents/(MacOS/airelays-desktop|Resources/runtime/bin/python3)( |$)' >/dev/null; then
  echo "The old app or relay has not exited. Installation stopped; existing app preserved." >&2
  exit 1
fi

if [[ -e "$APP_PATH" ]]; then
  mkdir "$INSTALL_DIR/previous"
  mv "$APP_PATH" "$INSTALL_DIR/previous/AIRelays.app"
fi
if ! mv "$INSTALL_DIR/AIRelays.app" "$APP_PATH"; then
  if [[ -d "$INSTALL_DIR/previous/AIRelays.app" ]]; then
    mv "$INSTALL_DIR/previous/AIRelays.app" "$APP_PATH"
    open "$APP_PATH"
  fi
  exit 1
fi
open "$APP_PATH"
echo "Installed and launched: $APP_PATH"
if [[ -d "$INSTALL_DIR/previous/AIRelays.app" ]]; then
  echo "Previous app preserved at: $INSTALL_DIR/previous/AIRelays.app"
fi
