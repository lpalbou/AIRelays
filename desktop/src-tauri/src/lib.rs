//! AIRelays desktop: Rust core that supervises the relay, owns the tray,
//! and hosts the shared web dashboard.

mod commands;
mod relay;
mod settings;
mod state;
pub mod traffic;
mod tray;
mod updates;

use state::AppState;
use tauri::Manager;

fn should_hide_dashboard(label: &str, tray_available: bool, visible: bool, focused: bool) -> bool {
    label == "main" && tray_available && visible && !focused
}

/// Marks launches made by the login item.
const AUTOSTART_ARG: &str = "--autostart";

pub fn run() {
    tauri::Builder::default()
        // First plugin, so a second launch exits before it creates a tray or
        // a relay supervisor: one tray per user session. The running app
        // shows its dashboard instead, which is what a user opening the
        // app again expects.
        // A login-item launch (`--autostart`) while the app already runs
        // stays silent.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            if !argv.iter().any(|arg| arg == AUTOSTART_ARG) {
                commands::show_dashboard(app);
            }
        }))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec![AUTOSTART_ARG]),
        ))
        .plugin(tauri_plugin_notification::init())
        .setup(|app| {
            // Before any child process spawns: GUI PATH lacks user bin dirs
            // (breaks the `claude` CLI and any PATH-installed relay).
            relay::extend_path_for_gui();
            let settings_path = app
                .path()
                .app_config_dir()
                .expect("app config dir")
                .join("app-settings.json");
            let state = AppState::load(settings_path);
            let reopened = std::env::args().any(|arg| arg == updates::AFTER_UPDATE_ARG);
            state.after_update.store(reopened, std::sync::atomic::Ordering::SeqCst);
            app.manage(state);

            // Re-register an existing login item so it carries the current
            // launch arguments.
            {
                use tauri_plugin_autostart::ManagerExt;
                if app.autolaunch().is_enabled().unwrap_or(false) {
                    let _ = app.autolaunch().enable();
                }
            }

            // Tray-first app: no dock icon on macOS.
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // The window title always carries the running version (single
            // source: Cargo.toml via the compiled package info), so what the
            // user sees can never drift from what is actually installed.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_title(&format!(
                    "AIRelays {}",
                    app.package_info().version
                ));
            }

            // A failed tray (e.g. Linux without an appindicator host) must
            // not leave the app invisible: fall back to showing the window.
            let tray_ok = tray::init(app.handle()).is_ok();
            let first_run = app.state::<state::AppState>().first_run;
            // After an in-app update, show the dashboard: it reports the
            // outcome, and a tray-only reopen looks like the app vanished.
            let after_update = app
                .state::<state::AppState>()
                .after_update
                .load(std::sync::atomic::Ordering::SeqCst);
            if !tray_ok || first_run || after_update {
                commands::show_dashboard(app.handle());
            }
            state::spawn_status_loop(app.handle().clone());
            state::spawn_activity_loop(app.handle().clone());
            state::spawn_launch_start(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            match event {
                // Hiding preserves the page, edits, and running relay.
                tauri::WindowEvent::CloseRequested { api, .. } => {
                    api.prevent_close();
                    let _ = window.hide();
                }
                tauri::WindowEvent::Focused(false) if window.label() == "main" => {
                    let window = window.clone();
                    tauri::async_runtime::spawn(async move {
                        // Let tray/menu focus settle before checking again: an
                        // old blur must not hide a newly reopened dashboard.
                        tokio::time::sleep(std::time::Duration::from_millis(120)).await;
                        let app = window.app_handle().clone();
                        let _ = app.run_on_main_thread(move || {
                            if should_hide_dashboard(
                                window.label(),
                                window.app_handle().tray_by_id(tray::TRAY_ID).is_some(),
                                window.is_visible().unwrap_or(false),
                                window.is_focused().unwrap_or(true),
                            ) {
                                let _ = window.hide();
                            }
                        });
                    });
                }
                _ => {}
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_state,
            commands::save_settings,
            commands::start_relay,
            commands::stop_relay,
            commands::restart_relay,
            commands::set_auth_mode,
            commands::set_network_exposure,
            commands::get_console,
            commands::clear_console,
            commands::get_traffic,
            commands::run_doctor,
            commands::run_login,
            commands::get_usage,
            commands::token_action,
            commands::set_custom_token,
            commands::set_login_method,
            commands::logout_account,
            commands::refresh_accounts,
            commands::open_path,
            commands::get_autostart,
            commands::set_autostart,
            commands::set_claude_token,
            commands::clear_claude_token,
            commands::logout_claude,
            commands::submit_login_code,
            commands::cancel_login,
            commands::get_models,
            commands::get_log_retention,
            commands::set_log_retention,
            commands::open_url,
            commands::check_for_update,
            commands::install_update,
            commands::take_update_result,
        ])
        .build(tauri::generate_context!())
        .expect("error building AIRelays desktop app")
        .run(|app, event| {
            #[cfg(target_os = "macos")]
            if matches!(event, tauri::RunEvent::Reopen { .. }) {
                commands::show_dashboard(app);
            }
            if let tauri::RunEvent::ExitRequested { api, code, .. } = event {
                // Keep running when all windows are hidden; only explicit
                // Quit (exit code set) may terminate the app.
                if code.is_none() {
                    api.prevent_exit();
                } else {
                    app.state::<AppState>().supervisor.stop();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::should_hide_dashboard;

    #[test]
    fn dashboard_hides_only_after_blur_with_a_tray_available() {
        assert!(should_hide_dashboard("main", true, true, false));
        assert!(!should_hide_dashboard("main", true, true, true));
        assert!(!should_hide_dashboard("main", false, true, false));
        assert!(!should_hide_dashboard("main", true, false, false));
        assert!(!should_hide_dashboard("other", true, true, false));
    }
}
