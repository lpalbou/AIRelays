//! Update check and in-place update for the desktop app.
//!
//! The check reads GitHub's release list. Installing an update runs the
//! same one-line installer users run by hand (scripts/install-desktop.*,
//! compiled into the app), pinned to the release the check found: its
//! version and the SHA-256 digest GitHub publishes for its installer, which
//! the installer enforces. A detached wrapper (update-wrapper.*) waits for
//! this app to exit, so the relay stops cleanly and no file is in use, then
//! reopens AIRelays whether the install succeeded or not.

use crate::relay::{configure_child, robust_lock};
use crate::settings::AppSettings;
use serde::Serialize;
use serde_json::Value;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::AppHandle;

const RELEASES_API: &str = "https://api.github.com/repos/lpalbou/AIRelays/releases?per_page=20";
/// Release notes longer than this are cut; the release page has the rest.
const NOTES_LIMIT: usize = 6000;
/// How long the app waits for the updater to report that it runs.
const READY_TIMEOUT: Duration = Duration::from_secs(10);

#[cfg(windows)]
const INSTALLER_SCRIPT: (&str, &str) =
    ("install-desktop.ps1", include_str!("../../../scripts/install-desktop.ps1"));
#[cfg(windows)]
const WRAPPER_SCRIPT: (&str, &str) = ("run-update.ps1", include_str!("update-wrapper.ps1"));
#[cfg(not(windows))]
const INSTALLER_SCRIPT: (&str, &str) =
    ("install-desktop.sh", include_str!("../../../scripts/install-desktop.sh"));
#[cfg(not(windows))]
const WRAPPER_SCRIPT: (&str, &str) = ("run-update.sh", include_str!("update-wrapper.sh"));

/// Installer asset published for this platform's desktop build.
fn asset_suffix() -> Option<&'static str> {
    if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        Some("_x64-setup.exe")
    } else if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        Some("_aarch64.dmg")
    } else if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
        Some("_amd64.AppImage")
    } else {
        None
    }
}

#[derive(Serialize, Debug, Default)]
pub struct UpdateInfo {
    pub current: String,
    /// Newest stable release that ships an installer for this platform.
    pub latest: Option<String>,
    pub update_available: bool,
    pub release_url: Option<String>,
    pub release_notes: Option<String>,
    pub published_at: Option<String>,
    /// True when this copy can update itself in place.
    pub can_install: bool,
    /// Why it cannot, when it cannot (shown instead of the update button).
    pub install_hint: Option<String>,
    /// Where the updater writes its progress.
    pub log_path: String,
}

/// The release the last check offered: the only one `launch` installs.
#[derive(Clone, Debug, PartialEq)]
struct Offer {
    version: String,
    sha256: String,
}

static OFFER: Mutex<Option<Offer>> = Mutex::new(None);
/// Set once an updater has started: a second one must not race it.
static UPDATING: AtomicBool = AtomicBool::new(false);

/// The argument the updater reopens the app with.
pub const AFTER_UPDATE_ARG: &str = "--after-update";

/// Parses a stable "X.Y.Z" (optional leading "v"). Pre-releases and other
/// suffixes return None, so they are never offered as updates.
pub fn parse_version(text: &str) -> Option<(u64, u64, u64)> {
    let text = text.strip_prefix('v').unwrap_or(text);
    let mut parts = text.split('.');
    let mut next = || {
        let part = parts.next()?;
        // u64::from_str also accepts "+1"; versions are plain digits.
        if part.is_empty() || !part.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        part.parse::<u64>().ok()
    };
    let version = (next()?, next()?, next()?);
    parts.next().is_none().then_some(version)
}

/// The numeric core of the running version: "0.18.0-rc1" compares as
/// 0.18.0, so a pre-release build is still offered the final release.
fn parse_current(text: &str) -> Option<(u64, u64, u64)> {
    parse_version(text.split(['-', '+']).next().unwrap_or(text))
}

/// The installer asset of `release` for this platform, if attached.
fn installer_asset<'a>(release: &'a Value, suffix: &str) -> Option<&'a Value> {
    release["assets"]
        .as_array()?
        .iter()
        .find(|asset| asset["name"].as_str().is_some_and(|name| name.ends_with(suffix)))
}

