# Completed: Tray address submenu and left click opens the dashboard

## Metadata

- Created: 2026-10-25
- Status: Completed
- Completed: 2026-10-25

## Context

The tray menu showed only the local endpoint in its status line; finding the
address to type on another device meant opening the dashboard. On macOS a
left click on the tray icon opened the menu instead of the dashboard, even
though the tray was built with `show_menu_on_left_click(false)` and handled
`TrayIconEvent::Click` for the left button.

Root cause: macOS 27 stopped delivering clicks to tray-icon's overlay view
while an `NSMenu` is attached to the `NSStatusItem`; AppKit opens the
attached menu on any click, so the left-click handler never ran.
tray-icon 0.24.1 (pulled in by Tauri 2.11.5) attaches the menu permanently
(`setMenu` at creation and in every `set_menu`). Upstream fixed it in
tray-icon 0.25.1 (tauri-apps/tray-icon#365, closing #355), which attaches the
menu only while showing it; Tauri 2.12 is the first line that depends on it.

## What we did

- Tray (`desktop/src-tauri/src/tray.rs`): on macOS the status item carries no
  menu. A right click attaches the current menu, pops it up
  (`show_menu`, modal) and detaches it again, one main-loop turn after the
  click so Tauri's tray-listener lock is not held during the menu's nested
  event loop. Left click opens the dashboard on press or release on macOS,
  on release elsewhere. Windows keeps its native right-click menu; Linux
  (appindicator, no click events) keeps the menu with **Open Dashboard**
  first. The workaround also works unchanged after a move to Tauri 2.12.
- **Copy Address** submenu: the local endpoint, then one entry per
  private-network address (the dashboard's LAN list, shared through
  `commands::lan_endpoints`), plus a note about the router's public address
  when internet access is on. Choosing an entry copies the URL to the
  clipboard (`arboard`, one handle for the process lifetime so X11 keeps
  serving it) and confirms with a notification and a console line.
- The status loop compares the address list every tick (interface
  enumeration stays cached for 10 s) and rebuilds the menu when Wi-Fi or
  settings change it.
- Hide-on-blur: a window shown in the last 600 ms is never hidden by the
  blur the tray click itself caused, so a left click always ends with the
  dashboard visible and focused, never toggled away.

## Validation

- `cargo check` and `cargo test` (desktop crate): new tests for the click
  mapping and the address list (local first, no duplicates); hide-on-blur
  test extended with the grace window.
- Not verifiable here: real tray clicks need a packaged app on macOS 27,
  Windows and Linux. The non-macOS branches were reviewed but not compiled
  (cross-checking for Windows needs the Windows SDK for `ring`).

## Follow-ups

- Move to Tauri 2.12 (tray-icon 0.25.1, wry 0.57, tao 0.37) in its own
  change; the macOS workaround can then be dropped or kept as harmless.
