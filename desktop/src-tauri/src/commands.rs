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
    /// Set while a sign-in flow waits for the browser: the URL to visit.
    pub login_url: Option<String>,
    /// Pairing code for a running device-code sign-in.
    pub login_code: Option<String>,
    /// Provider of the running sign-in ("openai"/"claude"), for
    /// provider-appropriate banner wording.
    pub login_provider: Option<String>,
    /// True while the running sign-in can accept a pasted code on stdin.
    pub login_accepts_code: bool,
    pub login_running: bool,
    /// Whether the Claude provider actually runs under the current settings
    /// (feature flag AND loopback AND no X-Forwarded-For trust). Single
    /// source of truth for the UI — re-deriving the rule in JS caused traps.
    pub claude_effective: bool,
    /// True when a stored Claude token file exists. It overrides the CLI's
    /// own sign-in for relay requests, so the UI must offer sign-out even
    /// when the row looks signed out (a stale token masks CLI auth).
    pub claude_token_present: bool,
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
        // "Already managed" is a benign race (double-click, auto-restart
        // colliding with a manual start) — the relay is fine; marking the
        // lifecycle Failed here would wrongly flip the UI to red.
        if !state.supervisor.is_managed() {
            *robust_lock(&state.supervisor.lifecycle) = crate::relay::Lifecycle::Failed;
        }
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
    let addresses: Vec<String> = match local_ip_address::list_afinet_netifas() {
        Ok(interfaces) => interfaces
            .into_iter()
            .filter_map(|(_, ip)| match ip {
                // Only real private-network addresses (RFC 1918). Link-local
                // 169.254.x.x self-assigned addresses are unreachable noise
                // from bridges and unconfigured interfaces.
                std::net::IpAddr::V4(v4)
                    if v4.is_private() && !v4.is_loopback() && !v4.is_link_local() =>
                {
                    Some(v4.to_string())
                }
                _ => None,
            })
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
        // Debounced /healthz liveness — deliberately NOT derived from the
        // status payload, whose fetch can fail while the relay is fine.
        reachable: state.is_reachable(),
        managed: state.supervisor.is_managed(),
        auth_mismatch: *robust_lock(&state.auth_mismatch),
        login_url: robust_lock(&state.login_url).clone(),
        login_code: robust_lock(&state.login_code).clone(),
        login_provider: robust_lock(&state.login_provider).clone(),
        login_accepts_code: robust_lock(&state.login_stdin).is_some(),
        login_running: robust_lock(&state.login_pid).is_some(),
        claude_effective: settings.claude_effectively_enabled(),
        claude_token_present: AppSettings::data_dir().join("claude-token").exists(),
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
        // Restarting only makes sense for a relay this app owns. With an
        // external relay on the port, stop() is a no-op and start() fails
        // to bind — flipping the UI to Failed while the external relay
        // keeps serving its old config.
        {
            let state = app.state::<AppState>();
            if !state.supervisor.is_managed() && state.is_reachable() {
                return Err(
                    "A relay outside this app is answering on this address; restart it where it was started (or stop it, then use Start here).".into(),
                );
            }
        }
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

/// Whether the app is registered to start at login (OS state, not a
/// settings-file entry: the OS registry is the single source of truth).
#[tauri::command]
pub fn get_autostart(app: AppHandle) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    app.autolaunch().is_enabled().map_err(|error| error.to_string())
}

