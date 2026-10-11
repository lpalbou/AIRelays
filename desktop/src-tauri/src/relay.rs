//! Relay process supervision: resolve the launch command, spawn, capture
//! output, stop, and report lifecycle.
//!
//! Design constraints (from adversarial review):
//! - never hold the child mutex across sleeps or waits; stop() extracts the
//!   child under the lock and terminates it outside the lock
//! - kill the whole process tree: process groups on Unix, a Job Object on
//!   Windows, so `claude` subprocesses and uvicorn workers die with us
//! - command overrides are split quote-aware (paths with spaces)

use crate::settings::{ensure_private_dir, write_private, AppSettings};
use serde::Serialize;
use std::ffi::OsString;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, MutexGuard, OnceLock};
#[cfg(unix)]
use std::time::Duration;
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

/// The bundled interpreter runs isolated (ADR 0008): `-I` ignores PYTHON*
/// variables, the user site-packages and the working directory, so nothing
/// outside the app can shadow the bundled relay; `-B -u` replace the
/// PYTHONDONTWRITEBYTECODE/PYTHONUNBUFFERED variables that `-I` ignores;
/// `-X utf8` makes piped output UTF-8 on every OS. AIRELAYS_* variables are
/// read by the relay itself and keep working.
const EMBEDDED_PYTHON_FLAGS: [&str; 6] = ["-I", "-B", "-u", "-X", "utf8", "-m"];

/// What the AppImage runtime (APPIMAGE, APPDIR, ARGV0, OWD) and the bundled
/// linuxdeploy GTK hook (`apprun-hooks/linuxdeploy-plugin-gtk.sh`, verified
/// in the v0.16.0 AppImage) export into this process. They point into the
/// app's mount, so a child that inherits them (the relay, then a browser or
/// file manager it opens) loads this app's GTK modules and schemas instead
/// of the system's. Exact names only: a wildcard would also drop variables
/// the user set, such as GTK_IM_MODULE (input methods) or GDK_SCALE.
/// XDG_DATA_DIRS is restored separately (the hook prepends to it).
const APPIMAGE_INJECTED_VARS: [&str; 14] = [
    "APPDIR",
    "APPIMAGE",
    "ARGV0",
    "OWD",
    "GTK_DATA_PREFIX",
    "GTK_THEME",
    "GTK_EXE_PREFIX",
    "GTK_PATH",
    "GTK_IM_MODULE_FILE",
    "GDK_BACKEND",
    "GDK_PIXBUF_MODULE_FILE",
    "GSETTINGS_SCHEMA_DIR",
    "GI_TYPELIB_PATH",
    "GIO_EXTRA_MODULES",
];

/// Upper bound for the one-time login-shell PATH probe; a profile script
/// that waits on something must not stall relay starts.
#[cfg(unix)]
const LOGIN_SHELL_TIMEOUT: Duration = Duration::from_secs(5);
#[cfg(unix)]
const PATH_BEGIN_MARKER: &str = "__AIRELAYS_PATH_BEGIN__";
#[cfg(unix)]
const PATH_END_MARKER: &str = "__AIRELAYS_PATH_END__";

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
            let mut args: Vec<String> =
                EMBEDDED_PYTHON_FLAGS.iter().map(|flag| flag.to_string()).collect();
            args.push("airelays".into());
            return Ok((python, args));
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

        ensure_private_dir(&AppSettings::data_dir())
            .map_err(|error| format!("Cannot create data dir: {error}"))?;

        let mut command = relay_process(&program, &args);
        command
            .current_dir(AppSettings::data_dir())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

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
/// without the user's bin directories, so external tools like the `claude`
/// CLI are "not found" even though they work in a terminal. Extending the
/// process PATH once at startup fixes every child spawn — relay, login
/// flows, doctor — in one place.
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
    // The config reveals the security posture: owner-only from creation,
    // replaced atomically so a relay starting concurrently never reads a
    // half-written file.
    write_private(&AppSettings::relay_config_path(), rendered.as_bytes())
        .map_err(|error| format!("Cannot write relay config: {error}"))
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

/// Command for the relay or one of its CLI subcommands. The bundled
/// interpreter ignores PYTHON* variables (`-I`) and gets `-B -u -X utf8`
/// instead; the variables stay for a relay command override or an
/// `airelays` found on PATH, whose streamed output (sign-in URLs) depends
/// on unbuffered stdout.
pub fn relay_process(program: &str, args: &[String]) -> Command {
    let mut command = Command::new(program);
    command
        .args(args)
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONUTF8", "1");
    configure_child(&mut command);
    command
}

