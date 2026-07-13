//! System tray: state icon and the control menu.
//!
//! The tray is the primary control surface on all three platforms. State is
//! shown by shape and color on every OS: a glowing green bolt with relay
//! arcs when the relay answers, a red bolt alone when it does not (the
//! shape difference keeps the state readable for colorblind users).
//! Request activity plays a short pulse animation: the bolt swells into a
//! brighter green with a ripple ring, then eases back to the state icon.
//!
//! Security toggles (auth mode, LAN exposure) intentionally live only in
//! the dashboard, where their consequences are explained — not one
//! accidental menu click away.

use crate::relay::robust_lock;
use crate::state::AppState;
use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::OnceLock;
use std::time::Instant;
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
// Pulse animation frames occupy ids BASE..BASE+FRAME_COUNT, so the
// per-frame dedupe in apply_icon works unchanged.
const ICON_PULSE_BASE: u8 = 3;
static APPLIED_ICON: AtomicU8 = AtomicU8::new(ICON_NONE);

/// Activity pulse: a fast-attack / slow-decay glow swell with a ripple
/// ring, pre-rendered by scripts/make_tray_icons.swift. The total runtime
/// (~1.1s) is deliberately below the 1.5s status-loop tick, so a pulse
/// started on one tick always finishes before the next tick syncs.
const PULSE_FRAME_COUNT: usize = 16;
const PULSE_FRAME_MS: u64 = 70;
static PULSE_FRAMES: [&[u8]; PULSE_FRAME_COUNT] = [
    include_bytes!("../icons/tray-pulse-00.png"),
    include_bytes!("../icons/tray-pulse-01.png"),
    include_bytes!("../icons/tray-pulse-02.png"),
    include_bytes!("../icons/tray-pulse-03.png"),
    include_bytes!("../icons/tray-pulse-04.png"),
    include_bytes!("../icons/tray-pulse-05.png"),
    include_bytes!("../icons/tray-pulse-06.png"),
    include_bytes!("../icons/tray-pulse-07.png"),
    include_bytes!("../icons/tray-pulse-08.png"),
    include_bytes!("../icons/tray-pulse-09.png"),
    include_bytes!("../icons/tray-pulse-10.png"),
    include_bytes!("../icons/tray-pulse-11.png"),
    include_bytes!("../icons/tray-pulse-12.png"),
    include_bytes!("../icons/tray-pulse-13.png"),
    include_bytes!("../icons/tray-pulse-14.png"),
    include_bytes!("../icons/tray-pulse-15.png"),
];

/// Monotonically increasing pulse id: a re-trigger bumps it and the
/// running animation task notices and stands down, so overlapping pulses
/// restart the animation instead of interleaving frames.
static PULSE_GENERATION: AtomicU64 = AtomicU64::new(0);
/// Monotonic time (ms) until which a pulse animation owns the icon.
/// sync_icon defers to it instead of stomping mid-animation frames — and
/// because the deadline expires on its own, the every-tick self-healing
/// resumes even if an animation task never runs to completion.
static PULSE_DEADLINE_MS: AtomicU64 = AtomicU64::new(0);

/// Milliseconds since the first call (process-relative monotonic clock):
/// immune to wall-clock jumps, and 0 naturally means "no pulse running".
fn monotonic_ms() -> u64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_millis() as u64
}

pub fn init(app: &AppHandle) -> tauri::Result<()> {
    let tray = TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon_image(ICON_DISCONNECTED))
        .tooltip(format!("AIRelays {}", app.package_info().version))
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
/// While a pulse animation is in flight it yields (the animation re-syncs
/// state itself when it finishes, at most ~1.1s later).
pub fn sync_icon(app: &AppHandle) {
    if monotonic_ms() < PULSE_DEADLINE_MS.load(Ordering::Relaxed) {
        return;
    }
    let desired = if app.state::<AppState>().is_reachable() {
        ICON_CONNECTED
    } else {
        ICON_DISCONNECTED
    };
    apply_icon(app, desired);
}

/// One activity pulse: the glyph swells bright with a ripple ring, then
/// eases back to the state icon (~1.1s). A re-trigger while animating
/// restarts the pulse from the attack instead of interleaving two runs.
pub fn pulse(app: &AppHandle) {
    let generation = PULSE_GENERATION.fetch_add(1, Ordering::Relaxed) + 1;
    let total_ms = PULSE_FRAME_MS * PULSE_FRAME_COUNT as u64;
    PULSE_DEADLINE_MS.store(monotonic_ms() + total_ms, Ordering::Relaxed);
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        for frame in 0..PULSE_FRAME_COUNT {
            // A newer pulse owns the tray now; it will restore state.
            if PULSE_GENERATION.load(Ordering::Relaxed) != generation {
                return;
            }
            apply_icon(&app, ICON_PULSE_BASE + frame as u8);
            tokio::time::sleep(std::time::Duration::from_millis(PULSE_FRAME_MS)).await;
        }
        if PULSE_GENERATION.load(Ordering::Relaxed) != generation {
            return;
        }
        // Force a re-apply of the real state (pulse is never "desired").
        PULSE_DEADLINE_MS.store(0, Ordering::Relaxed);
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
    // connected, red when not, a bright green pulse on request activity.
    let bytes: &[u8] = match kind {
        ICON_CONNECTED => include_bytes!("../icons/tray-connected.png"),
        kind if kind >= ICON_PULSE_BASE
            && usize::from(kind - ICON_PULSE_BASE) < PULSE_FRAME_COUNT =>
        {
            PULSE_FRAMES[usize::from(kind - ICON_PULSE_BASE)]
        }
        _ => include_bytes!("../icons/tray-disconnected.png"),
    };
    tauri::image::Image::from_bytes(bytes).expect("embedded tray icon")
}

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let state = app.state::<AppState>();
    let reachable = state.is_reachable();
    let managed = state.supervisor.is_managed();
    let auth_mismatch = *robust_lock(&state.auth_mismatch);
    let lifecycle = *robust_lock(&state.supervisor.lifecycle);
    let endpoint = robust_lock(&state.settings).base_url();

    // Same decision order as the dashboard hero and sidebar: the three
    // surfaces must never contradict each other for one underlying state.
    let status_text = if reachable {
        "Running"
    } else if auth_mismatch {
        "Running — key mismatch"
    } else if lifecycle == crate::relay::Lifecycle::Starting {
        "Starting…"
    } else if lifecycle == crate::relay::Lifecycle::Stopping {
        "Stopping…"
    } else if lifecycle == crate::relay::Lifecycle::Failed {
        "Failed — see Console"
    } else if managed {
        "Running — not responding"
    } else {
        "Stopped"
    };
    let status_line = MenuItem::with_id(
        app,
        "status",
        format!("{status_text} — {endpoint}"),
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