#[tauri::command]
pub fn set_autostart(app: AppHandle, enabled: bool) -> Result<(), String> {
    use tauri_plugin_autostart::ManagerExt;
    let autolaunch = app.autolaunch();
    let result = if enabled { autolaunch.enable() } else { autolaunch.disable() };
    if let Err(error) = result {
        // Disabling an entry that never existed errors on some platforms;
        // only fail when the OS state actually contradicts the request.
        if autolaunch.is_enabled().unwrap_or(!enabled) != enabled {
            return Err(error.to_string());
        }
    }
    let state = app.state::<AppState>();
    state.supervisor.log(
        "app",
        if enabled { "Start at login enabled." } else { "Start at login disabled." },
        false,
    );
    Ok(())
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

/// Kills a lingering sign-in subprocess. A login that was abandoned (browser
/// closed, flow never finished) holds the fixed OAuth callback port for up
/// to 15 minutes and makes every new attempt fail with "address in use".
fn kill_previous_login(app: &AppHandle) {
    let state = app.state::<AppState>();
    let previous = robust_lock(&state.login_pid).take();
    // Clear all shared login state here: the dying child's own teardown
    // deliberately no-ops once its pid is gone (see run_login_streamed),
    // so this is the only place that resets the banner after a kill.
    *robust_lock(&state.login_stdin) = None;
    *robust_lock(&state.login_url) = None;
    *robust_lock(&state.login_code) = None;
    *robust_lock(&state.login_provider) = None;
    if let Some(pid) = previous {
        app.state::<AppState>()
            .supervisor
            .log("login", "Cancelling the previous unfinished sign-in.", false);
        #[cfg(unix)]
        unsafe {
            // Children run in their own process group (configure_platform).
            libc::kill(-(pid as i32), libc::SIGTERM);
        }
        #[cfg(windows)]
        {
            let _ = Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .output();
        }
    }
}

/// Runs a sign-in flow with live output, tracking the child PID (so a stuck
/// flow can be replaced) and capturing the printed authorize URL for the UI.
fn run_login_streamed(
    app: &AppHandle,
    label: &str,
    provider: &str,
    program: &str,
    args: &[String],
) -> Result<(), String> {
    kill_previous_login(app);
    let state = app.state::<AppState>();
    state
        .supervisor
        .log(label, &format!("{} {}", program, args.join(" ")), false);

    let mut command = Command::new(program);
    command
        .args(args)
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        // Piped, kept open: Claude's browser flow asks for a code on stdin
        // (the dashboard collects it via submit_login_code). Inheriting the
        // GUI's stdin would make interactive reads hit EOF or hang.
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    configure_platform(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| format!("Cannot run {program}: {error}"))?;
    let child_pid = child.id();
    *robust_lock(&state.login_pid) = Some(child_pid);
    *robust_lock(&state.login_url) = None;
    *robust_lock(&state.login_code) = None;
    *robust_lock(&state.login_provider) = Some(provider.to_string());
    *robust_lock(&state.login_stdin) = child.stdin.take();
    state
        .login_cancelled
        .store(false, std::sync::atomic::Ordering::Relaxed);

    // Watchdog: external CLIs (claude) have no internal login timeout; an
    // abandoned flow would otherwise pend forever and block future logins.
    let timeout_secs = robust_lock(&state.settings).login_timeout_seconds.max(60);
    {
        let app_watchdog = app.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_secs(timeout_secs));
            let state = app_watchdog.state::<AppState>();
            if *robust_lock(&state.login_pid) == Some(child_pid) {
                state.supervisor.log(
                    "login",
                    &format!("Sign-in timed out after {timeout_secs}s; cancelling it."),
                    true,
                );
                kill_previous_login(&app_watchdog);
            }
        });
    }

    let stdout_lines = std::sync::Arc::new(Mutex::new(Vec::<String>::new()));
    if let Some(stderr) = child.stderr.take() {
        // Stderr both streams to the console and joins the collected output
        // so failure toasts can show the real error (CLIs print there).
        let console = state.supervisor.console_handle();
        let sink = std::sync::Arc::clone(&stdout_lines);
        let source = label.to_string();
        std::thread::spawn(move || {
            use std::io::BufRead;
            let reader = std::io::BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                crate::relay::push_console(&console, &source, &line, false);
                robust_lock(&sink).push(line);
            }
        });
    }
    if let Some(stdout) = child.stdout.take() {
        let console = state.supervisor.console_handle();
        let sink = std::sync::Arc::clone(&stdout_lines);
        let url_slot = std::sync::Arc::clone(&state.login_url);
        let code_slot = std::sync::Arc::clone(&state.login_code);
        let source = label.to_string();
        std::thread::spawn(move || {
            use std::io::BufRead;
            let reader = std::io::BufReader::new(stdout);
            for line in reader.lines().map_while(Result::ok) {
                let trimmed = line.trim();
                // Sign-in flows print the URL to visit; device flows also
                // print a pairing code ("2. Enter this code: XXXX-XXXX").
                if let Some(url) = trimmed.split_whitespace().find(|w| w.starts_with("https://")) {
                    *robust_lock(&url_slot) = Some(url.to_string());
                }
                if let Some((_, code)) = trimmed.split_once("Enter this code:") {
                    let code = code.trim();
                    if !code.is_empty() {
                        *robust_lock(&code_slot) = Some(code.to_string());
                    }
                }
                crate::relay::push_console(&console, &source, &line, false);
                robust_lock(&sink).push(line);
            }
        });
    }

    let status = child
        .wait()
        .map_err(|error| format!("Waiting for {program} failed: {error}"))?;
    std::thread::sleep(Duration::from_millis(120));
    // Teardown only if the shared slots still belong to THIS login: a
    // replacement login may already have been started (kill_previous_login
    // + new spawn), and clearing unconditionally would wipe its state,
    // leaving an orphaned, uncancellable flow holding the callback port.
    if *robust_lock(&state.login_pid) == Some(child_pid) {
        *robust_lock(&state.login_pid) = None;
        *robust_lock(&state.login_url) = None;
        *robust_lock(&state.login_code) = None;
        *robust_lock(&state.login_provider) = None;
        *robust_lock(&state.login_stdin) = None;
    }

    if status.success() {
        Ok(())
    } else if state
        .login_cancelled
        .load(std::sync::atomic::Ordering::Relaxed)
    {
        Err("Sign-in cancelled.".into())
    } else {
        let stdout = robust_lock(&stdout_lines).join("\n");
        Err(concise_error(&stdout))
    }
}