/// The one place every child this app spawns is configured (relay, CLI
/// commands, sign-in flows, openers, taskkill): environment hygiene, PATH,
/// process group, and no console window on Windows.
pub fn configure_child(command: &mut Command) {
    apply_child_env(command);
    // GUI apps launched from Finder/launchd inherit a minimal PATH
    // (/usr/bin:/bin:...), so user-installed tools like `claude` in
    // ~/.local/bin or /opt/homebrew/bin are invisible. Use the login
    // shell's PATH for children so they see what the user's terminal sees.
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

/// Environment changes applied to every child, computed once from this
/// process's environment.
#[derive(Debug, Default, PartialEq)]
struct ChildEnvChanges {
    remove: Vec<&'static str>,
    set: Vec<(&'static str, OsString)>,
}

fn apply_child_env(command: &mut Command) {
    static CHANGES: OnceLock<ChildEnvChanges> = OnceLock::new();
    let changes = CHANGES.get_or_init(|| appimage_env_changes(|name| std::env::var_os(name)));
    for name in &changes.remove {
        command.env_remove(name);
    }
    for (name, value) in &changes.set {
        command.env(name, value);
    }
}

/// Undoes what the AppImage runtime and its GTK hook added to this
/// process's environment. Outside an AppImage nothing changes.
fn appimage_env_changes(get: impl Fn(&str) -> Option<OsString>) -> ChildEnvChanges {
    let mut changes = ChildEnvChanges::default();
    if get("APPIMAGE").is_none() {
        return changes;
    }
    changes.remove.extend(APPIMAGE_INJECTED_VARS);
    let appdir = get("APPDIR").and_then(|value| value.into_string().ok());
    // A value that is not UTF-8 was not built by the hook's prefixing in a
    // form we can edit safely; leave it as it is.
    if let Some(Ok(value)) = get("XDG_DATA_DIRS").map(OsString::into_string) {
        match restore_xdg_data_dirs(&value, appdir.as_deref()) {
            Some(restored) if restored != value => {
                changes.set.push(("XDG_DATA_DIRS", OsString::from(restored)));
            }
            Some(_) => {}
            None => changes.remove.push("XDG_DATA_DIRS"),
        }
    }
    changes
}

/// The hook sets `XDG_DATA_DIRS="$APPDIR/usr/share:/usr/share:$XDG_DATA_DIRS"`.
/// Returns the original value, or None when it was unset or empty (the
/// XDG default then applies again). If the value was changed since, only
/// the entries inside the AppImage mount are dropped.
fn restore_xdg_data_dirs(value: &str, appdir: Option<&str>) -> Option<String> {
    let Some(appdir) = appdir.map(|dir| dir.trim_end_matches('/')).filter(|dir| !dir.is_empty())
    else {
        return Some(value.to_string());
    };
    let original = match value.strip_prefix(&format!("{appdir}/usr/share:/usr/share:")) {
        Some(rest) => rest.to_string(),
        None => {
            let inside = format!("{appdir}/");
            value
                .split(':')
                .filter(|entry| *entry != appdir && !entry.starts_with(&inside))
                .collect::<Vec<_>>()
                .join(":")
        }
    };
    if original.split(':').all(str::is_empty) {
        None
    } else {
        Some(original)
    }
}

/// The user's login-shell PATH, resolved once per app run, bounded by
/// LOGIN_SHELL_TIMEOUT. Merged in front of the inherited PATH so nothing
/// the app already had is lost.
#[cfg(unix)]
fn login_shell_path() -> Option<String> {
    static PATH: OnceLock<Option<String>> = OnceLock::new();
    PATH.get_or_init(|| {
        let path = probe_login_shell_path()?;
        let inherited = std::env::var("PATH").unwrap_or_default();
        Some(if inherited.is_empty() {
            path
        } else {
            format!("{path}:{inherited}")
        })
    })
    .clone()
}

/// Runs `$SHELL -lc` once. `printenv PATH` prints the exported, colon-joined
/// PATH in every shell (fish's `echo $PATH` prints a space-separated list),
/// and the markers fence off anything profile scripts print. Reading stops
/// at the end marker, so a background process a profile starts cannot hold
/// the pipe open past the timeout.
#[cfg(unix)]
fn probe_login_shell_path() -> Option<String> {
    use std::os::unix::process::CommandExt;
    let shell = std::env::var("SHELL")
        .ok()
        .filter(|shell| !shell.is_empty())
        .unwrap_or_else(|| "/bin/sh".into());
    let mut command = Command::new(shell);
    command
        .args([
            "-lc",
            &format!("echo {PATH_BEGIN_MARKER}; printenv PATH; echo {PATH_END_MARKER}"),
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0);
    apply_child_env(&mut command);
    let mut child = command.spawn().ok()?;
    let stdout = child.stdout.take()?;
    let (sender, receiver) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let lines = BufReader::new(stdout).lines().map_while(Result::ok);
        let _ = sender.send(parse_marked_path(lines));
    });
    let path = receiver.recv_timeout(LOGIN_SHELL_TIMEOUT).ok().flatten();
    if path.is_none() {
        // SAFETY: the shell leads its own process group (process_group(0))
        // and is not reaped yet, so this group id cannot have been reused.
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
    }
    // Reap without blocking the caller.
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    path
}

/// The first non-empty line between the begin and end markers.
#[cfg(unix)]
fn parse_marked_path(lines: impl Iterator<Item = String>) -> Option<String> {
    let mut inside = false;
    let mut path = None;
    for line in lines {
        let line = line.trim();
        if line == PATH_BEGIN_MARKER {
            inside = true;
        } else if line == PATH_END_MARKER {
            break;
        } else if inside && path.is_none() && !line.is_empty() {
            path = Some(line.to_string());
        }
    }
    path
}

/// Windows GUI apps inherit the user's full PATH already.
#[cfg(windows)]
fn login_shell_path() -> Option<String> {
    None
}

#[cfg(unix)]
fn terminate_tree(supervised: &mut Supervised) {
    // SIGTERM the process group so uvicorn and any claude subprocesses can
    // shut down cleanly; escalate to SIGKILL after the grace period.
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
    // job object at least guarantees no orphaned uvicorn/claude processes.
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env_of(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<OsString> {
        let map: HashMap<String, OsString> = pairs
            .iter()
            .map(|(name, value)| (name.to_string(), OsString::from(value)))
            .collect();
        move |name| map.get(name).cloned()
    }

    #[test]
    fn outside_an_appimage_the_environment_is_untouched() {
        let changes = appimage_env_changes(env_of(&[
            ("APPDIR", "/opt/somewhere"),
            ("GTK_PATH", "/usr/lib/gtk-3.0"),
            ("XDG_DATA_DIRS", "/usr/local/share:/usr/share"),
        ]));
        assert_eq!(changes, ChildEnvChanges::default());
    }

    #[test]
    fn appimage_hook_variables_are_removed_by_exact_name_only() {
        let appdir = "/tmp/.mount_AIRelaXYZ";
        let changes = appimage_env_changes(env_of(&[
            ("APPIMAGE", "/home/u/.local/bin/airelays-desktop"),
            ("APPDIR", appdir),
            ("GTK_IM_MODULE", "fcitx"),
            ("GDK_SCALE", "2"),
            ("XDG_DATA_DIRS", &format!("{appdir}/usr/share:/usr/share:/usr/local/share:/usr/share")),
        ]));
        for name in APPIMAGE_INJECTED_VARS {
            assert!(changes.remove.contains(&name), "{name} must be removed");
        }
        assert!(!changes.remove.contains(&"GTK_IM_MODULE"));
        assert!(!changes.remove.contains(&"GDK_SCALE"));
        assert_eq!(
            changes.set,
            vec![("XDG_DATA_DIRS", OsString::from("/usr/local/share:/usr/share"))]
        );
    }

    #[test]
    fn xdg_data_dirs_set_only_by_the_hook_is_unset_again() {
        let appdir = "/tmp/.mount_AIRelaXYZ";
        let changes = appimage_env_changes(env_of(&[
            ("APPIMAGE", "/x.AppImage"),
            ("APPDIR", appdir),
            ("XDG_DATA_DIRS", &format!("{appdir}/usr/share:/usr/share:")),
        ]));
        assert!(changes.remove.contains(&"XDG_DATA_DIRS"));
        assert!(changes.set.is_empty());
    }

    #[test]
    fn xdg_data_dirs_edited_later_only_loses_mount_entries() {
        assert_eq!(
            restore_xdg_data_dirs("/opt/a:/tmp/.mount_X/usr/share:/usr/share", Some("/tmp/.mount_X")),
            Some("/opt/a:/usr/share".into())
        );
        assert_eq!(
            restore_xdg_data_dirs("/usr/share", None),
            Some("/usr/share".into())
        );
    }

    #[test]
    fn embedded_interpreter_runs_isolated() {
        let nanos = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let resources = std::env::temp_dir().join(format!("airelays-relay-test-{}-{nanos}", std::process::id()));
        let python = if cfg!(windows) {
            resources.join("runtime").join("python.exe")
        } else {
            resources.join("runtime").join("bin").join("python3")
        };
        std::fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::fs::write(&python, b"").unwrap();
        let (program, args) =
            RelaySupervisor::resolve_command(&AppSettings::default(), Some(resources.clone())).unwrap();
        assert_eq!(program, python.to_string_lossy());
        assert_eq!(args, ["-I", "-B", "-u", "-X", "utf8", "-m", "airelays"]);
        std::fs::remove_dir_all(&resources).unwrap();
    }

    #[test]
    fn relay_command_override_is_not_rewritten() {
        let settings = AppSettings {
            relay_command_override: "\"/opt/my relay/airelays\" --verbose".into(),
            ..AppSettings::default()
        };
        let (program, args) = RelaySupervisor::resolve_command(&settings, None).unwrap();
        assert_eq!(program, "/opt/my relay/airelays");
        assert_eq!(args, ["--verbose"]);
    }

    #[cfg(unix)]
    #[test]
    fn login_shell_output_is_read_between_markers() {
        let lines = [
            "Welcome to the machine",
            PATH_BEGIN_MARKER,
            "/home/u/.local/bin:/usr/bin",
            PATH_END_MARKER,
            "/ignored",
        ]
        .map(String::from);
        assert_eq!(
            parse_marked_path(lines.into_iter()),
            Some("/home/u/.local/bin:/usr/bin".into())
        );
        assert_eq!(parse_marked_path(["/usr/bin".to_string()].into_iter()), None);
    }
}
