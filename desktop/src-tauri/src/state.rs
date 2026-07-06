//! Shared application state and the status polling loop.

use crate::relay::{robust_lock, Lifecycle, RelaySupervisor};
use crate::settings::AppSettings;
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager};

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

/// Background loop: polls the relay status endpoint, reconciles the child
/// process, and asks the tray to refresh when observable state changes.
pub fn spawn_status_loop(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .expect("reqwest client");
        let mut last_reachable: Option<bool> = None;
        loop {
            let exited = {
                let state = app.state::<AppState>();
                state.supervisor.reap_if_exited()
            };

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
                }
                *robust_lock(&state.relay_status) = status;
                *robust_lock(&state.auth_mismatch) = auth_mismatch;

                if exited || last_reachable != Some(reachable) {
                    last_reachable = Some(reachable);
                    crate::tray::refresh(&app);
                }
            }
            tokio::time::sleep(Duration::from_millis(1500)).await;
        }
    });
}
