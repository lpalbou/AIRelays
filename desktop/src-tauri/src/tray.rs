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
//!
//! Clicks: left click always brings the dashboard forward; right click
//! opens the menu. On Linux the appindicator host owns every click and
//! always shows the menu, which therefore starts with "Open Dashboard".
//!
//! macOS 27 stopped delivering clicks to tray-icon's overlay view while an
//! NSMenu is attached to the NSStatusItem: AppKit opens the attached menu
//! on any click, so `show_menu_on_left_click(false)` had no effect and the
//! left-click event never arrived. tray-icon 0.24 (Tauri 2.11) attaches the
//! menu permanently; 0.25.1 (Tauri 2.12) attaches it only while showing
//! it. Until the app moves to Tauri 2.12, the macOS tray carries no menu:
//! a right click attaches it, pops it up, and detaches it again.

use crate::relay::robust_lock;
use crate::state::AppState;
use std::sync::atomic::{AtomicU64, AtomicU8, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager};

pub(crate) const TRAY_ID: &str = "airelays-tray";

/// Menu item ids for the address entries are `copy-addr-<index>` into this
/// list, which is replaced together with the menu it was built for.
const COPY_ADDRESS_PREFIX: &str = "copy-addr-";
static MENU_ADDRESSES: Mutex<Vec<String>> = Mutex::new(Vec::new());

/// The current menu on macOS, attached to the status item only while it is
/// shown (see the module docs).
#[cfg(target_os = "macos")]
static MAC_MENU: Mutex<Option<Menu<tauri::Wry>>> = Mutex::new(None);

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
/// (~1s) is protected from status-loop updates by the animation deadline.
const PULSE_FRAME_COUNT: usize = 16;
const PULSE_FRAME_MS: u64 = 65;
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
    let menu = build_menu(app)?;
    let builder = TrayIconBuilder::with_id(TRAY_ID)
        .icon(icon_image(ICON_DISCONNECTED))
        .tooltip(format!("AIRelays {}", app.package_info().version))
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button, button_state, .. } = event {
                match click_action(button, button_state) {
                    ClickAction::ShowDashboard => crate::commands::show_dashboard(tray.app_handle()),
                    ClickAction::ShowMenu => show_menu_soon(tray.app_handle()),
                    ClickAction::None => {}
                }
            }
        })
        .on_menu_event(handle_menu_event);
    #[cfg(target_os = "macos")]
    let builder = {
        *robust_lock(&MAC_MENU) = Some(menu);
        builder
    };
    #[cfg(not(target_os = "macos"))]
    let builder = builder.menu(&menu);
    let tray = builder.build(app)?;
    let _ = tray;
    APPLIED_ICON.store(ICON_DISCONNECTED, Ordering::Relaxed);
    Ok(())
}

#[derive(Debug, PartialEq, Eq)]
enum ClickAction {
    ShowDashboard,
    ShowMenu,
    None,
}

/// What a tray click does. Left click opens the dashboard: on macOS on
/// press or release (the overlay view reports both; showing is
/// idempotent), elsewhere on release like native tray icons. Right click
/// opens the menu: macOS pops it up on press (see `show_menu_soon`);
/// Windows shows it natively on release; Linux never reports clicks.
fn click_action(button: MouseButton, state: MouseButtonState) -> ClickAction {
    match (button, state) {
        (MouseButton::Left, MouseButtonState::Up) => ClickAction::ShowDashboard,
        (MouseButton::Left, MouseButtonState::Down) if cfg!(target_os = "macos") => {
            ClickAction::ShowDashboard
        }
        (MouseButton::Right, MouseButtonState::Down) if cfg!(target_os = "macos") => {
            ClickAction::ShowMenu
        }
        _ => ClickAction::None,
    }
}

/// Pops the menu up on macOS (other platforms show it natively). Runs one
/// main-loop turn later: the tray event handler holds Tauri's listener
/// lock, and the menu runs a nested event loop until it closes.
fn show_menu_soon(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    {
        let app = app.clone();
        std::thread::spawn(move || {
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || pop_up_menu(&handle));
        });
    }
    #[cfg(not(target_os = "macos"))]
    let _ = app;
}

#[cfg(target_os = "macos")]
fn pop_up_menu(app: &AppHandle) {
    let Some(tray) = app.tray_by_id(TRAY_ID) else {
        return;
    };
    let menu = robust_lock(&MAC_MENU).clone();
    let Some(menu) = menu else {
        return;
    };
    if tray.set_menu(Some(menu)).is_ok() {
        // NSStatusBarButton.performClick: blocks until the menu closes.
        let _ = tray.with_inner_tray_icon(|inner| inner.show_menu());
    }
    // Detached again, so the next left click reaches the dashboard handler.
    let _ = tray.set_menu(None::<Menu<tauri::Wry>>);
}

/// Rebuilds menu and icon to match current state. Called on lifecycle,
/// reachability, settings, and address-list changes.
pub fn refresh(app: &AppHandle) {
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        if let Ok(menu) = build_menu(app) {
            #[cfg(target_os = "macos")]
            {
                let _ = tray;
                *robust_lock(&MAC_MENU) = Some(menu);
            }
            #[cfg(not(target_os = "macos"))]
            let _ = tray.set_menu(Some(menu));
        }
    }
    sync_icon(app);
}

