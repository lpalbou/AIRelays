# Completed: Uniform cross-platform runtime, first slice

## Metadata

- Created: 2026-10-11
- Status: Completed
- Completed: 2026-10-11

## Context

Three adversarial reviews (one per OS) of AIRelays in CLI and tray modes found
that several mechanisms only worked on some platforms: OpenAI sign-ins kept
in the OS keyring could not be saved on Windows (Credential Manager's size
limit), prompted on macOS for every other interpreter, and could block the
relay on a locked Linux keyring; `claude` children received a POSIX-shaped
environment that breaks on Windows, put system prompts in the process list,
and failed on argument limits; `airelays init` wrote unparsable TOML with
Windows paths; data directories were readable by other local users; the
macOS app's signature was invalid; tests ran on Linux only.

## What we did

- [ADR 0008](../../../adr/0008-uniform-cross-platform-runtime.md): one
  mechanism per concern on every OS, per-OS behavior as data.
- `airelay.private_io`: owner-only directories, atomic owner-only files, and
  a sidecar inter-process lock (retries only on contention; unsupported
  filesystems refresh with a warning). OpenAI auth is file-only; refreshes
  are serialized across processes in a shielded task; the `keyring`
  dependency is removed; keyring-era sign-ins are reported (doctor, relay
  status, dashboard) without reading the keyring.
- `airelay.claude_launch`: one launcher for inference, control requests,
  probes and sign-in: resolution without pinning versioned paths, per-OS
  environment allowlists, `--system-prompt-file`, argument-limit 413s,
  console-free Windows children, probes off the event loop;
  `[providers.claude].command` / `AIRELAYS_CLAUDE_COMMAND`.
- Desktop: one `configure_child()` for every child (AppImage variables
  stripped by exact name, isolated `-I -B -u -X utf8` interpreter), private
  writes in Rust, storage selector removed; valid ad-hoc macOS signature
  with a strict CI gate.
- Config TOML escaping; log retention works on Windows (`os.lstat`).
- CI: pytest on Ubuntu, macOS and Windows × 3.11–3.13; desktop crate tests
  on all three OSes (compile-only on Windows).

## Validation

- `pytest`: 444 passed on Python 3.11, 3.12 and 3.13 (macOS host); desktop
  `cargo test` 15 passed; 15 desktop browser tests; node usage tests; MkDocs,
  build and `twine check` (twine 7) green.
- Final cross-review by the three OS reviewers: no remaining blockers.

## Follow-ups

- Planned track: [cross-platform runtime](../../planned/platform/README.md)
  (0022 lifecycle and single-instance tray, 0023 service install, 0024 signed
  installers).
- Proposed [0021](../../proposed/accounts/0021_keyring_sign_in_import.md):
  optional import of keyring-era sign-ins (deferred).
- The first three-OS CI run surfaced two pre-existing Windows bugs (upload
  rename while the temp file was open; ~15.6 ms `time.monotonic()` ties in
  account selection before Python 3.13), fixed in 0.16.2.
