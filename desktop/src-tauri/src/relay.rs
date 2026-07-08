//! Relay process supervision: resolve the launch command, spawn, capture
//! output, stop, and report lifecycle.
//!
//! Design constraints (from adversarial review):
//! - never hold the child mutex across sleeps or waits; stop() extracts the
//!   child under the lock and terminates it outside the lock
//! - kill the whole process tree: process groups on Unix, a Job Object on
//!   Windows, so uvicorn workers die with us
//! - command overrides are split quote-aware (paths with spaces)

use crate::settings::AppSettings;
use serde::Serialize;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Lifecycle {
    Stopped,
    Starting,
    Running,
    Stopping,
    Failed,
}

#[derive(Clone, Serialize)]
pub struct ConsoleEntry {
    pub at_ms: u128,
    pub source: String,
    pub text: String,
    pub is_error: bool,
}

const CONSOLE_CAP: usize = 500;
const STOP_GRACE_MS: u64 = 5000;

/// A supervised child plus the platform handle that owns its process tree.
struct Supervised {
    child: Child,
    #[cfg(windows)]
    job: job_object::Job,
}

pub struct RelaySupervisor {
    child: Mutex<Option<Supervised>>,
    pub lifecycle: Mutex<Lifecycle>,
    pub console: Arc<Mutex<Vec<ConsoleEntry>>>,
    /// True from a successful start until an explicit user stop. When the
    /// child dies while this is set, the status loop knows the exit was a
    /// crash and may auto-restart; a user stop never triggers a respawn.
    desired_running: std::sync::atomic::AtomicBool,
}

/// Locks that survive a poisoned mutex: a panic in one worker thread must
/// not take down every other subsystem that shares the state.
pub fn robust_lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl RelaySupervisor {
    pub fn new() -> Self {
        Self {
            child: Mutex::new(None),
            lifecycle: Mutex::new(Lifecycle::Stopped),
            console: Arc::new(Mutex::new(Vec::new())),
            desired_running: std::sync::atomic::AtomicBool::new(false),
        }
    }

    pub fn desired_running(&self) -> bool {
        self.desired_running.load(std::sync::atomic::Ordering::Relaxed)
    }

    /// Resolution order: explicit override from settings, embedded runtime
    /// shipped in the app resources, then `airelays` on PATH.
    pub fn resolve_command(
        settings: &AppSettings,
        resource_dir: Option<PathBuf>,
    ) -> Result<(String, Vec<String>), String> {
        let trimmed = settings.relay_command_override.trim();
        if !trimmed.is_empty() {
            let mut parts = shlex::split(trimmed)
                .ok_or_else(|| "Relay command override has unbalanced quotes.".to_string())?
                .into_iter();
            let program = parts
                .next()
                .ok_or_else(|| "Relay command override is empty.".to_string())?;
            return Ok((program, parts.collect()));
        }
        if let Some(python) = resource_dir.and_then(embedded_python) {
            return Ok((python, vec!["-m".into(), "airelays".into()]));
        }
        Ok(("airelays".into(), Vec::new()))
    }

    pub fn is_managed(&self) -> bool {
        robust_lock(&self.child).is_some()
    }

    pub fn start(
        &self,
        settings: &AppSettings,
        resource_dir: Option<PathBuf>,
    ) -> Result<u32, String> {
        // Held across the spawn: releasing it after only the check opens a
        // TOCTOU window where two concurrent starts both pass and one child
        // leaks unsupervised (tray Start + auto-restart, for example).
        let mut child_slot = robust_lock(&self.child);
        if child_slot.is_some() {
            return Err("The relay is already managed by this app.".into());
        }

        write_relay_config(settings)?;

        let (program, mut args) = Self::resolve_command(settings, resource_dir)?;
        args.push("serve".into());
        args.push("--config".into());
        args.push(AppSettings::relay_config_path().to_string_lossy().into_owned());
        let extra = shlex::split(settings.extra_serve_args.trim())
            .ok_or_else(|| "Extra serve arguments have unbalanced quotes.".to_string())?;
        args.extend(extra);

        self.log("relay", &format!("Starting: {} {}", program, args.join(" ")), false);

        std::fs::create_dir_all(AppSettings::data_dir())
            .map_err(|error| format!("Cannot create data dir: {error}"))?;

        let mut command = Command::new(&program);
        command
            .args(&args)
            .env("PYTHONUNBUFFERED", "1")
            // The embedded runtime lives inside the app bundle; never let it
            // write bytecode next to the sealed/signed files.
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .current_dir(AppSettings::data_dir())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        configure_platform(&mut command);

        let mut child = command
            .spawn()
            .map_err(|error| format!("Cannot start relay ({program}): {error}"))?;

        #[cfg(windows)]
        let job = job_object::Job::assign(&child).map_err(|error| {
            let _ = child.kill();
            format!("Cannot create job object: {error}")
        })?;

        if let Some(stdout) = child.stdout.take() {
            self.pipe_to_console(stdout, "relay", false);
        }
        if let Some(stderr) = child.stderr.take() {
            self.pipe_to_console(stderr, "relay", true);
        }

        let pid = child.id();
        *child_slot = Some(Supervised {
            child,
            #[cfg(windows)]
            job,
        });
        drop(child_slot);
        *robust_lock(&self.lifecycle) = Lifecycle::Starting;
        self.desired_running
            .store(true, std::sync::atomic::Ordering::Relaxed);
        Ok(pid)
    }

    /// Blocking (up to the grace period); callers must not run this on the
    /// UI thread — the commands layer wraps it in spawn_blocking.
    pub fn stop(&self) {
        // Clear intent first so the status loop never mistakes this
        // deliberate stop for a crash and races a respawn.
        self.desired_running
            .store(false, std::sync::atomic::Ordering::Relaxed);
        let supervised = robust_lock(&self.child).take();
        let Some(mut supervised) = supervised else {
            return;
        };
        *robust_lock(&self.lifecycle) = Lifecycle::Stopping;
        self.log("relay", "Stopping relay...", false);
        terminate_tree(&mut supervised);
        let _ = supervised.child.wait();
        *robust_lock(&self.lifecycle) = Lifecycle::Stopped;
        self.log("relay", "Relay stopped.", false);
    }

    /// Reconciles lifecycle with the actual child state; returns true when
    /// the process died since the last check.
    pub fn reap_if_exited(&self) -> bool {
        let mut guard = robust_lock(&self.child);
        if let Some(supervised) = guard.as_mut() {
            if let Ok(Some(status)) = supervised.child.try_wait() {
                *guard = None;
                drop(guard);
                let failed = !status.success();
                *robust_lock(&self.lifecycle) =
                    if failed { Lifecycle::Failed } else { Lifecycle::Stopped };
                self.log("relay", &format!("Relay process exited ({status})."), failed);
                return true;
            }
        }
        false
    }

    pub fn mark_running(&self) {
        let mut lifecycle = robust_lock(&self.lifecycle);
        if *lifecycle == Lifecycle::Starting {
            *lifecycle = Lifecycle::Running;
        }
    }

    pub fn log(&self, source: &str, text: &str, is_error: bool) {
        push_console(&self.console, source, text, is_error);
    }

    pub fn console_handle(&self) -> Arc<Mutex<Vec<ConsoleEntry>>> {
        Arc::clone(&self.console)
    }

    fn pipe_to_console(
        &self,
        stream: impl std::io::Read + Send + 'static,
        source: &'static str,
        is_error: bool,
    ) {
        spawn_console_pipe(self.console_handle(), stream, source.to_string(), is_error);
    }
}