/// Program + leading args for an external CLI like `claude`. On Windows
/// the CLI installs as a .cmd shim that CreateProcess cannot start
/// directly, so it must run through `cmd /C`.
fn external_cli_invocation(bin: &str) -> (String, Vec<String>) {
    if cfg!(windows) && !bin.to_ascii_lowercase().ends_with(".exe") {
        ("cmd".into(), vec!["/C".into(), bin.into()])
    } else {
        (bin.into(), Vec::new())
    }
}

/// Cancels the running sign-in flow at the user's request.
#[tauri::command]
pub fn cancel_login(app: AppHandle) -> Result<(), String> {
    let state = app.state::<AppState>();
    if robust_lock(&state.login_pid).is_none() {
        return Ok(());
    }
    state
        .login_cancelled
        .store(true, std::sync::atomic::Ordering::Relaxed);
    kill_previous_login(&app);
    Ok(())
}

/// Delivers a code the user pasted in the dashboard to the stdin of the
/// running sign-in flow (Claude's browser flow shows one on the callback
/// page and reads it from the terminal).
#[tauri::command]
pub fn submit_login_code(app: AppHandle, code: String) -> Result<(), String> {
    use std::io::Write;
    let code = code.trim().to_string();
    if code.is_empty() {
        return Err("Paste the code shown in the browser first.".into());
    }
    let state = app.state::<AppState>();
    let mut slot = robust_lock(&state.login_stdin);
    let Some(stdin) = slot.as_mut() else {
        return Err("No sign-in is waiting for a code right now.".into());
    };
    writeln!(stdin, "{code}").map_err(|error| format!("Cannot deliver the code: {error}"))?;
    stdin.flush().ok();
    state
        .supervisor
        .log("login", "Code delivered to the sign-in flow.", false);
    Ok(())
}

#[tauri::command]
pub async fn run_login(app: AppHandle, provider: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || match provider.as_str() {
        "openai" => {
            let state = app.state::<AppState>();
            let settings = robust_lock(&state.settings).clone();
            let resource_dir = app.path().resource_dir().ok();
            let (program, mut args) = RelaySupervisor::resolve_command(&settings, resource_dir)?;
            args.push("login".into());
            // Explicit method: never rely on the CLI's headless autodetect
            // from inside a GUI process.
            args.push(if settings.login_method == "device" {
                "--device".into()
            } else {
                "--browser".into()
            });
            args.push("--config".into());
            args.push(AppSettings::relay_config_path().to_string_lossy().into_owned());
            run_login_streamed(&app, "openai-login", "openai", &program, &args)
        }
        "claude" => {
            // Claude auth lives in the external claude CLI, not the relay.
            let claude_bin = {
                let state = app.state::<AppState>();
                let bin = robust_lock(&state.settings).claude_bin.clone();
                bin
            };
            let (program, mut args) = external_cli_invocation(&claude_bin);
            args.extend(["auth".into(), "login".into(), "--claudeai".into()]);
            run_login_streamed(&app, "claude-login", "claude", &program, &args)
        }
        other => Err(format!("Unknown login provider: {other}")),
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Stores a Claude Code OAuth token (from `claude setup-token` run on any
/// browser-equipped machine) in the same 0600 file the CLI's
/// `airelays claude set-token` writes; the relay injects it into every
/// `claude` invocation, so it applies without a restart.
#[tauri::command]
pub async fn set_claude_token(app: AppHandle, token: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let token = token.trim().to_string();
        if token.is_empty() {
            return Err("The token is empty. Paste the full token printed by `claude setup-token`.".into());
        }
        let path = AppSettings::data_dir().join("claude-token");
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("Cannot create data dir: {error}"))?;
        }
        std::fs::write(&path, format!("{token}\n"))
            .map_err(|error| format!("Cannot write the token file: {error}"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
        }
        let state = app.state::<AppState>();
        state.supervisor.log(
            "claude-login",
            "Claude token stored; the relay uses it on the next Claude request.",
            false,
        );
        Ok(())
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Removes the stored Claude token. Needed because a stale stored token
/// overrides the CLI's own login for every relay Claude request — without
/// this, a bad token can permanently mask a valid browser sign-in.
#[tauri::command]
pub async fn clear_claude_token(app: AppHandle) -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let path = AppSettings::data_dir().join("claude-token");
        let existed = path.exists();
        if existed {
            std::fs::remove_file(&path)
                .map_err(|error| format!("Cannot remove the token file: {error}"))?;
            let state = app.state::<AppState>();
            state.supervisor.log(
                "claude-login",
                "Stored Claude token removed; the relay now uses the claude CLI's own sign-in.",
                false,
            );
        }
        Ok(existed)
    })
    .await
    .map_err(|error| error.to_string())?
}

