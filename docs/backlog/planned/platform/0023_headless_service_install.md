# Planned: Headless service install

## Metadata

- Created: 2026-10-11
- Status: Planned (P2)

## Summary

`airelays service install|uninstall` registers the relay as a per-user
service on each OS, recording the absolute interpreter path and a PATH
snapshot, plus a Windows headless installer.

## Scope

- macOS: per-user LaunchAgent in the `gui/<uid>` domain, or `user/<uid>`
  when no GUI session exists (Claude browser profiles then need a setup
  token); never a LaunchDaemon.
- Linux: `systemd --user` unit with `Restart=on-failure`; document
  `loginctl enable-linger`; without systemd (containers) print foreground
  and `AIRELAYS_DATA_DIR` guidance, and document `--host 0.0.0.0` with bearer
  auth for published container ports.
- Windows: a logon scheduled task; `scripts/install-headless.ps1`.
- `airelays uninstall --purge`: data directory, autostart entries, and
  Claude profile sign-outs.

## Validation

- Install/uninstall smoke per OS in the installers workflow.
