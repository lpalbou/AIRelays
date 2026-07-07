//! Shared application state and the status polling loop.

use crate::relay::{robust_lock, Lifecycle, RelaySupervisor};
use crate::settings::AppSettings;
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};
use tauri_plugin_notification::NotificationExt;

/// Crash auto-restart: capped exponential backoff, then give up. The caps
/// keep a permanently broken config from burning CPU in a spawn loop while
/// still healing the transient midnight crash within seconds.
const RESTART_MAX_ATTEMPTS: u32 = 5;
const RESTART_BASE_DELAY_SECS: u64 = 2;
const RESTART_MAX_DELAY_SECS: u64 = 60;

fn notify(app: &AppHandle, title: &str, body: &str) {
    // Best effort: a denied notification permission must never affect
    // supervision itself.
    let _ = app.notification().builder().title(title).body(body).show();
}

pub struct AppState {
    pub settings: Mutex<AppSettings>,
    pub supervisor: RelaySupervisor,
    /// Latest `/v1/relay/status` payload, None when unreachable.
    pub relay_status: Mutex<Option<Value>>,
    /// True when the relay answers with 401/403: running, but our token is
    /// wrong or missing — a different situation than "not running".
    pub auth_mismatch: Mutex<bool>,
    pub settings_path: PathBuf,
    /// True when no settings file existed at startup (first run).
    pub first_run: bool,
    /// PID of an in-flight sign-in subprocess. Only one login may run at a
    /// time (the OAuth callback binds a fixed localhost port).
    pub login_pid: Mutex<Option<u32>>,
    /// The authorize URL printed by the running sign-in flow, so the UI can
    /// offer it for copying when the browser did not open. Arc because the
    /// stdout pipe thread writes it.
    pub login_url: std::sync::Arc<Mutex<Option<String>>>,
    /// The pairing code printed by a device-code sign-in flow.
    pub login_code: std::sync::Arc<Mutex<Option<String>>>,
    /// Which provider the running sign-in belongs to ("openai"/"claude"),
    /// so the UI can render provider-appropriate instructions.
    pub login_provider: Mutex<Option<String>>,
    /// Stdin of the running sign-in child. Claude's browser flow shows the
    /// user a code that must be typed into the CLI; the dashboard collects
    /// it and writes it here.
    pub login_stdin: Mutex<Option<std::process::ChildStdin>>,
    /// Set when the user cancels the running sign-in, so its non-zero exit
    /// is reported as a cancellation instead of a failure.
    pub login_cancelled: std::sync::atomic::AtomicBool,
}

impl AppState {
    pub fn load(settings_path: PathBuf) -> Self {
        let raw = std::fs::read(&settings_path).ok();
        let first_run = raw.is_none();
        let settings = match raw.as_deref().map(serde_json::from_slice::<AppSettings>) {
            Some(Ok(parsed)) => parsed,
            Some(Err(_)) => {
                // Never silently reset a corrupt file to defaults: defaults
                // expose the LAN listener the user may have disabled. Keep a
                // backup so the choice is recoverable.
                let backup = settings_path.with_extension("json.corrupt");
                let _ = std::fs::copy(&settings_path, &backup);
                AppSettings::default()
            }
            None => AppSettings::default(),
        };
        Self {
            settings: Mutex::new(settings),
            supervisor: RelaySupervisor::new(),
            relay_status: Mutex::new(None),
            auth_mismatch: Mutex::new(false),
            settings_path,
            first_run,
            login_pid: Mutex::new(None),
            login_url: std::sync::Arc::new(Mutex::new(None)),
            login_code: std::sync::Arc::new(Mutex::new(None)),
            login_provider: Mutex::new(None),
            login_stdin: Mutex::new(None),
            login_cancelled: std::sync::atomic::AtomicBool::new(false),
        }
    }

    pub fn persist_settings(&self) -> Result<(), String> {
        let settings = robust_lock(&self.settings).clone();
        if let Some(parent) = self.settings_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("Cannot create settings dir: {error}"))?;
        }
        let json = serde_json::to_vec_pretty(&settings)
            .map_err(|error| format!("Cannot encode settings: {error}"))?;
        std::fs::write(&self.settings_path, json)
            .map_err(|error| format!("Cannot write settings: {error}"))
    }

    pub fn is_reachable(&self) -> bool {
        robust_lock(&self.relay_status).is_some()
    }

    pub fn lifecycle(&self) -> Lifecycle {
        *robust_lock(&self.supervisor.lifecycle)
    }
}

/// Starts the relay once when the app opens (used with "start at login").
/// Waits for the first status polls so an already-running relay — e.g. one
/// managed by the CLI or a previous session — is detected and respected
/// instead of colliding on the port.
pub fn spawn_launch_start(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        {
            let state = app.state::<AppState>();
            if !robust_lock(&state.settings).start_relay_on_launch {
                return;
            }
        }
        tokio::time::sleep(Duration::from_millis(3000)).await;
        let should_start = {
            let state = app.state::<AppState>();
            !state.is_reachable()
                && !*robust_lock(&state.auth_mismatch)
                && !state.supervisor.is_managed()
        };
        if should_start {
            let app_clone = app.clone();
            let _ = tauri::async_runtime::spawn_blocking(move || {
                crate::commands::start_relay_blocking(&app_clone)
            })
            .await;
        }
    });
}

