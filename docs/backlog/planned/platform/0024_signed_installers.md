# Planned: Signed installers

## Metadata

- Created: 2026-10-11
- Status: Planned (P2)

## Summary

Developer ID signing with hardened runtime, notarization and stapling on
macOS (every binary under `Resources/runtime` signed inside-out), and
Authenticode signing on Windows, so installers open without security
warnings.

## Notes

- Until then the macOS app carries a valid ad-hoc signature (CI checks
  `codesign --verify --deep --strict`; `spctl` rejects ad-hoc apps by
  design) and users open it through Privacy & Security, Open Anyway.
- Re-signing changes the bundled interpreter's identity; harmless now that
  AIRelays stores no secrets in the keychain.
- After notarization, `SMAppService` can replace the autostart LaunchAgent.
