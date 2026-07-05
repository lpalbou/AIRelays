//! Tauri command layer: the contract between the web dashboard and the core.
//!
//! Anything that can block (process stop, file IO, subprocess runs) executes
//! on blocking threads, never on the main/UI thread.

use crate::relay::{
    configure_platform, robust_lock, spawn_console_pipe, write_relay_config, ConsoleEntry,
    RelaySupervisor,
};
use crate::settings::AppSettings;
use crate::state::AppState;
use crate::traffic::RequestSummary;
use serde::Serialize;
use serde_json::Value;
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager};

#[derive(Serialize)]
pub struct UiState {
    pub lifecycle: crate::relay::Lifecycle,
    pub reachable: bool,
    pub managed: bool,
    /// True when the relay answers but rejects our token (running relay,
    /// credential mismatch) — distinct from "not reachable".
    pub auth_mismatch: bool,
    pub settings: AppSettings,
    pub relay_status: Option<Value>,
    pub local_endpoint: String,
    pub lan_endpoints: Vec<String>,
    pub config_path: String,
    pub logs_dir: String,
}

// MARK: internal helpers shared with the tray

pub fn show_dashboard(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

pub fn start_relay_blocking(app: &AppHandle) -> Result<u32, String> {
    let state = app.state::<AppState>();
    let settings = robust_lock(&state.settings).clone();
    let resource_dir = app.path().resource_dir().ok();
    let result = state.supervisor.start(&settings, resource_dir);
    if let Err(ref error) = result {
        state.supervisor.log("relay", error, true);
        *robust_lock(&state.supervisor.lifecycle) = crate::relay::Lifecycle::Failed;
    }
    crate::tray::refresh(app);
    result
}

pub fn stop_relay_blocking(app: &AppHandle) {
    let state = app.state::<AppState>();
    state.supervisor.stop();
    *robust_lock(&state.relay_status) = None;
    crate::tray::refresh(app);
}

fn restart_if_managed_blocking(app: &AppHandle) {
    let managed = app.state::<AppState>().supervisor.is_managed();
    if managed {
        stop_relay_blocking(app);
        let _ = start_relay_blocking(app);
    }
}

/// Persists a settings mutation, rewrites the relay config, and reports
/// every failure to the console instead of swallowing it.
fn apply_settings_change(
    app: &AppHandle,
    note: &str,
    mutate: impl FnOnce(&mut AppSettings),
) -> Result<(), String> {
    let state = app.state::<AppState>();
    let settings = {
        let mut settings = robust_lock(&state.settings);
        mutate(&mut settings);
        settings.clone()
    };
    let result = state
        .persist_settings()
        .and_then(|_| write_relay_config(&settings));
    match &result {
        Ok(()) => state.supervisor.log("config", note, false),
        Err(error) => state.supervisor.log("config", error, true),
    }
    crate::tray::refresh(app);
    result
}

pub fn apply_auth_mode_blocking(app: &AppHandle, require_token: bool) -> Result<(), String> {
    apply_settings_change(
        app,
        if require_token {
            "Auth mode: protected (relay token required)."
        } else {
            "Auth mode: open (no auth)."
        },
        |settings| settings.require_bearer_auth = require_token,
    )?;
    restart_if_managed_blocking(app);
    Ok(())
}

pub fn apply_network_exposure_blocking(app: &AppHandle, exposed: bool) -> Result<(), String> {
    apply_settings_change(
        app,
        if exposed {
            "Listener: all interfaces (private network can connect)."
        } else {
            "Listener: loopback only (this machine)."
        },
        |settings| {
            settings.host = if exposed { "0.0.0.0".into() } else { "127.0.0.1".into() };
        },
    )?;
    restart_if_managed_blocking(app);
    Ok(())
}

/// Interface enumeration is not free; cache briefly since the dashboard
/// polls every 1.5 s.
fn lan_addresses() -> Vec<String> {
    static CACHE: Mutex<Option<(Instant, Vec<String>)>> = Mutex::new(None);
    let mut cache = robust_lock(&CACHE);
    if let Some((at, addresses)) = cache.as_ref() {
        if at.elapsed() < Duration::from_secs(10) {
            return addresses.clone();
        }
    }
    let addresses = match local_ip_address::list_afinet_netifas() {
        Ok(interfaces) => interfaces
            .into_iter()
            .filter(|(_, ip)| ip.is_ipv4() && !ip.is_loopback())
            .map(|(_, ip)| ip.to_string())
            .collect(),
        Err(_) => Vec::new(),
    };
    *cache = Some((Instant::now(), addresses.clone()));
    addresses
}

/// Runs a relay CLI subcommand, streaming its output to the console live
/// (required for login flows that print a verification URL and then wait).
/// Returns captured stdout for JSON-producing commands.
fn run_relay_cli(app: &AppHandle, label: &str, extra_args: &[&str]) -> Result<StreamedRun, String> {
    let state = app.state::<AppState>();
    let settings = robust_lock(&state.settings).clone();
    let resource_dir = app.path().resource_dir().ok();
    let (program, mut args) = RelaySupervisor::resolve_command(&settings, resource_dir)?;
    args.extend(extra_args.iter().map(|s| s.to_string()));
    args.push("--config".into());
    args.push(AppSettings::relay_config_path().to_string_lossy().into_owned());
    run_streamed(app, label, &program, &args)
}

pub struct StreamedRun {
    pub success: bool,
    pub stdout: String,
}

impl StreamedRun {
    fn ok_or_concise_error(self) -> Result<String, String> {
        if self.success {
            Ok(self.stdout)
        } else {
            Err(concise_error(&self.stdout))
        }
    }
}

/// Spawns any program with output streamed to the console; returns the
/// captured stdout and exit success.
fn run_streamed(
    app: &AppHandle,
    label: &str,
    program: &str,
    args: &[String],
) -> Result<StreamedRun, String> {
    let state = app.state::<AppState>();
    state
        .supervisor
        .log(label, &format!("{} {}", program, args.join(" ")), false);

    let mut command = Command::new(program);
    command
        .args(args)
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    configure_platform(&mut command);

    let mut child = command
        .spawn()
        .map_err(|error| format!("Cannot run {program}: {error}"))?;

    // stderr streams live; stdout is captured line-by-line so JSON output
    // remains parseable while still appearing in the console.
    if let Some(stderr) = child.stderr.take() {
        spawn_console_pipe(state.supervisor.console_handle(), stderr, label.to_string(), false);
    }
    let stdout_lines = std::sync::Arc::new(Mutex::new(Vec::<String>::new()));
    if let Some(stdout) = child.stdout.take() {
        let console = state.supervisor.console_handle();
        let sink = std::sync::Arc::clone(&stdout_lines);
        let source = label.to_string();
        std::thread::spawn(move || {
            use std::io::BufRead;
            let reader = std::io::BufReader::new(stdout);
            for line in reader.lines().map_while(Result::ok) {
                crate::relay::push_console(&console, &source, &line, false);
                robust_lock(&sink).push(line);
            }
        });
    }

    let status = child
        .wait()
        .map_err(|error| format!("Waiting for {program} failed: {error}"))?;
    // Give the pipe threads a moment to flush the final lines.
    std::thread::sleep(Duration::from_millis(120));
    let stdout = robust_lock(&stdout_lines).join("\n");

    Ok(StreamedRun {
        success: status.success(),
        stdout,
    })
}

/// The last meaningful line of tool output carries the actual failure;
/// tracebacks belong in the console, not in a dialog.
fn concise_error(output: &str) -> String {
    output
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.chars().all(|c| "^~".contains(c)))
        .next_back()
        .unwrap_or("Command failed. See the Console tab for details.")
        .to_string()
}