/// Picks the newest stable release carrying an installer that ends with
/// `suffix` from a GitHub `/releases` payload.
fn newest_release<'a>(releases: &'a [Value], suffix: &str) -> Option<(&'a Value, (u64, u64, u64))> {
    releases
        .iter()
        .filter(|release| {
            !release["draft"].as_bool().unwrap_or(true)
                && !release["prerelease"].as_bool().unwrap_or(true)
        })
        .filter(|release| installer_asset(release, suffix).is_some())
        .filter_map(|release| Some((release, parse_version(release["tag_name"].as_str()?)?)))
        .max_by_key(|(_, version)| *version)
}

/// GitHub's "sha256:<hex>" asset digest, as bare lowercase hex.
fn asset_sha256(asset: &Value) -> Option<String> {
    let hex = asset["digest"].as_str()?.strip_prefix("sha256:")?;
    (hex.len() == 64 && hex.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .then(|| hex.to_ascii_lowercase())
}

fn truncate_notes(body: &str) -> String {
    let body = body.trim();
    if body.chars().count() <= NOTES_LIMIT {
        return body.to_string();
    }
    let cut: String = body.chars().take(NOTES_LIMIT).collect();
    // End on a line boundary so the cut never splits a sentence mid-word.
    let cut = cut.rfind('\n').map_or(cut.as_str(), |end| &cut[..end]);
    format!("{cut}\n\n…")
}

/// The check result, and the offer to remember when the update can be
/// installed from here.
fn summarize(current: &str, releases: &[Value], suffix: &str) -> (UpdateInfo, Option<Offer>) {
    let mut info = UpdateInfo { current: current.to_string(), ..Default::default() };
    let Some((release, latest)) = newest_release(releases, suffix) else {
        return (info, None);
    };
    let version = format!("{}.{}.{}", latest.0, latest.1, latest.2);
    info.update_available = parse_current(current).is_some_and(|current| latest > current);
    info.release_url = release["html_url"].as_str().map(str::to_string);
    info.release_notes = release["body"].as_str().map(truncate_notes).filter(|notes| !notes.is_empty());
    info.published_at = release["published_at"].as_str().map(str::to_string);
    info.latest = Some(version.clone());
    if !info.update_available {
        return (info, None);
    }
    match installer_asset(release, suffix).and_then(asset_sha256) {
        Some(sha256) => (info, Some(Offer { version, sha256 })),
        None => {
            info.install_hint = Some(format!("GitHub lists no checksum for this installer. {MANUAL_HINT}"));
            (info, None)
        }
    }
}

pub async fn check(app: &AppHandle) -> Result<UpdateInfo, String> {
    let current = app.package_info().version.to_string();
    let Some(suffix) = asset_suffix() else {
        return Err("No desktop build is published for this system.".into());
    };
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .user_agent(format!("airelays-desktop/{current}"))
        .build()
        .map_err(|error| error.to_string())?;
    let response = client
        .get(RELEASES_API)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|_| "Cannot reach GitHub. Check your internet connection and try again.".to_string())?;
    let status = response.status();
    // GitHub also sends remaining=0 on the last successful call of a
    // window, so only a refused call counts as rate-limited.
    let exhausted = response
        .headers()
        .get("x-ratelimit-remaining")
        .is_some_and(|remaining| remaining == "0");
    if (status == reqwest::StatusCode::FORBIDDEN && exhausted)
        || status == reqwest::StatusCode::TOO_MANY_REQUESTS
    {
        return Err("GitHub is limiting update checks from this network. Try again in an hour, \
                    or open the releases page."
            .into());
    }
    if !status.is_success() {
        return Err(format!("GitHub answered {status}. Try again later."));
    }
    let releases: Vec<Value> =
        response.json().await.map_err(|_| "Unexpected answer from GitHub.".to_string())?;
    let (mut info, mut offer) = summarize(&current, &releases, suffix);
    info.log_path = log_path().to_string_lossy().into_owned();
    if info.update_available && info.install_hint.is_none() {
        match install_target() {
            Ok(_) => info.can_install = offer.is_some(),
            Err(hint) => {
                info.install_hint = Some(hint);
                offer = None;
            }
        }
    }
    *robust_lock(&OFFER) = offer;
    Ok(info)
}