/// Streams a reader line-by-line into the console buffer from a thread.
pub fn spawn_console_pipe(
    console: Arc<Mutex<Vec<ConsoleEntry>>>,
    stream: impl std::io::Read + Send + 'static,
    source: String,
    is_error: bool,
) {
    std::thread::spawn(move || {
        let reader = BufReader::new(stream);
        for line in reader.lines().map_while(Result::ok) {
            push_console(&console, &source, &line, is_error);
        }
    });
}

/// Access-log lines produced by the app's own 1.5 s status polling (and
/// health probes) would drown out everything meaningful in the console.
fn is_polling_noise(text: &str) -> bool {
    (text.contains("GET /v1/relay/status HTTP") || text.contains("GET /healthz HTTP"))
        && text.contains(" 200 ")
}

pub fn push_console(
    console: &Arc<Mutex<Vec<ConsoleEntry>>>,
    source: &str,
    text: &str,
    is_error: bool,
) {
    let trimmed = text.trim();
    if trimmed.is_empty() || is_polling_noise(trimmed) {
        return;
    }
    let mut entries = robust_lock(console);
    entries.push(ConsoleEntry {
        at_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis(),
        source: source.into(),
        text: trimmed.into(),
        is_error,
    });
    let overflow = entries.len().saturating_sub(CONSOLE_CAP);
    if overflow > 0 {
        entries.drain(..overflow);
    }
}

