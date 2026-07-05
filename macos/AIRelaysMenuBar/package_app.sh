#!/bin/zsh
set -euo pipefail

# Packages AIRelaysMenuBar.app as a self-contained bundle:
# - the Swift menu bar binary
# - a standalone CPython runtime under Contents/Resources/runtime
#   with the airelays package installed, so the app never depends on a
#   development checkout or a system Python
# - the app icon and the SwiftPM resource bundle with menu bar glyphs

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_NAME="AIRelays.app"
APP_DIR="$SCRIPT_DIR/build/$APP_NAME"
EXECUTABLE="$(find "$SCRIPT_DIR/.build" -path '*/release/AIRelaysMenuBar' -type f | head -n 1)"
MACOS_DIR="$APP_DIR/Contents/MacOS"
RESOURCES_DIR="$APP_DIR/Contents/Resources"
INFO_TEMPLATE="$SCRIPT_DIR/Info.plist.template"
INFO_PLIST="$APP_DIR/Contents/Info.plist"
PKGINFO_FILE="$APP_DIR/Contents/PkgInfo"
APP_ICON="$SCRIPT_DIR/assets/AppIcon.icns"
RESOURCE_BUNDLE="$(dirname "$EXECUTABLE")/AIRelaysMenuBar_AIRelaysMenuBar.bundle"
RUNTIME_DIR="$RESOURCES_DIR/runtime"
RUNTIME_CACHE="$SCRIPT_DIR/.runtime_cache"

# Standalone CPython from astral-sh/python-build-standalone (install_only
# builds are relocatable). Override PYTHON_RUNTIME_URL to pin differently.
PYTHON_RUNTIME_URL="${PYTHON_RUNTIME_URL:-https://github.com/astral-sh/python-build-standalone/releases/download/20260623/cpython-3.13.14+20260623-aarch64-apple-darwin-install_only.tar.gz}"

if [[ -z "$EXECUTABLE" || ! -x "$EXECUTABLE" ]]; then
  echo "Missing release executable under $SCRIPT_DIR/.build" >&2
  echo "Run: swift build --package-path macos/AIRelaysMenuBar -c release" >&2
  exit 1
fi

mkdir -p "$MACOS_DIR" "$RESOURCES_DIR" "$RUNTIME_CACHE"
# The SwiftPM target keeps its internal name; the shipped executable is
# renamed so the app and its process both appear as "AIRelays".
cp "$EXECUTABLE" "$MACOS_DIR/AIRelays"

# Stale artifacts from earlier packaging layouts must not ship.
rm -f "$RESOURCES_DIR/bootstrap.json" "$MACOS_DIR/AIRelaysMenuBar"

cp "$INFO_TEMPLATE" "$INFO_PLIST"

# App icon shown in Finder, Dock, and the app switcher.
if [[ -f "$APP_ICON" ]]; then
  cp "$APP_ICON" "$RESOURCES_DIR/AppIcon.icns"
fi

# SwiftPM resource bundle carrying the menu bar glyphs.
if [[ -d "$RESOURCE_BUNDLE" ]]; then
  rm -rf "$RESOURCES_DIR/$(basename "$RESOURCE_BUNDLE")"
  cp -R "$RESOURCE_BUNDLE" "$RESOURCES_DIR/"
fi

# Embedded Python runtime: download once into the cache, then extract fresh
# into the bundle and install airelays from the repository checkout.
RUNTIME_TARBALL="$RUNTIME_CACHE/$(basename "$PYTHON_RUNTIME_URL")"
if [[ ! -f "$RUNTIME_TARBALL" ]]; then
  echo "Downloading Python runtime: $PYTHON_RUNTIME_URL"
  curl -fL --retry 3 -o "$RUNTIME_TARBALL.partial" "$PYTHON_RUNTIME_URL"
  mv "$RUNTIME_TARBALL.partial" "$RUNTIME_TARBALL"
fi

rm -rf "$RUNTIME_DIR"
EXTRACT_DIR="$(mktemp -d)"
tar -xzf "$RUNTIME_TARBALL" -C "$EXTRACT_DIR"
mv "$EXTRACT_DIR/python" "$RUNTIME_DIR"
rmdir "$EXTRACT_DIR"

echo "Installing airelays into the embedded runtime"
"$RUNTIME_DIR/bin/python3" -m pip install --quiet --no-cache-dir "$REPO_ROOT"

# Console-script shebangs embed absolute build paths; the app invokes
# `python3 -m airelays` instead, so drop them to keep the bundle relocatable.
rm -f "$RUNTIME_DIR/bin/airelays"

# Precompile all bytecode now: the code signature seals the bundle contents,
# so Python must never write .pyc files after signing (the app also runs the
# relay with PYTHONDONTWRITEBYTECODE=1).
"$RUNTIME_DIR/bin/python3" -m compileall -qq "$RUNTIME_DIR/lib" || true

printf 'APPL????' > "$PKGINFO_FILE"
codesign --force --deep --sign - "$APP_DIR" >/dev/null

echo "$APP_DIR"