/// Where the installer would put the app, when this copy is that install.
/// A copy elsewhere (a dev build, a .deb, an AppImage outside
/// ~/.local/bin, an app run from its DMG) is not updated in place: the
/// installer would write a second copy instead of replacing this one.
#[derive(Debug)]
struct InstallTarget {
    /// The installed app, reopened after the update.
    relaunch: PathBuf,
    /// macOS: the folder holding AIRelays.app (AIRELAYS_APP_DIR).
    app_dir: Option<PathBuf>,
}

const MANUAL_HINT: &str = "Download the update from the release page.";

#[cfg(windows)]
fn install_target() -> Result<InstallTarget, String> {
    use std::path::Path;
    let exe = std::env::current_exe().map_err(|error| error.to_string())?;
    let local = std::env::var_os("LOCALAPPDATA").ok_or("LOCALAPPDATA is not set.")?;
    let expected = Path::new(&local).join("AIRelays").join("airelays-desktop.exe");
    let canonical = |path: &Path| {
        std::fs::canonicalize(path)
            .unwrap_or_else(|_| path.to_path_buf())
            .to_string_lossy()
            .to_lowercase()
    };
    if canonical(&exe) == canonical(&expected) {
        Ok(InstallTarget { relaunch: exe, app_dir: None })
    } else {
        Err(format!("This copy runs from {}, not the installed location. {MANUAL_HINT}", exe.display()))
    }
}

#[cfg(target_os = "macos")]
fn install_target() -> Result<InstallTarget, String> {
    let exe = std::env::current_exe().map_err(|error| error.to_string())?;
    // .../AIRelays.app/Contents/MacOS/airelays-desktop
    let bundle = exe
        .ancestors()
        .nth(3)
        .filter(|bundle| bundle.file_name().is_some_and(|name| name == "AIRelays.app"))
        .ok_or_else(|| format!("This copy is not an installed AIRelays.app. {MANUAL_HINT}"))?;
    // A disk image, or Gatekeeper's read-only App Translocation copy of an
    // app opened straight from Downloads.
    let path = bundle.to_string_lossy();
    if bundle.starts_with("/Volumes") || path.contains("/AppTranslocation/") {
        return Err("AIRelays is not running from an Applications folder. Move it to \
                    Applications and reopen it, or download the update from the release page."
            .into());
    }
    let app_dir = bundle.parent().ok_or("Unexpected app location.")?.to_path_buf();
    // The installer stages the new app next to the old one.
    let probe = app_dir.join(format!(".airelays-write-test-{}", std::process::id()));
    if std::fs::create_dir(&probe).and_then(|_| std::fs::remove_dir(&probe)).is_err() {
        return Err(format!(
            "You cannot write to {}, so AIRelays cannot replace itself there. {MANUAL_HINT}",
            app_dir.display()
        ));
    }
    Ok(InstallTarget { relaunch: bundle.to_path_buf(), app_dir: Some(app_dir) })
}

#[cfg(target_os = "linux")]
fn install_target() -> Result<InstallTarget, String> {
    use std::path::Path;
    let appimage = std::env::var_os("APPIMAGE").map(PathBuf::from).ok_or_else(|| {
        format!("Only the AppImage installed by the AIRelays installer updates itself. {MANUAL_HINT}")
    })?;
    let home = std::env::var_os("HOME").ok_or("HOME is not set.")?;
    let expected = Path::new(&home).join(".local/bin/airelays-desktop");
    let canonical = |path: &Path| std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    if canonical(&appimage) == canonical(&expected) {
        Ok(InstallTarget { relaunch: expected, app_dir: None })
    } else {
        Err(format!("This AppImage runs from {}, not ~/.local/bin. {MANUAL_HINT}", appimage.display()))
    }
}

#[cfg(not(any(windows, target_os = "macos", target_os = "linux")))]
fn install_target() -> Result<InstallTarget, String> {
    Err(MANUAL_HINT.into())
}

/// The updater's scripts, log and markers, under the relay's data folder.
fn update_dir() -> PathBuf {
    AppSettings::data_dir().join("update")
}

pub fn log_path() -> PathBuf {
    update_dir().join("update.log")
}

fn ready_path() -> PathBuf {
    update_dir().join("updater.ready")
}

fn failed_path() -> PathBuf {
    update_dir().join("update.failed")
}

