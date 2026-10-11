# Cross-platform runtime backlog track

## Status

Planned

## Purpose

Remaining work from the cross-OS strategy recorded in
[ADR 0008](../../../adr/0008-uniform-cross-platform-runtime.md): the same
lifecycle, service and packaging behavior on macOS, Linux and Windows, in CLI
and tray modes. The first slice (private credential files, one `claude`
launcher, isolated bundled interpreter, valid macOS ad-hoc signing, tests on
all three OSes) shipped in 0.16.1.

## Items

1. [0022_relay_lifecycle_and_single_instance_tray.md](0022_relay_lifecycle_and_single_instance_tray.md): graceful shutdown, no orphaned relays, one relay per data directory, one tray instance (P1)
2. [0023_headless_service_install.md](0023_headless_service_install.md): `airelays service install` per OS and a Windows headless installer (P2)
3. [0024_signed_installers.md](0024_signed_installers.md): Developer ID notarization on macOS and Authenticode on Windows (P2)

## Order

0022 first: it removes orphaned relays and gives Windows its only graceful
shutdown path. 0023 and 0024 are independent.

## Related material

- [ADR 0003](../../../adr/0003-local-config-and-default-deny-relay-security.md)
- [ADR 0007](../../../adr/0007-relay-owned-claude-accounts.md)
- [ADR 0008](../../../adr/0008-uniform-cross-platform-runtime.md)
- [Proposed 0014: wall-clock bench expiry](../../proposed/accounts/0014_wall_clock_bench_expiry_across_system_sleep.md)