// MARK: commands exposed to the dashboard

#[tauri::command]
pub fn get_state(app: AppHandle) -> UiState {
    let state = app.state::<AppState>();
    let settings = robust_lock(&state.settings).clone();
    let port = settings.port;
    let relay_status = robust_lock(&state.relay_status).clone();
    let ui_state = UiState {
        lifecycle: state.lifecycle(),
        reachable: relay_status.is_some(),
        managed: state.supervisor.is_managed(),
        auth_mismatch: *robust_lock(&state.auth_mismatch),
        local_endpoint: settings.base_url(),
        lan_endpoints: if settings.is_loopback_host() {
            Vec::new()
        } else {
            lan_addresses()
                .into_iter()
                .map(|ip| format!("http://{ip}:{port}/v1"))
                .collect()
        },
        relay_status,
        config_path: AppSettings::relay_config_path().to_string_lossy().into_owned(),
        logs_dir: AppSettings::logs_dir().to_string_lossy().into_owned(),
        settings,
    };
    ui_state
}

#[tauri::command]
pub async fn save_settings(app: AppHandle, settings: AppSettings) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        settings.validate()?;
        let state = app.state::<AppState>();
        *robust_lock(&state.settings) = settings.clone();
        state.persist_settings()?;
        write_relay_config(&settings)?;
        state.supervisor.log("config", "Saved settings and relay config.", false);
        crate::tray::refresh(&app);
        Ok(())
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn start_relay(app: AppHandle) -> Result<u32, String> {
    tauri::async_runtime::spawn_blocking(move || start_relay_blocking(&app))
        .await
        .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn stop_relay(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        stop_relay_blocking(&app);
        Ok(())
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn restart_relay(app: AppHandle) -> Result<u32, String> {
    tauri::async_runtime::spawn_blocking(move || {
        stop_relay_blocking(&app);
        start_relay_blocking(&app)
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn set_auth_mode(app: AppHandle, require_token: bool) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || apply_auth_mode_blocking(&app, require_token))
        .await
        .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn set_network_exposure(app: AppHandle, exposed: bool) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || apply_network_exposure_blocking(&app, exposed))
        .await
        .map_err(|error| error.to_string())?
}

#[tauri::command]
pub fn get_console(app: AppHandle) -> Vec<ConsoleEntry> {
    robust_lock(&app.state::<AppState>().supervisor.console).clone()
}

#[tauri::command]
pub fn clear_console(app: AppHandle) {
    robust_lock(&app.state::<AppState>().supervisor.console).clear();
}

#[tauri::command]
pub async fn get_traffic() -> Result<Vec<RequestSummary>, String> {
    tauri::async_runtime::spawn_blocking(crate::traffic::recent_requests)
        .await
        .map_err(|error| error.to_string())
}

/// Runs the relay's diagnostic. Returns true when every check passed;
/// false means the report (in the console) contains failed checks — that is
/// a finding, not a command error.
#[tauri::command]
pub async fn run_doctor(app: AppHandle, skip_response: bool) -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut args = vec!["doctor"];
        if skip_response {
            args.push("--skip-response");
        }
        run_relay_cli(&app, "check-setup", &args).map(|run| run.success)
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub async fn run_login(app: AppHandle, provider: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || match provider.as_str() {
        "openai" => run_relay_cli(&app, "openai-login", &["login"])?
            .ok_or_concise_error()
            .map(|_| ()),
        "claude" => {
            // Claude auth lives in the external claude CLI, not the relay.
            let claude_bin = {
                let state = app.state::<AppState>();
                let settings = robust_lock(&state.settings);
                settings.claude_bin.clone()
            };
            run_streamed(
                &app,
                "claude-login",
                &claude_bin,
                &["auth".into(), "login".into(), "--claudeai".into()],
            )?
            .ok_or_concise_error()
            .map(|_| ())
        }
        other => Err(format!("Unknown login provider: {other}")),
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Fetches per-account usage from the relay's subscription-status endpoint.
#[tauri::command]
pub async fn get_usage(app: AppHandle) -> Result<Value, String> {
    let (base_url, requires_auth) = {
        let state = app.state::<AppState>();
        let settings = robust_lock(&state.settings);
        (settings.base_url(), settings.require_bearer_auth)
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|error| error.to_string())?;
    let mut request = client.get(format!("{base_url}/subscription/status"));
    if requires_auth {
        if let Ok(token) = std::fs::read_to_string(AppSettings::bearer_token_file()) {
            request = request.bearer_auth(token.trim());
        }
    }
    let response = request
        .send()
        .await
        .map_err(|_| "The relay is not reachable.".to_string())?;
    if !response.status().is_success() {
        let code = response.status();
        let detail: Value = response.json().await.unwrap_or(Value::Null);
        let message = detail
            .get("detail")
            .and_then(Value::as_str)
            .map(String::from)
            .unwrap_or_else(|| format!("Relay answered {code}."));
        return Err(message);
    }
    response
        .json()
        .await
        .map_err(|_| "Unexpected usage payload.".to_string())
}

#[tauri::command]
pub async fn token_action(app: AppHandle, action: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let args: &[&str] = match action.as_str() {
            "show" => &["token", "show", "--json"],
            "rotate" => &["token", "rotate", "--json"],
            _ => return Err(format!("Unknown token action: {action}")),
        };
        let stdout = run_relay_cli(&app, "token", args)?.ok_or_concise_error()?;
        let value: Value = serde_json::from_str(stdout.trim())
            .map_err(|_| "Token command returned unexpected output.".to_string())?;
        value
            .get("relay_token")
            .and_then(Value::as_str)
            .map(String::from)
            .ok_or_else(|| "No relay token configured yet.".to_string())
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
pub fn set_custom_token(token: String) -> Result<(), String> {
    let trimmed = token.trim();
    if trimmed.is_empty() {
        return Err("Enter a non-empty relay token.".into());
    }
    let path = AppSettings::bearer_token_file();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    }
    std::fs::write(&path, format!("{trimmed}\n")).map_err(|error| error.to_string())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

#[tauri::command]
pub fn open_path(path: String) -> Result<(), String> {
    let opener = if cfg!(target_os = "macos") {
        "open"
    } else if cfg!(target_os = "windows") {
        "explorer"
    } else {
        "xdg-open"
    };
    let mut child = Command::new(opener)
        .arg(&path)
        .spawn()
        .map_err(|error| format!("Cannot open {path}: {error}"))?;
    // Reap in the background to avoid zombie openers on Unix.
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}
