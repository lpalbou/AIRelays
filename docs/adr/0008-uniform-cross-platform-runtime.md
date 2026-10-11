# 0008: Uniform Cross-Platform Runtime

## Status

Accepted, 2026-10-11. Complements [ADR 0003](0003-local-config-and-default-deny-relay-security.md)
and [ADR 0007](0007-relay-owned-claude-accounts.md).

## Context

AIRelays runs on macOS, Linux and Windows, as a headless CLI relay and as a
tray app that supervises the same relay. Several mechanisms behaved
differently per OS in ways users could not see:

- OpenAI sign-ins were stored in the OS keyring first and a file second.
  Windows Credential Manager cannot hold an OpenAI token set (its blob limit is
  smaller than the record), macOS ties each keychain item to the interpreter
  that last wrote it and prompts any other one, and a locked Linux keyring can
  block the relay. Two stores also let a rotated refresh token diverge.
- `claude` child processes were started with a POSIX-shaped environment and
  argv, which breaks name resolution and launches on Windows, puts system
  prompts in the process list, and fails on argument-size limits.
- Data directories were created with default permissions.

## Decision

- **One mechanism per concern, on every OS.** Per-OS behavior is expressed as
  data (path lists, environment names, process flags) behind one abstraction,
  and is the same in CLI and tray modes.
- **Credentials are private files.** OpenAI sign-ins are stored only as files
  under the data directory: directories AIRelays creates are owner-only
  (0700), files are created owner-only (0600) and replaced atomically, and a
  separate lock file serializes refresh-then-save across processes. On Windows
  protection comes from the user profile's ACL. There is no keyring storage
  mode and no fallback between stores. AIRelays does not read the OS keyring;
  sign-ins an older version stored there are reported as needing a new
  sign-in. Claude credentials stay owned by Claude Code (ADR 0007).
- **One launcher for `claude`.** An explicit `claude_command` wins; otherwise
  the executable is resolved at relay start (and again after a failed launch)
  from the inherited PATH and known install locations, never pinned to a
  versioned path. Windows uses a real executable and refuses `.cmd`/`.bat`
  shims. The child PATH starts with the directory where `claude` was found.
  The child environment comes from one allowlist with per-OS additions,
  matched case-insensitively only on Windows. Large or sensitive inputs leave
  the command line where the CLI supports it; requests that still exceed the
  OS argument limit are rejected with 413. Windows children start without a
  console window.
- **The bundled interpreter is isolated** (`-I -B -u -X utf8`) whenever the
  tray starts the relay or a CLI command.
- **Packaging is valid by default.** The macOS bundle carries a valid ad-hoc
  signature checked in CI until it is Developer ID signed and notarized;
  Windows installers remain unsigned until code signing is added.
- **Tests run on all three OSes** in CI.
- **Next, in this order:** an opt-in stdin lifeline for graceful relay
  shutdown and orphan prevention, one relay per data directory, a
  single-instance tray, and per-user service installation for headless use.

## Alternatives

Keeping the keyring as an opt-in store would preserve encryption at rest on
macOS and some Linux desktops, but keeps per-OS failure modes, prompts and
hangs that a relay cannot recover from on its own. An automatic keyring
import would still need to read the keyring, which can prompt or block; a new
sign-in is simpler and explicit.

## Consequences

Sign-ins behave the same on every OS and in every mode, and no keyring prompt
or lock can stall the relay. Tokens are protected by file permissions and disk
encryption rather than the OS keyring. OpenAI accounts that an older version
stored in the keyring must sign in again once.
