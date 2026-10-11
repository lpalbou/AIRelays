# Proposed: Import OpenAI sign-ins saved in the OS keyring

## Metadata

- Created: 2026-10-11
- Status: Proposed (deferred by the owner)

## Summary

Offer a one-time, user-initiated import of OpenAI sign-ins that AIRelays
0.16.0 and earlier stored in the OS keyring, so upgraded users keep their
accounts without signing in again.

## Reason

ADR 0008 makes private files the only credential store. Sign-ins an older
version kept in the keyring are reported by `airelays doctor` and need a new
sign-in. The owner chose not to ship an import system for 0.16.1 to keep the
product simple; a one-off script covered the owner's own migration.

## Constraints from the cross-OS review

- Reading the keyring can prompt (macOS ties items to the interpreter that
  last wrote them) or block (a locked Linux collection has no unlock
  timeout), so an import must run only on explicit user action, in a
  separate process with a hard timeout, never on the relay's event loop.
- Linux: check `is_locked()` through Secret Service and skip a locked
  collection instead of prompting.
- Only import roots without an `auth.json`; the file is always newer.
- Windows has nothing to import (the keyring writes never succeeded).
- Would re-add `keyring` as a dependency for the import window only.

## Promotion criteria

Evidence that upgraded users lose accounts in practice and that signing in
again is a real burden.
