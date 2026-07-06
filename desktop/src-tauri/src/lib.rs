//! AIRelays desktop: Rust core that supervises the relay, owns the tray,
//! and hosts the shared web dashboard.

mod commands;
mod relay;
mod settings;
mod state;
pub mod traffic;
mod tray;

use state::AppState;
use tauri::Manager;

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let settings_path = app
                .path()
                .app_config_dir()
                .expect("app config dir")
                .join("app-settings.json");
            app.manage(AppState::load(settings_path));

            // Tray-first app: no dock icon on macOS.
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // A failed tray (e.g. Linux without an appindicator host) must
            // not leave the app invisible: fall back to showing the window.
            let tray_ok = tray::init(app.handle()).is_ok();
            let first_run = app.state::<state::AppState>().first_run;
            if !tray_ok || first_run {
                commands::show_dashboard(app.handle());
            }
            state::spawn_status_loop(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the dashboard hides it; the app lives in the tray.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
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
        ])
        .build(tauri::generate_context!())
        .expect("error building AIRelays desktop app")
        .run(|app, event| {
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
