//! System tray: state icon and the control menu.
//!
//! The tray is the primary control surface on all three platforms. State is
//! shown by shape and color on every OS: a glowing green bolt with relay
//! arcs when the relay answers, a red bolt alone when it does not (the
//! shape difference keeps the state readable for colorblind users).
//!
//! Security toggles (auth mode, LAN exposure) intentionally live only in
//! the dashboard, where their consequences are explained — not one
//! accidental menu click away.

use crate::relay::robust_lock;
use crate::state::AppState;
use std::sync::atomic::{AtomicU8, Ordering};
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager};

const TRAY_ID: &str = "airelays-tray";

// Last icon actually applied to the tray (0 = none yet). A missed or failed
// set_icon used to stick until the next reachability *change*; tracking the
// applied state lets the status loop re-assert it cheaply on every tick.
const ICON_NONE: u8 = 0;
const ICON_DISCONNECTED: u8 = 1;
const ICON_CONNECTED: u8 = 2;
const ICON_PULSE: u8 = 3;
static APPLIED_ICON: AtomicU8 = AtomicU8::new(ICON_NONE);

pub fn init(app: &AppHandle) -> tauri::Result<()> {
    let tray = TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon_image(ICON_DISCONNECTED))
        .tooltip("AIRelays")
        .menu(&build_menu(app)?)
        .show_menu_on_left_click(true)
        .on_menu_event(handle_menu_event)
        .build(app)?;
    let _ = tray;
    APPLIED_ICON.store(ICON_DISCONNECTED, Ordering::Relaxed);
    Ok(())
}

/// Rebuilds menu and icon to match current state. Called on lifecycle and
/// reachability changes.
pub fn refresh(app: &AppHandle) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        if let Ok(menu) = build_menu(app) {
            let _ = tray.set_menu(Some(menu));
        }
    }
    sync_icon(app);
}

/// Re-asserts the tray icon from current reachability. No-op when already
/// correct, so the status loop calls it every tick — any missed update
/// self-heals within one poll instead of sticking until the next change.
pub fn sync_icon(app: &AppHandle) {
    let desired = if app.state::<AppState>().is_reachable() {
        ICON_CONNECTED
    } else {
        ICON_DISCONNECTED
    };
    apply_icon(app, desired);
}

/// One short activity blink: bright bolt, then back to the state icon.
/// Overlapping pulses collapse into one (the revert re-syncs from state).
pub fn pulse(app: &AppHandle) {
    if APPLIED_ICON.load(Ordering::Relaxed) == ICON_PULSE {
        return;
    }
    apply_icon(app, ICON_PULSE);
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(300)).await;
        // Force a re-apply of the real state (pulse is never "desired").
        APPLIED_ICON.store(ICON_NONE, Ordering::Relaxed);
        sync_icon(&app);
    });
}

fn apply_icon(app: &AppHandle, desired: u8) {
    if APPLIED_ICON.load(Ordering::Relaxed) == desired {
        return;
    }
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        if tray.set_icon(Some(icon_image(desired))).is_ok() {
            APPLIED_ICON.store(desired, Ordering::Relaxed);
        }
    }
}

fn icon_image(kind: u8) -> tauri::image::Image<'static> {
    // Compiled in: no filesystem dependency, identical behavior in dev and
    // bundled builds. Color-coded on every platform: glowing green when
    // connected, red when not, bright flash on request activity.
    let bytes: &[u8] = match kind {
        ICON_CONNECTED => include_bytes!("../icons/tray-connected.png"),
        ICON_PULSE => include_bytes!("../icons/tray-pulse.png"),
        _ => include_bytes!("../icons/tray-disconnected.png"),
    };
    tauri::image::Image::from_bytes(bytes).expect("embedded tray icon")
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let state = app.state::<AppState>();
    let reachable = state.is_reachable();
    let managed = state.supervisor.is_managed();
    let endpoint = robust_lock(&state.settings).base_url();

    let status_line = MenuItem::with_id(
        app,
        "status",
        format!(
            "{} — {}",
            if reachable {
                "Running"
            } else if managed {
                "Starting"
            } else {
                "Stopped"
            },
            endpoint
        ),
        false,
        None::<&str>,
    )?;
    let open = MenuItem::with_id(app, "open-dashboard", "Open Dashboard", true, None::<&str>)?;
    let start = MenuItem::with_id(app, "start", "Start Relay", !managed, None::<&str>)?;
    let stop = MenuItem::with_id(app, "stop", "Stop Relay", managed, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart", "Restart Relay", managed, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit AIRelays", true, None::<&str>)?;

    Menu::with_items(
        app,
        &[
            &status_line,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &PredefinedMenuItem::separator(app)?,
            &start,
            &stop,
            &restart,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )
}

fn handle_menu_event(app: &AppHandle, event: tauri::menu::MenuEvent) {
    let app = app.clone();
    match event.id().as_ref() {
        "open-dashboard" => crate::commands::show_dashboard(&app),
        // Process control can block for seconds; never run it on the UI
        // thread the menu event arrives on.
        "start" => {
            tauri::async_runtime::spawn_blocking(move || {
                let _ = crate::commands::start_relay_blocking(&app);
            });
        }
        "stop" => {
            tauri::async_runtime::spawn_blocking(move || {
                crate::commands::stop_relay_blocking(&app);
            });
        }
        "restart" => {
            tauri::async_runtime::spawn_blocking(move || {
                crate::commands::stop_relay_blocking(&app);
                let _ = crate::commands::start_relay_blocking(&app);
            });
        }
        "quit" => {
            tauri::async_runtime::spawn_blocking(move || {
                app.state::<AppState>().supervisor.stop();
                app.exit(0);
            });
        }
        _ => {}
    }
}