/// Every URL the relay is reachable at, local endpoint first, then LAN,
/// then the public (internet) endpoint when known; the status loop compares
/// it every tick (cheap: interface enumeration is cached for 10 s, the
/// public IP for minutes) and rebuilds the menu when Wi-Fi, settings or a
/// finished public-IP lookup change it.
pub fn menu_addresses(settings: &crate::settings::AppSettings, public: Option<&str>) -> Vec<String> {
    let local = settings.base_url();
    let mut addresses = vec![local.clone()];
    for url in crate::commands::lan_endpoints(settings).into_iter().chain(public.map(String::from)) {
        if !addresses.contains(&url) {
            addresses.push(url);
        }
    }
    addresses
}

/// Re-asserts the tray icon from current reachability. No-op when already
/// correct, so the status loop calls it every tick — any missed update
/// self-heals within one poll instead of sticking until the next change.
/// While a pulse animation is in flight it yields (the animation re-syncs
/// state itself when it finishes, about one second after the last request).
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
/// eases back to the state icon (~1s). A re-trigger while animating
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
    let settings = robust_lock(&state.settings).clone();
    let endpoint = settings.base_url();
    let (public, _) = crate::public_ip::endpoint(app, &settings);
    let addresses = menu_addresses(&settings, public.as_deref());

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
    let copy = Submenu::with_id(app, "addresses", "Copy Address", true)?;
    for (index, url) in addresses.iter().enumerate() {
        let place = if index == 0 {
            "this machine"
        } else if public.as_deref() == Some(url.as_str()) {
            "internet"
        } else {
            "network"
        };
        copy.append(&MenuItem::with_id(
            app,
            format!("{COPY_ADDRESS_PREFIX}{index}"),
            format!("{url}  ({place})"),
            true,
            None::<&str>,
        )?)?;
    }
    if settings.effective_network_scope() == "any" && public.is_none() {
        copy.append(&MenuItem::with_id(
            app,
            "addresses-public-note",
            "Internet address: your router's public IP and forwarded port",
            false,
            None::<&str>,
        )?)?;
    }
    *robust_lock(&MENU_ADDRESSES) = addresses;
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
            &copy,
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
        id => {
            if let Some(url) = id
                .strip_prefix(COPY_ADDRESS_PREFIX)
                .and_then(|index| index.parse::<usize>().ok())
                .and_then(|index| robust_lock(&MENU_ADDRESSES).get(index).cloned())
            {
                copy_address(&app, &url);
            }
        }
    }
}

fn copy_address(app: &AppHandle, url: &str) {
    let state = app.state::<AppState>();
    match copy_to_clipboard(url) {
        Ok(()) => {
            state.supervisor.log("app", &format!("Copied {url} to the clipboard."), false);
            crate::state::notify(app, "Address copied", &format!("Copied {url}"));
        }
        Err(error) => {
            let message = format!("Cannot copy {url} to the clipboard: {error}");
            state.supervisor.log("app", &message, true);
            crate::state::notify(app, "Copy failed", &message);
        }
    }
}

/// One clipboard handle for the process lifetime: on X11 the owning
/// process serves the copied text, so a dropped handle would lose it.
fn copy_to_clipboard(text: &str) -> Result<(), String> {
    static CLIPBOARD: Mutex<Option<arboard::Clipboard>> = Mutex::new(None);
    let mut clipboard = robust_lock(&CLIPBOARD);
    if clipboard.is_none() {
        *clipboard = Some(arboard::Clipboard::new().map_err(|error| error.to_string())?);
    }
    clipboard
        .as_mut()
        .expect("clipboard initialized above")
        .set_text(text.to_string())
        .map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn left_click_opens_the_dashboard_and_right_click_the_menu() {
        assert_eq!(click_action(MouseButton::Left, MouseButtonState::Up), ClickAction::ShowDashboard);
        assert_eq!(click_action(MouseButton::Middle, MouseButtonState::Up), ClickAction::None);
        if cfg!(target_os = "macos") {
            assert_eq!(click_action(MouseButton::Left, MouseButtonState::Down), ClickAction::ShowDashboard);
            assert_eq!(click_action(MouseButton::Right, MouseButtonState::Down), ClickAction::ShowMenu);
        } else {
            // Windows shows the attached menu natively on right release.
            assert_eq!(click_action(MouseButton::Right, MouseButtonState::Up), ClickAction::None);
        }
    }

    #[test]
    fn addresses_list_the_local_endpoint_first_without_duplicates() {
        let mut settings = crate::settings::AppSettings {
            host: "127.0.0.1".into(),
            ..Default::default()
        };
        assert_eq!(menu_addresses(&settings, None), vec![settings.base_url()]);
        settings.host = "0.0.0.0".into();
        let addresses = menu_addresses(&settings, None);
        assert_eq!(addresses[0], format!("http://127.0.0.1:{}/v1", settings.port));
        let mut unique = addresses.clone();
        unique.dedup();
        assert_eq!(unique.len(), addresses.len());
        assert!(addresses[1..].iter().all(|url| !url.contains("127.0.0.1")));
    }

    #[test]
    fn addresses_end_with_the_public_endpoint_when_known() {
        let settings = crate::settings::AppSettings {
            host: "0.0.0.0".into(),
            network_scope: "any".into(),
            ..Default::default()
        };
        let public = format!("http://203.0.113.10:{}/v1", settings.port);
        let addresses = menu_addresses(&settings, Some(&public));
        assert_eq!(addresses.last(), Some(&public));
        assert_eq!(addresses.iter().filter(|url| **url == public).count(), 1);
        assert_eq!(addresses[0], settings.base_url());
    }
}
