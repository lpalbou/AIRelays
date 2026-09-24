#!/usr/bin/env bash
# Install or update the AIRelays desktop app from the GitHub release installers.
#
#   curl -fsSL https://raw.githubusercontent.com/lpalbou/AIRelays/main/scripts/install-desktop.sh | bash
#
# macOS (Apple Silicon): copies AIRelays.app from the release DMG into
#   /Applications (or ~/Applications when /Applications is not writable).
# Linux (x86_64): installs the release AppImage as ~/.local/bin/airelays-desktop
#   and registers a launcher entry. No sudo on either platform.
#
# The app is self-contained: it embeds its own Python and relay, so neither
# Python nor npm is needed. Downloads are verified against the SHA-256 digest
# GitHub publishes for each release asset.
#
# Environment:
#   AIRELAYS_VERSION=0.14.1  install that release instead of the newest one
#   AIRELAYS_APP_DIR=DIR     macOS: install AIRelays.app into DIR
#   AIRELAYS_NO_LAUNCH=1     do not start the app after installing
set -euo pipefail

REPO="lpalbou/AIRelays"
API="https://api.github.com/repos/$REPO"

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null || die "curl is required."

case "$(uname -s)/$(uname -m)" in
  Darwin/arm64) PLATFORM=macos; ASSET_PATTERN='_aarch64\.dmg$' ;;
  Darwin/*)
    die "the desktop app is only built for Apple Silicon Macs. On Intel Macs, install the headless relay instead:
  curl -fsSL https://raw.githubusercontent.com/$REPO/main/scripts/install-headless.sh | bash" ;;
  Linux/x86_64) PLATFORM=linux; ASSET_PATTERN='_amd64\.AppImage$' ;;
  Linux/*)
    die "the desktop app is only built for x86_64 Linux. Install the headless relay instead:
  curl -fsSL https://raw.githubusercontent.com/$REPO/main/scripts/install-headless.sh | bash" ;;
  *) die "unsupported system $(uname -s). On Windows, run in PowerShell:
  irm https://raw.githubusercontent.com/$REPO/main/scripts/install-desktop.ps1 | iex" ;;
esac

# Prints "<download url> <digest>" for the first asset matching the pattern.
# Splitting on JSON separators keeps this independent of jq, Python, and of
# whether the API response is pretty-printed.
find_asset() {
  tr ',{}' '\n\n\n' | awk -v pattern="$ASSET_PATTERN" '
    /"digest":/ { digest = $0; sub(/.*"digest": *"?/, "", digest); sub(/".*/, "", digest) }
    /"browser_download_url":/ {
      url = $0; sub(/.*"browser_download_url": *"/, "", url); sub(/".*/, "", url)
      if (url ~ pattern) { print url, (digest == "" || digest == "null" ? "-" : digest); exit }
      digest = ""
    }'
}

if [[ -n "${AIRELAYS_VERSION:-}" ]]; then
  TAG="v${AIRELAYS_VERSION#v}"
  RELEASES="$(curl -fsSL "$API/releases/tags/$TAG")" || die "release $TAG not found."
else
  # Newest release first. Desktop installers are attached a few minutes after
  # a release is created, so fall back to the newest release that has one.
  RELEASES="$(curl -fsSL "$API/releases?per_page=10")" || die "could not reach the GitHub API."
fi
read -r URL DIGEST < <(printf '%s' "$RELEASES" | find_asset) || true
[[ -n "${URL:-}" ]] || die "no $PLATFORM desktop installer found in ${TAG:-the recent releases}."