#[derive(Serialize)]
pub struct ClaudeLogoutOutcome {
    /// A stored relay token file existed and was deleted.
    pub token_removed: bool,
    /// `claude auth logout` exited successfully.
    pub cli_signed_out: bool,
    pub cli_error: Option<String>,
}

/// Complete Claude sign-out: the stored relay token AND the claude CLI's
/// own credentials. Token file first — it can mask CLI auth (ghost auth)
/// and must go even on machines without the claude binary; the CLI step's
/// result is reported separately so a partial sign-out is never presented
/// as success.
#[tauri::command]
pub async fn logout_claude(app: AppHandle) -> Result<ClaudeLogoutOutcome, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let path = AppSettings::data_dir().join("claude-token");
        let token_removed = path.exists();
        if token_removed {
            std::fs::remove_file(&path)
                .map_err(|error| format!("Cannot remove the stored token: {error}"))?;
        }
        let claude_bin = {
            let state = app.state::<AppState>();
            let bin = robust_lock(&state.settings).claude_bin.clone();
            bin
        };
        let (program, mut logout_args) = external_cli_invocation(&claude_bin);
        logout_args.extend(["auth".into(), "logout".into()]);
        let (cli_signed_out, cli_error) = match Command::new(&program)
            .args(&logout_args)
            .stdin(Stdio::null())
            .output()
        {
            Ok(output) if output.status.success() => (true, None),
            Ok(output) => {
                let text = format!(
                    "{}\n{}",
                    String::from_utf8_lossy(&output.stdout),
                    String::from_utf8_lossy(&output.stderr)
                );
                (false, Some(concise_error(&text)))
            }
            Err(error) => (false, Some(format!("Cannot run {claude_bin}: {error}"))),
        };
        let state = app.state::<AppState>();
        state.supervisor.log(
            "claude-logout",
            &format!(
                "Claude sign-out: stored token removed={token_removed}, CLI signed out={cli_signed_out}."
            ),
            cli_error.is_some(),
        );
        if let Some(error) = &cli_error {
            state.supervisor.log("claude-logout", error, true);
        }
        Ok(ClaudeLogoutOutcome { token_removed, cli_signed_out, cli_error })
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Manual hard refresh: clears usage-limit benches on the running relay and
/// re-checks capacity, so a recovered account returns to rotation without a
/// restart. Talks to the relay over HTTP because bench state lives in the
/// relay process, not this one.
#[tauri::command]
pub async fn refresh_accounts(app: AppHandle) -> Result<Value, String> {
    let (base_url, requires_auth) = {
        let state = app.state::<AppState>();
        let settings = robust_lock(&state.settings);
        (settings.base_url(), settings.require_bearer_auth)
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .build()
        .map_err(|error| error.to_string())?;
    let mut request = client.post(format!("{base_url}/relay/accounts/refresh"));
    if requires_auth {
        if let Ok(token) = std::fs::read_to_string(AppSettings::bearer_token_file()) {
            request = request.bearer_auth(token.trim());
        }
    }
    let response = request
        .send()
        .await
        .map_err(|_| "The relay is not running.".to_string())?;
    if !response.status().is_success() {
        return Err(format!("Refresh failed ({}).", response.status()));
    }
    response.json().await.map_err(|_| "Unexpected refresh response.".to_string())
}

/// Fetches the relay's model list (all providers), for the Models tab.
#[tauri::command]
pub async fn get_models(app: AppHandle) -> Result<Value, String> {
    let (base_url, requires_auth) = {
        let state = app.state::<AppState>();
        let settings = robust_lock(&state.settings);
        (settings.base_url(), settings.require_bearer_auth)
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|error| error.to_string())?;
    let mut request = client.get(format!("{base_url}/models"));
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
        return Err(format!("Relay answered {}.", response.status()));
    }
    response
        .json()
        .await
        .map_err(|_| "Unexpected models payload.".to_string())
}

