# Planned: Relay lifecycle and single-instance tray

## Metadata

- Created: 2026-10-11
- Status: Planned (P1)

## Summary

One shutdown protocol on every OS, no orphaned relays, one relay per data
directory, and one tray instance.

## Progress

- Done in 0.17.1: `tauri-plugin-single-instance` (a second launch focuses the
  running app's dashboard; verified on macOS with `open -n` and a direct
  binary launch) and the per-data-directory relay lock (`data_dir/relay.lock`,
  informational `relay.pid`, through `private_io.InterProcessLock`). A relay
  refused by the lock exits with status 75; the tray drops its intent to run,
  notifies once and does not restart it. Login-item launches pass
  `--autostart`, so a second instance started by the login item does not
  open the dashboard (existing login items are re-registered at startup).
- To verify on Windows and Linux: one tray after a second launch and after a
  login-item launch, and the status-75 handling.
- Remaining: everything else below (lifeline stdin, tray handling of a relay
  managed elsewhere, installer `--quit`, Linux tray detection, Windows port
  1455, macOS PATH fallback, config rewrite).

## Scope

- `--lifeline-stdin` (passed only by the tray): the relay reads stdin in a
  daemon thread and shuts down gracefully on EOF; SIGTERM takes the same
  path. Replace `uvicorn.run` with a `Server` whose `should_exit` the
  lifeline sets; graceful timeout about 3 s; the relay ends its own `claude`
  children. Services (systemd, launchd, Task Scheduler, nohup) give an empty
  stdin, so the flag must stay opt-in.
- The tray signals only the relay; process-group SIGKILL (Unix) or the Job
  Object (Windows) remain backstops after at least 10 s. On Windows a tray
  crash still kills the relay at once (KILL_ON_JOB_CLOSE); accepted.
- `data_dir/relay.lock`: exclusive, non-blocking lock (msvcrt `LK_NBLCK` at
  a far offset on Windows; pid kept in a separate file, informational only).
  The tray reports "relay managed elsewhere (pid N)" instead of fighting it.
- `tauri-plugin-single-instance` on all OSes, with a `--quit` path used by
  installers instead of force-killing.
- Linux: detect `org.kde.StatusNotifierWatcher` before hiding the window
  (GNOME without AppIndicator has no tray); fix the autostart `Exec` quoting
  and the extract-and-run prefix; drop the stale libfuse2 check.
- Windows: exclusive bind for the login callback port 1455 and a clear
  message for WinError 10013.
- macOS: PATH discovery through an interactive login shell as a fallback
  only (markers, 10 s timeout, kill the process group); `doctor` reports a
  non-GUI session (`launchctl managername`, SSH variables, enrolled Claude
  profiles reporting signed out) without touching the keychain.
- Wall-clock reset deadlines for benches and cooldowns (proposed item 0014).
- The tray rewrites `config.toml` on every relay start and drops keys it
  does not model (for example `[providers.claude].command`). Edit only the
  keys the user changed (`toml_edit`), or model `claude_command` in the tray
  settings; until then desktop users set `AIRELAYS_CLAUDE_COMMAND`.

## Validation

- Lifecycle tests per OS: kill the tray, assert the relay exits (graceful on
  Unix, job kill on Windows); a second relay on the same data dir is refused.
- systemd/launchd/Task Scheduler smoke: the relay keeps running with an
  empty stdin.