TMP="$(mktemp -d)"
cleanup() {
  if [[ -n "${MOUNT:-}" ]]; then hdiutil detach -quiet "$MOUNT" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

FILE="$TMP/${URL##*/}"
say "Downloading ${URL##*/}"
curl -fL --progress-bar -o "$FILE" "$URL"

if [[ "$DIGEST" == sha256:* ]]; then
  if command -v sha256sum >/dev/null; then
    ACTUAL="$(sha256sum "$FILE" | cut -d' ' -f1)"
  else
    ACTUAL="$(shasum -a 256 "$FILE" | cut -d' ' -f1)"
  fi
  [[ "$ACTUAL" == "${DIGEST#sha256:}" ]] || die "checksum mismatch for ${URL##*/}: expected ${DIGEST#sha256:}, got $ACTUAL"
  say "Checksum verified (sha256 $ACTUAL)"
else
  say "GitHub published no checksum for this asset; skipping verification."
fi

install_macos() {
  local app_dir="${AIRELAYS_APP_DIR:-/Applications}"
  if [[ -z "${AIRELAYS_APP_DIR:-}" && ! -w /Applications ]]; then
    app_dir="$HOME/Applications"
  fi
  mkdir -p "$app_dir"
  local app="$app_dir/AIRelays.app"

  MOUNT="$TMP/mnt"
  hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MOUNT" "$FILE" >/dev/null
  [[ -d "$MOUNT/AIRelays.app" ]] || die "the DMG does not contain AIRelays.app."

  # Stage next to the destination so the final swap is a same-volume rename.
  local staging
  staging="$(mktemp -d "$app_dir/.airelays-install.XXXXXX")"
  ditto "$MOUNT/AIRelays.app" "$staging/AIRelays.app"
  hdiutil detach -quiet "$MOUNT"
  MOUNT=""
  # Unsigned build: clear any quarantine flag so Gatekeeper does not report
  # the app as damaged. curl downloads normally carry none.
  /usr/bin/xattr -dr com.apple.quarantine "$staging/AIRelays.app" >/dev/null 2>&1 || true

  # Stop only this installation's app and embedded relay, never other relays.
  local escaped pattern
  escaped="$(printf '%s' "$app" | sed 's/[][\.*^$+?(){}|]/\\&/g')"
  pattern="^$escaped/Contents/(MacOS/airelays-desktop|Resources/runtime/bin/python3)( |$)"
  if pgrep -f "$pattern" >/dev/null; then
    say "Stopping the running AIRelays app"
    pkill -TERM -f "$pattern" || true
    for _ in $(seq 30); do pgrep -f "$pattern" >/dev/null || break; sleep 1; done
    if pgrep -f "$pattern" >/dev/null; then
      rm -rf "$staging"
      die "the running AIRelays app did not exit; quit it and run the installer again."
    fi
  fi

  if [[ -e "$app" ]]; then mv "$app" "$staging/previous.app"; fi
  if ! mv "$staging/AIRelays.app" "$app"; then
    if [[ -e "$staging/previous.app" ]]; then mv "$staging/previous.app" "$app"; fi
    die "could not install $app; the previous app was restored."
  fi
  rm -rf "$staging"
  say "Installed $app"

  if [[ -z "${AIRELAYS_NO_LAUNCH:-}" ]]; then open "$app"; fi
}

install_linux() {
  local bin_dir="$HOME/.local/bin"
  local target="$bin_dir/airelays-desktop"
  local data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
  mkdir -p "$bin_dir" "$data_dir/applications" "$data_dir/icons/hicolor/128x128/apps"

  chmod +x "$FILE"
  # Atomic replace: a running copy keeps its old file until it restarts.
  mv "$FILE" "$target.new"
  mv -f "$target.new" "$target"
  say "Installed $target"

  # AppImages mount themselves through FUSE 2, which recent distributions no
  # longer install by default. Without it, extract-and-run works unprivileged.
  local exec_line="$target"
  if ! { ldconfig -p 2>/dev/null || true; } | grep -q 'libfuse\.so\.2'; then
    exec_line="env APPIMAGE_EXTRACT_AND_RUN=1 $target"
    say "libfuse2 not found; the launcher uses APPIMAGE_EXTRACT_AND_RUN=1 (install libfuse2 for faster starts)."
  fi

  curl -fsSL -o "$data_dir/icons/hicolor/128x128/apps/airelays.png" \
    "https://raw.githubusercontent.com/$REPO/main/desktop/src-tauri/icons/128x128.png" 2>/dev/null || true
  cat > "$data_dir/applications/airelays.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AIRelays
Comment=Local OpenAI-compatible relay control
Exec=$exec_line
Icon=airelays
Terminal=false
Categories=Development;
EOF
  say "Added the AIRelays launcher to your applications menu"

  if [[ -z "${AIRELAYS_NO_LAUNCH:-}" && -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    if pgrep -f "^$target( |$)" >/dev/null; then
      say "AIRelays is already running; restart it to use the new version."
    else
      # shellcheck disable=SC2086 # exec_line is intentionally word-split
      nohup $exec_line >/dev/null 2>&1 &
    fi
  fi
}

"install_$PLATFORM"
say "Done. The app starts the relay (default http://127.0.0.1:8317) and shares ~/.config/airelays with the CLI."