/// The reason the last update failed, once: reading clears it.
pub fn take_failure() -> Option<String> {
    let path = failed_path();
    let bytes = std::fs::read(&path).ok()?;
    let _ = std::fs::remove_file(&path);
    let text = String::from_utf8_lossy(&bytes);
    let reason = text.trim().trim_start_matches('\u{feff}').trim();
    Some(if reason.is_empty() { "The installer failed.".to_string() } else { reason.to_string() })
}

/// What the dashboard reports after the updater reopened the app.
#[derive(Serialize, Debug)]
pub struct UpdateResult {
    /// True when this launch is the updater's reopen.
    pub after_update: bool,
    /// Why the update failed, when it did.
    pub failure: Option<String>,
}

pub fn take_result(after_update: bool) -> UpdateResult {
    UpdateResult { after_update, failure: take_failure() }
}

fn write_script(name: &str, text: &str) -> Result<PathBuf, String> {
    let path = update_dir().join(name);
    // Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page.
    let bom = if cfg!(windows) { "\u{feff}" } else { "" };
    std::fs::write(&path, format!("{bom}{text}"))
        .map_err(|error| format!("Cannot write {}: {error}", path.display()))?;
    Ok(path)
}

/// Starts the updater for `version`, which must be the release the last
/// check offered, and returns once the updater reports that it runs. The
/// caller then quits the app; the updater does the rest.
pub fn launch(version: &str) -> Result<(), String> {
    if UPDATING.swap(true, Ordering::SeqCst) {
        return Err("An update is already running.".into());
    }
    let result = start_updater(version);
    if result.is_err() {
        UPDATING.store(false, Ordering::SeqCst);
    }
    result
}

fn remove_if_present(path: &std::path::Path) -> Result<(), String> {
    match std::fs::remove_file(path) {
        Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
            Err(format!("Cannot remove {}: {error}", path.display()))
        }
        _ => Ok(()),
    }
}