/// Background loop: polls the relay status endpoint, reconciles the child
/// process, and asks the tray to refresh when observable state changes.
pub fn spawn_status_loop(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .expect("reqwest client");
        let mut last_reachable: Option<bool> = None;
        // Served-request count from the previous poll, for the activity blink.
        let mut last_requests_total: Option<u64> = None;
        // Crash-restart bookkeeping, local to the loop: no other code path
        // restarts the relay implicitly.
        let mut restart_attempts: u32 = 0;
        let mut restart_due: Option<Instant> = None;
        loop {
            let exited = {
                let state = app.state::<AppState>();
                state.supervisor.reap_if_exited()
            };

            // The child died without a user stop: schedule a respawn with
            // capped backoff, or give up after too many consecutive failures.
            if exited && restart_due.is_none() {
                let (wants_restart, auto_restart_enabled) = {
                    let state = app.state::<AppState>();
                    let wants = state.supervisor.desired_running();
                    let enabled = robust_lock(&state.settings).auto_restart_relay;
                    (wants, enabled)
                };
                if wants_restart && auto_restart_enabled {
                    if restart_attempts >= RESTART_MAX_ATTEMPTS {
                        let state = app.state::<AppState>();
                        state.supervisor.log(
                            "relay",
                            &format!(
                                "Auto-restart gave up after {RESTART_MAX_ATTEMPTS} failed attempts. Fix the cause (see above), then use Start Relay."
                            ),
                            true,
                        );
                        notify(
                            &app,
                            "AIRelays relay is down",
                            "Automatic restarts failed repeatedly. Open the Console tab for details, then use Start Relay.",
                        );
                    } else {
                        let delay = (RESTART_BASE_DELAY_SECS << restart_attempts)
                            .min(RESTART_MAX_DELAY_SECS);
                        restart_attempts += 1;
                        restart_due = Some(Instant::now() + Duration::from_secs(delay));
                        let state = app.state::<AppState>();
                        state.supervisor.log(
                            "relay",
                            &format!(
                                "Relay exited unexpectedly — restarting in {delay}s (attempt {restart_attempts}/{RESTART_MAX_ATTEMPTS})."
                            ),
                            true,
                        );
                        notify(
                            &app,
                            "AIRelays relay stopped unexpectedly",
                            &format!("Restarting automatically in {delay}s."),
                        );
                    }
                }
            }

            if restart_due.is_some_and(|due| Instant::now() >= due) {
                restart_due = None;
                // A user action in the meantime (manual start/stop) makes
                // this respawn wrong: start() would fail on a managed child,
                // and after a stop the user wants it down.
                let still_wanted = {
                    let state = app.state::<AppState>();
                    state.supervisor.desired_running() && !state.supervisor.is_managed()
                };
                if still_wanted {
                    let app_clone = app.clone();
                    let result = tauri::async_runtime::spawn_blocking(move || {
                        crate::commands::start_relay_blocking(&app_clone)
                    })
                    .await;
                    if !matches!(result, Ok(Ok(_))) {
                        // Spawn failure (bad command/config): no child exists,
                        // so reap never fires again — feed the backoff loop
                        // directly by faking an exit next tick.
                        let state = app.state::<AppState>();
                        state.supervisor.log(
                            "relay",
                            "Auto-restart attempt failed to spawn the relay.",
                            true,
                        );
                        if restart_attempts < RESTART_MAX_ATTEMPTS {
                            let delay = (RESTART_BASE_DELAY_SECS << restart_attempts)
                                .min(RESTART_MAX_DELAY_SECS);
                            restart_attempts += 1;
                            restart_due = Some(Instant::now() + Duration::from_secs(delay));
                        } else {
                            notify(
                                &app,
                                "AIRelays relay is down",
                                "Automatic restarts failed repeatedly. Open the Console tab for details, then use Start Relay.",
                            );
                        }
                    }
                }
            }

            let (base_url, requires_auth) = {
                let state = app.state::<AppState>();
                let settings = robust_lock(&state.settings);
                (settings.base_url(), settings.require_bearer_auth)
            };
            let mut request = client.get(format!("{base_url}/relay/status"));
            if requires_auth {
                if let Ok(token) = std::fs::read_to_string(AppSettings::bearer_token_file()) {
                    request = request.bearer_auth(token.trim());
                }
            }
            let (status, auth_mismatch): (Option<Value>, bool) = match request.send().await {
                Ok(response) if response.status().is_success() => {
                    (response.json().await.ok(), false)
                }
                Ok(response)
                    if response.status() == reqwest::StatusCode::UNAUTHORIZED
                        || response.status() == reqwest::StatusCode::FORBIDDEN =>
                {
                    // Something is serving and rejecting us: running relay,
                    // wrong/missing credential.
                    (None, true)
                }
                _ => (None, false),
            };

            {
                let state = app.state::<AppState>();
                let reachable = status.is_some();
                if reachable {
                    state.supervisor.mark_running();
                    // A healthy relay resets the crash budget: only
                    // consecutive failures count against the give-up cap.
                    restart_attempts = 0;
                }
                // Activity blink: the relay counts real requests; any
                // increase since the last poll flashes the tray once.
                let requests_total = status
                    .as_ref()
                    .and_then(|payload| payload.get("requests_total"))
                    .and_then(Value::as_u64);
                let should_pulse = matches!(
                    (last_requests_total, requests_total),
                    (Some(previous), Some(current)) if current > previous
                );
                last_requests_total = requests_total;
                *robust_lock(&state.relay_status) = status;
                *robust_lock(&state.auth_mismatch) = auth_mismatch;

                if exited || last_reachable != Some(reachable) {
                    last_reachable = Some(reachable);
                    crate::tray::refresh(&app);
                }
                if should_pulse {
                    crate::tray::pulse(&app);
                } else {
                    // Self-healing: re-assert the icon every tick; a missed
                    // or failed set_icon no longer sticks until the next
                    // reachability change.
                    crate::tray::sync_icon(&app);
                }
            }
            tokio::time::sleep(Duration::from_millis(1500)).await;
        }
    });
}
