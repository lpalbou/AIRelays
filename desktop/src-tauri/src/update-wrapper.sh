#!/bin/bash
# Runs an AIRelays desktop update. Written and started by the app
# (src/updates.rs), which passes everything through AIRELAYS_* variables so
# no path or version is ever spliced into this script.
#
# Waits for the app to exit, runs the release installer pinned to the
# approved version and SHA-256, then reopens AIRelays whether the update
# succeeded or not. A failure is recorded for the app to report.

exec >"$AIRELAYS_UPDATE_LOG" 2>&1 || exit 1
echo "AIRelays update to $AIRELAYS_VERSION started $(date)"
# Tells the app the updater is running, so it may quit.
echo $$ >"$AIRELAYS_UPDATE_READY"

fail() {
  echo "Update failed: $1"
  printf '%s\n' "$1" >"$AIRELAYS_UPDATE_FAILED"
}

for _ in $(seq 180); do kill -0 "$AIRELAYS_UPDATE_PID" 2>/dev/null || break; sleep 0.5; done
if kill -0 "$AIRELAYS_UPDATE_PID" 2>/dev/null; then
  fail "AIRelays did not close in time, so the update was cancelled."
  exit 1
fi

app="$AIRELAYS_UPDATE_RELAUNCH"
# This wrapper reopens the app itself, after success or failure.
export AIRELAYS_NO_LAUNCH=1
if bash "$AIRELAYS_UPDATE_INSTALLER"; then
  installed=""
  if [[ "$(uname -s)" == Darwin ]]; then
    # The installed bundle is the only reliable verdict.
    installed="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist" 2>/dev/null)"
  fi
  if [[ -n "$installed" && "$installed" != "$AIRELAYS_VERSION" ]]; then
    fail "the installed app reports version $installed instead of $AIRELAYS_VERSION."
  else
    echo "Update finished."
  fi
else
  reason="$(grep '^error:' "$AIRELAYS_UPDATE_LOG" | tail -n 1)"
  fail "${reason:-the installer failed.}"
fi

# The reopened app must not inherit this update's settings.
for name in $(compgen -e | grep '^AIRELAYS_UPDATE_'); do unset "$name"; done
unset AIRELAYS_NO_LAUNCH AIRELAYS_SHA256 AIRELAYS_VERSION AIRELAYS_APP_DIR
case "$(uname -s)" in
  Darwin) open "$app" --args --after-update || echo "Could not reopen AIRelays." ;;
  *)
    # Same rule as the installer's launcher entry: without FUSE 2 an
    # AppImage only runs extracted.
    if ! { ldconfig -p 2>/dev/null || /sbin/ldconfig -p 2>/dev/null || true; } | grep -q 'libfuse\.so\.2'; then
      export APPIMAGE_EXTRACT_AND_RUN=1
    fi
    nohup "$app" --after-update >/dev/null 2>&1 &
    ;;
esac