fn start_updater(version: &str) -> Result<(), String> {
    let offer = robust_lock(&OFFER)
        .clone()
        .filter(|offer| offer.version == version)
        .ok_or("Check for updates again before installing.")?;
    let target = install_target()?;
    let dir = update_dir();
    std::fs::create_dir_all(&dir).map_err(|error| format!("Cannot create {}: {error}", dir.display()))?;
    let installer = write_script(INSTALLER_SCRIPT.0, INSTALLER_SCRIPT.1)?;
    let wrapper = write_script(WRAPPER_SCRIPT.0, WRAPPER_SCRIPT.1)?;
    // A stale ready marker would pass the handshake without an updater.
    remove_if_present(&ready_path())?;
    remove_if_present(&failed_path())?;

    let mut command = if cfg!(windows) {
        let mut command = Command::new("powershell.exe");
        command.args(["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]);
        command
    } else {
        Command::new("/bin/bash")
    };
    command.arg(&wrapper);
    // AppImage-safe environment, own process group on Unix (outlives this
    // app), no console window on Windows.
    configure_child(&mut command);
    command
        .env("AIRELAYS_VERSION", &offer.version)
        .env("AIRELAYS_SHA256", &offer.sha256)
        .env("AIRELAYS_UPDATE_PID", std::process::id().to_string())
        .env("AIRELAYS_UPDATE_LOG", log_path())
        .env("AIRELAYS_UPDATE_READY", ready_path())
        .env("AIRELAYS_UPDATE_FAILED", failed_path())
        .env("AIRELAYS_UPDATE_INSTALLER", &installer)
        .env("AIRELAYS_UPDATE_RELAUNCH", &target.relaunch)
        .env_remove("AIRELAYS_APP_DIR")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    if let Some(app_dir) = &target.app_dir {
        command.env("AIRELAYS_APP_DIR", app_dir);
    }
    let mut child = spawn_detached(&mut command)?;

    let started = Instant::now();
    while !ready_path().exists() {
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!(
                "The updater stopped before it started ({status}). Details: {}",
                log_path().display()
            ));
        }
        if started.elapsed() > READY_TIMEOUT {
            let _ = child.kill();
            let _ = child.wait();
            return Err("The updater did not start. Your system may block scripts; \
                        download the update from the release page instead."
                .into());
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    // Reap on Unix if this app outlives it.
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

/// Spawns outside any Job Object this app may run in (some launchers and
/// terminals put their children in a kill-on-close job), so the updater
/// survives the app's exit. Falls back when the job forbids breakaway.
#[cfg(windows)]
fn spawn_detached(command: &mut Command) -> Result<std::process::Child, String> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
    command.creation_flags(CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB);
    command.spawn().or_else(|_| {
        command.creation_flags(CREATE_NO_WINDOW);
        command.spawn().map_err(|error| format!("Cannot start the updater: {error}"))
    })
}

#[cfg(not(windows))]
fn spawn_detached(command: &mut Command) -> Result<std::process::Child, String> {
    command.spawn().map_err(|error| format!("Cannot start the updater: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const DIGEST: &str = "sha256:f811f31b5905423504c3cf7b06771652c7fe0ebfe29402d62f31401e76835eb7";

    fn release(tag: &str, assets: &[&str], draft: bool, prerelease: bool) -> Value {
        json!({
            "tag_name": tag,
            "draft": draft,
            "prerelease": prerelease,
            "html_url": format!("https://github.com/lpalbou/AIRelays/releases/tag/{tag}"),
            "published_at": "2026-10-18T00:00:00Z",
            "body": format!("notes for {tag}"),
            "assets": assets.iter().map(|name| json!({"name": name, "digest": DIGEST})).collect::<Vec<_>>(),
        })
    }

    #[test]
    fn versions_parse_only_plain_stable_triples() {
        assert_eq!(parse_version("v0.17.2"), Some((0, 17, 2)));
        assert_eq!(parse_version("1.10.0"), Some((1, 10, 0)));
        for bad in ["0.18.0-rc1", "0.18", "0.18.0.1", "", "vv0.1.0", "+0.1.0", "0.1.+1", " 0.1.0"] {
            assert_eq!(parse_version(bad), None, "{bad:?}");
        }
        assert_eq!(parse_current("0.18.0-rc1"), Some((0, 18, 0)));
    }

    #[test]
    fn newest_stable_release_with_this_platforms_installer_wins() {
        let releases = vec![
            release("v0.19.0", &["AIRelays_0.19.0_x64-setup.exe"], true, false),
            release("v0.18.1", &["AIRelays_0.18.1_x64-setup.exe"], false, true),
            // Installers not attached yet.
            release("v0.18.0", &["airelays-0.18.0.tar.gz"], false, false),
            release("v0.17.10", &["AIRelays_0.17.10_x64-setup.exe"], false, false),
            release("v0.17.9", &["AIRelays_0.17.9_x64-setup.exe"], false, false),
        ];
        let (info, offer) = summarize("0.17.2", &releases, "_x64-setup.exe");
        assert_eq!(info.latest.as_deref(), Some("0.17.10"));
        assert!(info.update_available);
        assert_eq!(info.release_notes.as_deref(), Some("notes for v0.17.10"));
        assert_eq!(
            offer,
            Some(Offer { version: "0.17.10".into(), sha256: DIGEST["sha256:".len()..].into() })
        );
    }

    #[test]
    fn same_or_older_release_is_not_offered() {
        let releases = vec![release("v0.17.2", &["AIRelays_0.17.2_aarch64.dmg"], false, false)];
        let (info, offer) = summarize("0.17.2", &releases, "_aarch64.dmg");
        assert!(!info.update_available);
        assert_eq!(offer, None);
        assert!(!summarize("0.18.0", &releases, "_aarch64.dmg").0.update_available);
        assert!(summarize("0.17.2-rc1", &[release("v0.17.3", &["x_aarch64.dmg"], false, false)], "_aarch64.dmg").0.update_available);
        let (none, _) = summarize("0.17.2", &releases, "_x64-setup.exe");
        assert_eq!(none.latest, None);
        assert!(!none.update_available);
    }

    #[test]
    fn an_installer_without_a_checksum_is_not_installed_in_place() {
        let mut unverified = release("v0.18.0", &["AIRelays_0.18.0_x64-setup.exe"], false, false);
        unverified["assets"][0]["digest"] = Value::Null;
        let (info, offer) = summarize("0.17.2", &[unverified], "_x64-setup.exe");
        assert!(info.update_available);
        assert_eq!(offer, None);
        assert!(info.install_hint.is_some());
    }

    #[test]
    fn long_notes_are_cut_on_a_line_boundary() {
        let body = "line\n".repeat(NOTES_LIMIT);
        let notes = truncate_notes(&body);
        assert!(notes.ends_with("line\n\n…"));
        assert!(notes.chars().count() <= NOTES_LIMIT + 3);
    }
}