/// Fetches per-account usage from the relay's subscription-status endpoint,
/// plus Claude usage (same normalized shape) when that runtime is on.
#[tauri::command]
pub async fn get_usage(app: AppHandle) -> Result<Value, String> {
    let (base_url, requires_auth, claude_on) = {
        let state = app.state::<AppState>();
        let settings = robust_lock(&state.settings);
        (
            settings.base_url(),
            settings.require_bearer_auth,
            settings.claude_effectively_enabled(),
        )
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|error| error.to_string())?;
    let token = if requires_auth {
        std::fs::read_to_string(AppSettings::bearer_token_file())
            .ok()
            .map(|token| token.trim().to_string())
    } else {
        None
    };
    let authed = |mut request: reqwest::RequestBuilder| {
        if let Some(token) = &token {
            request = request.bearer_auth(token);
        }
        request
    };
    // all_accounts folds to the single-account shape when only one exists.
    let response = authed(client.get(format!("{base_url}/subscription/status?all_accounts=true")))
        .send()
        .await
        .map_err(|_| "The relay is not reachable.".to_string())?;
    let mut payload: Value = if response.status().is_success() {
        response
            .json()
            .await
            .map_err(|_| "Unexpected usage payload.".to_string())?
    } else {
        let code = response.status();
        let detail: Value = response.json().await.unwrap_or(Value::Null);
        let message = detail
            .get("detail")
            .and_then(Value::as_str)
            .map(String::from)
            .unwrap_or_else(|| format!("Relay answered {code}."));
        // An OpenAI failure (e.g. the provider is disabled → 501) must not
        // abort the whole command: a Claude-only relay still has Claude
        // usage to show.
        if !claude_on {
            return Err(message);
        }
        serde_json::json!({ "error": message })
    };

    // Claude usage is best-effort: its absence must never break the OpenAI
    // usage display. Failures are forwarded as {"error": ...} so the UI can
    // say WHY usage is missing (e.g. the upstream rate-limits its usage
    // endpoint for up to an hour) instead of showing a blank row.
    if claude_on {
        let claude_value = match authed(
            client.get(format!("{base_url}/subscription/status?provider=claude")),
        )
        .send()
        .await
        {
            Ok(response) if response.status().is_success() => {
                response.json::<Value>().await.ok()
            }
            Ok(response) => {
                let detail: Value = response.json().await.unwrap_or(Value::Null);
                let message = detail
                    .get("detail")
                    .and_then(Value::as_str)
                    .unwrap_or("Usage is temporarily unavailable.")
                    .to_string();
                Some(serde_json::json!({ "error": message }))
            }
            Err(_) => None,
        };
        if let (Some(claude), Some(map)) = (claude_value, payload.as_object_mut()) {
            map.insert("claude".into(), claude);
        }
    }
    Ok(payload)
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

/// Persists the sign-in method ("browser" or "device"). App-level setting;
/// the relay config is untouched.
#[tauri::command]
pub fn set_login_method(app: AppHandle, method: String) -> Result<(), String> {
    if method != "browser" && method != "device" {
        return Err(format!("Unknown sign-in method: {method}"));
    }
    let state = app.state::<AppState>();
    robust_lock(&state.settings).login_method = method;
    state.persist_settings()
}

/// Signs one OpenAI account out (deletes its stored credentials). The relay
/// hot-reloads the pool within a couple of seconds.
#[tauri::command]
pub async fn logout_account(app: AppHandle, email: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        run_relay_cli(&app, "logout", &["logout", &email, "--json"])?.ok_or_concise_error()?;
        crate::tray::refresh(&app);
        Ok(())
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
