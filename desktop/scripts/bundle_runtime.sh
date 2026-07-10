#!/usr/bin/env bash
set -euo pipefail

# Embeds a standalone CPython runtime with airelays installed into the Tauri
# resources, making the desktop app self-contained (no system Python needed).
#
# Usage: bundle_runtime.sh [macos-arm64|macos-x64|linux-x64|windows-x64]
# Default: the current platform.
#
# The runtime lands in desktop/src-tauri/runtime/, which tauri.conf.json
# ships as a resource. Bytecode is precompiled with hash-based invalidation
# (mtimes do not survive bundling) and the app always runs the relay with
# PYTHONDONTWRITEBYTECODE=1, so nothing writes into the installed app.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TAURI_DIR="$SCRIPT_DIR/../src-tauri"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
RUNTIME_DIR="$TAURI_DIR/runtime"
CACHE_DIR="$SCRIPT_DIR/../.runtime_cache"

PBS_RELEASE="${PBS_RELEASE:-20260623}"
PBS_PYTHON="${PBS_PYTHON:-3.13.14}"

detect_platform() {
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) echo "macos-arm64" ;;
    Darwin-x86_64) echo "macos-x64" ;;
    Linux-x86_64) echo "linux-x64" ;;
    MINGW*-*|MSYS*-*|CYGWIN*-*) echo "windows-x64" ;;
    *) echo "unsupported" ;;
  esac
}

PLATFORM="${1:-$(detect_platform)}"

case "$PLATFORM" in
  macos-arm64) TRIPLE="aarch64-apple-darwin" ;;
  macos-x64) TRIPLE="x86_64-apple-darwin" ;;
  linux-x64) TRIPLE="x86_64-unknown-linux-gnu" ;;
  windows-x64) TRIPLE="x86_64-pc-windows-msvc" ;;
  *) echo "Unsupported platform: $PLATFORM" >&2; exit 1 ;;
esac

TARBALL="cpython-${PBS_PYTHON}+${PBS_RELEASE}-${TRIPLE}-install_only.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${TARBALL}"

mkdir -p "$CACHE_DIR"
if [[ ! -f "$CACHE_DIR/$TARBALL" ]]; then
  echo "Downloading $URL"
  curl -fL --retry 3 -o "$CACHE_DIR/$TARBALL.partial" "$URL"
  mv "$CACHE_DIR/$TARBALL.partial" "$CACHE_DIR/$TARBALL"
fi

# Supply-chain check: python-build-standalone publishes SHA256 digests.
# Set PBS_SHA256 to pin; unset skips with a warning.
if [[ -n "${PBS_SHA256:-}" ]]; then
  echo "${PBS_SHA256}  $CACHE_DIR/$TARBALL" | shasum -a 256 -c -
else
  echo "WARNING: PBS_SHA256 not set; tarball checksum not verified." >&2
fi

rm -rf "$RUNTIME_DIR"
EXTRACT_DIR="$(mktemp -d)"
tar -xzf "$CACHE_DIR/$TARBALL" -C "$EXTRACT_DIR"
mv "$EXTRACT_DIR/python" "$RUNTIME_DIR"
rmdir "$EXTRACT_DIR"

if [[ "$PLATFORM" == "windows-x64" ]]; then
  PYTHON="$RUNTIME_DIR/python.exe"
else
  PYTHON="$RUNTIME_DIR/bin/python3"
fi

echo "Installing airelays into the embedded runtime"
"$PYTHON" -m pip install --quiet --no-cache-dir "$REPO_ROOT"

# The relay is a headless server: tkinter and its bundled Tcl/Tk are dead
# weight in every installer, and their shared objects reference Tcl/Tk
# libraries that AppImage tooling cannot resolve on build runners
# ("Could not find dependency: libtcl9.0.so"). Remove the GUI stack from
# both the unix (lib/pythonX.Y) and windows (Lib, DLLs) layouts.
echo "Removing tkinter/Tcl/Tk from the embedded runtime"
for stdlib in "$RUNTIME_DIR"/lib/python*/ "$RUNTIME_DIR/Lib/"; do
  [[ -d "$stdlib" ]] || continue
  rm -rf "$stdlib/tkinter" "$stdlib/idlelib" "$stdlib/turtledemo"
  rm -f "$stdlib/turtle.py"
  rm -f "$stdlib"/lib-dynload/_tkinter*.so
done
rm -rf "$RUNTIME_DIR"/lib/tcl* "$RUNTIME_DIR"/lib/tk* "$RUNTIME_DIR"/lib/itcl* \
  "$RUNTIME_DIR"/lib/thread* "$RUNTIME_DIR"/lib/sqlite3-tcl* "$RUNTIME_DIR/tcl"
rm -f "$RUNTIME_DIR"/lib/libtcl* "$RUNTIME_DIR"/lib/libtk* \
  "$RUNTIME_DIR"/DLLs/_tkinter.pyd "$RUNTIME_DIR"/DLLs/tcl*.dll "$RUNTIME_DIR"/DLLs/tk*.dll

# Console-script shebangs embed absolute build paths (leaking CI paths and
# breaking relocation); the app always invokes `python -m airelays`, so all
# entry-point scripts are dead weight. Keep only the interpreter binaries.
if [[ "$PLATFORM" == "windows-x64" ]]; then
  rm -rf "$RUNTIME_DIR/Scripts"
else
  find "$RUNTIME_DIR/bin" -type f ! -name 'python*' -delete
  find "$RUNTIME_DIR/bin" -type l ! -name 'python*' -delete
fi

echo "Precompiling bytecode (hash-based: resource copying does not preserve mtimes)"
"$PYTHON" -m compileall -qq --invalidation-mode unchecked-hash \
  "$RUNTIME_DIR/lib" "$RUNTIME_DIR/Lib" 2>/dev/null || true

echo "Embedded runtime ready: $RUNTIME_DIR ($PLATFORM)"
