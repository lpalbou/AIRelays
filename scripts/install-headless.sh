#!/usr/bin/env bash
# Install or update the AIRelays relay and `airelays` CLI (no GUI) from PyPI.
#
#   curl -fsSL https://raw.githubusercontent.com/lpalbou/AIRelays/main/scripts/install-headless.sh | bash
#
# For servers, SSH sessions, Intel Macs, and terminal-first use on macOS or
# Linux. No sudo. The relay is installed into its own isolated environment:
#   1. with uv (`uv tool install`) when uv is available;
#   2. otherwise into a virtualenv at ~/.local/share/airelays/venv when
#      Python 3.11+ is available, linked as ~/.local/bin/airelays;
#   3. otherwise uv is installed first (user-local, from astral.sh) and it
#      provides a suitable Python.
#
# Environment:
#   AIRELAYS_VERSION=0.14.1  install that release instead of the newest one
set -euo pipefail

REPO="lpalbou/AIRelays"
BIN_DIR="$HOME/.local/bin"

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) die "this script supports macOS and Linux. On Windows, run: py -m pip install airelays" ;;
esac

SPEC="airelays"
if [[ -n "${AIRELAYS_VERSION:-}" ]]; then SPEC="airelays==${AIRELAYS_VERSION#v}"; fi

find_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null \
      && "$candidate" -c 'import sys, venv; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

install_with_uv() {
  say "Installing $SPEC with uv"
  # --force replaces an existing install, so the same command also upgrades.
  "$1" tool install --force --python '>=3.11' "$SPEC"
}

install_with_venv() {
  local venv="${XDG_DATA_HOME:-$HOME/.local/share}/airelays/venv"
  say "Installing $SPEC into $venv (Python: $1)"
  "$1" -m venv "$venv"
  "$venv/bin/python" -m pip install --quiet --upgrade pip
  "$venv/bin/python" -m pip install --quiet --upgrade "$SPEC"
  mkdir -p "$BIN_DIR"
  ln -sf "$venv/bin/airelays" "$BIN_DIR/airelays"
}

UV="$(command -v uv || true)"
if [[ -n "$UV" ]]; then
  install_with_uv "$UV"
elif PYTHON="$(find_python)"; then
  install_with_venv "$PYTHON"
else
  say "Neither uv nor Python 3.11+ found; installing uv (https://docs.astral.sh/uv/)"
  command -v curl >/dev/null || die "curl is required."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  UV="$BIN_DIR/uv"
  [[ -x "$UV" ]] || UV="$(command -v uv)" || die "uv installation failed."
  install_with_uv "$UV"
fi

AIRELAYS="$BIN_DIR/airelays"
[[ -x "$AIRELAYS" ]] || AIRELAYS="$(command -v airelays)" || die "airelays was installed but not found on PATH."
"$AIRELAYS" --help >/dev/null || die "the installed airelays command failed to run."

VERSION="$("$(dirname "$(readlink -f "$AIRELAYS" 2>/dev/null || echo "$AIRELAYS")")/python" \
  -c 'import airelay; print(airelay.__version__)' 2>/dev/null || true)"
say "Installed airelays ${VERSION:-} at $AIRELAYS"

OTHER="$(command -v airelays || true)"
if [[ -n "$OTHER" && "$OTHER" != "$AIRELAYS" ]]; then
  say "Warning: another airelays at $OTHER comes first on PATH; remove it or reorder PATH."
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Add $BIN_DIR to your PATH, e.g.: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile" ;;
esac

cat <<EOF

Next steps:
  airelays init
  airelays login            # add --device on SSH or without a browser
  airelays doctor
  airelays serve --port 8080

Claude models also need the claude CLI; see https://github.com/$REPO#quick-start
EOF