/// GUI apps on macOS (and some Linux sessions) inherit a minimal PATH
/// without the user's bin directories, so PATH-installed tools are "not
/// found" even though they work in a terminal. Extending the process PATH
/// once at startup fixes every child spawn — relay, login flows, doctor —
/// in one place.
pub fn extend_path_for_gui() {
    let home = AppSettings::home_dir();
    let extras = [
        home.join(".local").join("bin"),
        home.join("bin"),
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/usr/local/bin"),
    ];
    let current = std::env::var_os("PATH").unwrap_or_default();
    let mut parts: Vec<PathBuf> = std::env::split_paths(&current).collect();
    for extra in extras {
        if extra.is_dir() && !parts.contains(&extra) {
            parts.push(extra);
        }
    }
    if let Ok(joined) = std::env::join_paths(parts) {
        std::env::set_var("PATH", joined);
    }
}

pub fn write_relay_config(settings: &AppSettings) -> Result<(), String> {
    settings.validate()?;
    let rendered = settings.render_config_toml()?;
    let path = AppSettings::relay_config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|error| format!("Cannot create config dir: {error}"))?;
    }
    std::fs::write(&path, rendered).map_err(|error| format!("Cannot write relay config: {error}"))?;
    // The config reveals the security posture; match the CLI's 0600.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

fn embedded_python(resource_dir: PathBuf) -> Option<String> {
    let candidates = if cfg!(windows) {
        vec![resource_dir.join("runtime").join("python.exe")]
    } else {
        // bin/python3 is normally a symlink; fall back to the real binary in
        // case a bundler dropped symlinks during resource copying.
        vec![
            resource_dir.join("runtime").join("bin").join("python3"),
            resource_dir.join("runtime").join("bin").join("python3.13"),
        ]
    };
    candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .map(|candidate| candidate.to_string_lossy().into_owned())
}

/// Applies to every subprocess we spawn (relay and CLI runs alike).
pub fn configure_platform(command: &mut Command) {
    // GUI apps launched from Finder/launchd inherit a minimal PATH
    // (/usr/bin:/bin:...), so user-installed tools in ~/.local/bin or
    // /opt/homebrew/bin are invisible. Use the login shell's PATH for
    // children so they see what the user's terminal sees.
    if let Some(path) = login_shell_path() {
        command.env("PATH", path);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // Own process group so stop() can signal the whole tree.
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
}

/// The user's interactive-shell PATH, resolved once per app run.
#[cfg(unix)]
fn login_shell_path() -> Option<String> {
    use std::sync::OnceLock;
    static PATH: OnceLock<Option<String>> = OnceLock::new();
    PATH.get_or_init(|| {
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/sh".into());
        let output = Command::new(shell)
            .args(["-lc", "echo $PATH"])
            .output()
            .ok()?;
        let path = String::from_utf8_lossy(&output.stdout).trim().to_string();
        // Merge with the inherited PATH so nothing the app already had is lost.
        let inherited = std::env::var("PATH").unwrap_or_default();
        if path.is_empty() {
            return None;
        }
        Some(if inherited.is_empty() {
            path
        } else {
            format!("{path}:{inherited}")
        })
    })
    .clone()
}

/// Windows GUI apps inherit the user's full PATH already.
#[cfg(windows)]
fn login_shell_path() -> Option<String> {
    None
}

#[cfg(unix)]
fn terminate_tree(supervised: &mut Supervised) {
    // SIGTERM the process group so uvicorn and any subprocesses can shut
    // down cleanly; escalate to SIGKILL after the grace period.
    let pgid = supervised.child.id() as i32;
    unsafe {
        libc::kill(-pgid, libc::SIGTERM);
    }
    for _ in 0..(STOP_GRACE_MS / 100) {
        if matches!(supervised.child.try_wait(), Ok(Some(_))) {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    unsafe {
        libc::kill(-pgid, libc::SIGKILL);
    }
}

#[cfg(windows)]
fn terminate_tree(supervised: &mut Supervised) {
    // Closing the job handle kills the entire tree
    // (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE). TerminateProcess semantics are
    // abrupt, but Windows offers no SIGTERM for windowless children; the
    // job object at least guarantees no orphaned uvicorn processes.
    supervised.job.terminate();
    let _ = supervised.child.kill();
}

#[cfg(windows)]
mod job_object {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    pub struct Job(HANDLE);

    // HANDLE is a raw pointer; the job handle is only used from the
    // supervisor's locked sections.
    unsafe impl Send for Job {}

    impl Job {
        pub fn assign(child: &std::process::Child) -> Result<Self, String> {
            unsafe {
                let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if job.is_null() {
                    return Err("CreateJobObject failed".into());
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                ) == 0
                {
                    CloseHandle(job);
                    return Err("SetInformationJobObject failed".into());
                }
                if AssignProcessToJobObject(job, child.as_raw_handle() as HANDLE) == 0 {
                    CloseHandle(job);
                    return Err("AssignProcessToJobObject failed".into());
                }
                Ok(Self(job))
            }
        }

        pub fn terminate(&self) {
            unsafe {
                TerminateJobObject(self.0, 1);
            }
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}
